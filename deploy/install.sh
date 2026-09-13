#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# bilibili-summary-cloud — 裸机一键部署脚本
#
# 目标环境：Ubuntu 22.04 / 24.04（Debian 12 也可）
# 配置要求：2 核 4G
#
# 用法：
#   sudo bash deploy/install.sh
#
# 脚本做的事：
#   1. 装系统依赖（Python / Postgres / Redis / ffmpeg / Nginx）
#   2. 建系统用户 bsum（不用 root 跑应用）
#   3. 把项目复制到 /opt/bsum，建 venv 装依赖
#   4. 初始化 Postgres 库与账号
#   5. 生成 .env（自动生成密钥，随机管理员密码）
#   6. 装 systemd 服务并启动
#   7. 配 Nginx 并热加载
#
# 幂等：可以重复执行，已存在的步骤会跳过。
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ─── 可调参数（想改就改这里）───
APP_DIR="/opt/bsum"
APP_USER="bsum"
DB_NAME="bilibili_summary"
DB_USER="bsum"
DB_PASS="$(openssl rand -hex 16)"
# 留空 = 自动探测系统自带的 python3 版本
# Ubuntu 24.04 → 3.12；Ubuntu 22.04 → 3.10（3.12 不在默认源里）
PY_VER=""

# 从哪复制项目：脚本所在的上一级目录
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── 颜色输出 ───
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; NC=$'\033[0m'

info()  { echo "${BLUE}[INFO]${NC} $*"; }
ok()    { echo "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo "${YELLOW}[WARN]${NC} $*"; }
die()   { echo "${RED}[FAIL]${NC} $*" >&2; exit 1; }

step() {
    echo
    echo "${BLUE}══════════════════════════════════════════${NC}"
    echo "${BLUE}  $*${NC}"
    echo "${BLUE}══════════════════════════════════════════${NC}"
}

# ─── 前置检查 ───
[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/install.sh"

if ! command -v apt-get >/dev/null 2>&1; then
    die "只支持 Debian/Ubuntu。CentOS 请把 apt-get 换成 dnf 自行调整。"
fi

# 检查内存，给个提示（不阻断）
MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
if [[ $MEM_MB -lt 1800 ]]; then
    warn "检测到内存仅 ${MEM_MB}MB，1G 机器建议改用 Docker 方案或加 swap。"
fi

# ═══════════════════════════════════════════════════════════════
step "1/7 安装系统依赖"
# ═══════════════════════════════════════════════════════════════

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# 自动探测 Python 版本：优先用系统已有的，避免去装源里没有的版本
if [[ -z "$PY_VER" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        info "探测到系统 Python ${PY_VER}"
    else
        die "系统未安装 python3，请先 apt install python3"
    fi
fi

# 依赖要求的 Python 最低版本
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    die "Python 版本过低（当前 ${PY_VER}），本项目要求 >= 3.10。Ubuntu 请升级到 22.04+。"
fi

apt-get install -y -qq \
    "python${PY_VER}" "python${PY_VER}-venv" python3-pip \
    postgresql postgresql-contrib \
    redis-server \
    ffmpeg \
    nginx \
    rsync \
    curl ca-certificates openssl

ok "系统依赖安装完成"
info "Python: $(python${PY_VER} --version)"
info "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
info "Postgres: $(psql --version)"
info "Redis:    $(redis-server --version | cut -d' ' -f1-3)"

# ═══════════════════════════════════════════════════════════════
step "2/7 创建应用用户"
# ═══════════════════════════════════════════════════════════════

if id "$APP_USER" &>/dev/null; then
    info "用户 $APP_USER 已存在，跳过"
else
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
    ok "已创建系统用户 $APP_USER"
fi

# ═══════════════════════════════════════════════════════════════
step "3/7 部署代码到 $APP_DIR"
# ═══════════════════════════════════════════════════════════════

if [[ "$SRC_DIR" == "$APP_DIR" ]]; then
    info "已在 $APP_DIR 内运行，跳过复制"
else
    mkdir -p "$APP_DIR"
    # 只同步代码，跳过本地开发产物
    rsync -a --delete \
        --exclude '.git' \
        --exclude '.venv' \
        --exclude '__pycache__' \
        --exclude 'data' \
        --exclude 'temp' \
        --exclude 'logs' \
        --exclude '*.pyc' \
        --exclude '.env' \
        "$SRC_DIR"/ "$APP_DIR"/
    ok "代码已同步到 $APP_DIR"
fi

mkdir -p "$APP_DIR"/{data,temp,logs}

# ─── venv ───
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    info "venv 已存在，跳过创建"
else
    python${PY_VER} -m venv "$APP_DIR/.venv"
    ok "已创建 venv"
fi

info "安装 Python 依赖（可能需要几分钟）…"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Python 依赖安装完成"

# ═══════════════════════════════════════════════════════════════
step "4/7 初始化数据库"
# ═══════════════════════════════════════════════════════════════

systemctl enable --now postgresql >/dev/null 2>&1
systemctl enable --now redis-server >/dev/null 2>&1

# 等 Postgres 就绪
for i in $(seq 1 30); do
    if sudo -u postgres psql -c '\q' >/dev/null 2>&1; then break; fi
    sleep 1
done
sudo -u postgres psql -c '\q' >/dev/null 2>&1 || die "Postgres 未能启动"

# 建库建用户（幂等）
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    info "数据库用户 $DB_USER 已存在，更新密码"
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" >/dev/null
else
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" >/dev/null
    ok "已创建数据库用户 $DB_USER"
fi

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    info "数据库 $DB_NAME 已存在，跳过"
else
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" >/dev/null
    ok "已创建数据库 $DB_NAME"
fi

# Postgres 默认只允许本地 socket + peer 认证，应用用 TCP 连
# 检查 pg_hba 是否允许 md5/scram 本地 TCP
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
if ! grep -qE '^host\s+all\s+all\s+127\.0\.0\.1/32\s+(scram-sha-256|md5)' "$PG_HBA"; then
    warn "pg_hba.conf 未放行 127.0.0.1 TCP 密码认证，正在追加…"
    echo "host    all             all             127.0.0.1/32            scram-sha-256" >> "$PG_HBA"
    systemctl reload postgresql
    ok "已放行本地 TCP 认证"
fi

# 验证连通
if PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -c '\q' >/dev/null 2>&1; then
    ok "数据库连接验证通过"
else
    die "数据库连接失败，请检查 pg_hba.conf 与密码"
fi

# ═══════════════════════════════════════════════════════════════
step "5/7 生成 .env"
# ═══════════════════════════════════════════════════════════════

ENV_FILE="$APP_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env 已存在，保留原有配置（不覆盖）"
    warn "如需重新生成：sudo rm $ENV_FILE 后重跑本脚本"
    ADMIN_PASS="(见已有 .env)"
else
    FERNET_KEY=$("$APP_DIR/.venv/bin/python" -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    JWT_KEY=$(openssl rand -hex 32)
    ADMIN_PASS=$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-14)
    # 从模板生成，替换掉所有占位值
    sed \
        -e "s|^APP_ENV=.*|APP_ENV=production|" \
        -e "s|^APP_BASE_URL=.*|APP_BASE_URL=http://$(hostname -I | awk '{print $1}')|" \
        -e "s|^SECRET_KEY=.*|SECRET_KEY=${FERNET_KEY}|" \
        -e "s|^JWT_SECRET=.*|JWT_SECRET=${JWT_KEY}|" \
        -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}|" \
        -e "s|^DATABASE_URL_SYNC=.*|DATABASE_URL_SYNC=postgresql+psycopg://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}|" \
        -e "s|^REDIS_URL=.*|REDIS_URL=redis://127.0.0.1:6379/0|" \
        -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASS}|" \
        "$APP_DIR/.env.example" > "$ENV_FILE"
    ok "已生成 $ENV_FILE（密钥自动生成）"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$ENV_FILE"          # 里面有数据库密码与加密密钥
chmod 700 "$APP_DIR/data"      # 存放用户 Cookie 等敏感文件

# ═══════════════════════════════════════════════════════════════
step "6/7 安装 systemd 服务"
# ═══════════════════════════════════════════════════════════════

cp "$APP_DIR/deploy/bsum-web.service"    /etc/systemd/system/
cp "$APP_DIR/deploy/bsum-worker.service" /etc/systemd/system/
systemctl daemon-reload

systemctl enable bsum-web bsum-worker >/dev/null 2>&1
systemctl restart bsum-web

# 等 Web 起来
info "等待 Web 服务就绪…"
WEB_OK=0
for i in $(seq 1 40); do
    if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
        WEB_OK=1; break
    fi
    sleep 1
done

if [[ $WEB_OK -eq 1 ]]; then
    ok "Web 服务健康检查通过"
else
    warn "Web 服务未在 40 秒内就绪，请查看日志："
    warn "  journalctl -u bsum-web -n 50 --no-pager"
    warn "  tail -50 $APP_DIR/logs/web.error.log"
fi

systemctl restart bsum-worker
sleep 3
if systemctl is-active --quiet bsum-worker; then
    ok "Worker 服务已启动"
else
    warn "Worker 启动异常，请查看：journalctl -u bsum-worker -n 50 --no-pager"
fi

# ═══════════════════════════════════════════════════════════════
step "7/7 配置 Nginx"
# ═══════════════════════════════════════════════════════════════

SERVER_IP=$(hostname -I | awk '{print $1}')

if [[ -d /etc/nginx/sites-available ]]; then
    sed "s|server_name your-domain.com;|server_name ${SERVER_IP};|" \
        "$APP_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/bsum
    ln -sf /etc/nginx/sites-available/bsum /etc/nginx/sites-enabled/bsum
    # 移除默认站点，避免抢占 80 端口
    rm -f /etc/nginx/sites-enabled/default
    if nginx -t >/dev/null 2>&1; then
        systemctl enable --now nginx >/dev/null 2>&1
        systemctl reload nginx
        ok "Nginx 配置完成"
    else
        warn "Nginx 配置校验失败，请手动检查：nginx -t"
    fi
else
    warn "未找到 /etc/nginx/sites-available，跳过 Nginx 配置"
fi

# ═══════════════════════════════════════════════════════════════
# 完成
# ═══════════════════════════════════════════════════════════════

echo
echo "${GREEN}══════════════════════════════════════════${NC}"
echo "${GREEN}  部署完成${NC}"
echo "${GREEN}══════════════════════════════════════════${NC}"
echo
echo "  访问地址：  http://${SERVER_IP}/"
echo "  管理后台：  http://${SERVER_IP}/admin"
echo
if [[ "$ADMIN_PASS" != "(见已有 .env)" ]]; then
    echo "  ${YELLOW}管理员账号：admin${NC}"
    echo "  ${YELLOW}管理员密码：${ADMIN_PASS}${NC}"
    echo "  ${YELLOW}← 请立即登录后修改，此密码只显示这一次${NC}"
    echo
fi
echo "  常用命令："
echo "    systemctl status bsum-web bsum-worker"
echo "    journalctl -u bsum-web -f"
echo "    tail -f ${APP_DIR}/logs/worker.log"
echo
echo "  ${YELLOW}下一步：${NC}"
echo "    1. 登录 /admin，在「密钥配置」里填 DeepSeek Key、COS、ASR 的密钥"
echo "    2. COS 地域必须与服务器地域一致，否则 ASR 拉音频会产生外网流量费"
echo "    3. 配好域名后跑 certbot 上 HTTPS：apt install certbot python3-certbot-nginx"
echo "                                      certbot --nginx -d 你的域名"
echo
echo "${YELLOW}  安全提醒：本机 8000 端口只监听 127.0.0.1，对外只经 Nginx。${NC}"
echo "${YELLOW}  如果云服务商有安全组，请只放行 80 / 443。${NC}"
echo
