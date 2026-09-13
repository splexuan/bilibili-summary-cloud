# 裸机部署指南（2 核 4G）

不用 Docker，直接在服务器上跑。适用于 **Ubuntu 22.04 / 24.04**（Debian 12 类似）。

---

## 为什么这个配置适合裸机

2 核 4G 跑这套东西余量充足：

| 组件 | 内存占用 | 说明 |
|---|---|---|
| Postgres | ~150 MB | 空库很小，数据多了会涨 |
| Redis | ~10 MB | 设了 256MB 上限，实际用得很少 |
| Web (uvicorn) | ~200 MB | 单进程 |
| Worker ×2 | ~500 MB | 大头是 ffmpeg 转码 |
| **合计** | **~900 MB** | 4G 里留了 3G 余量给 ffmpeg 峰值 |

Docker 会额外吃掉 200~300MB，还要多一层网络与卷管理。4G 机器上没必要。

---

## 一键部署

```bash
# 1. 把项目传到服务器（本地执行）
scp -r C:\Game\bilibili-summary-cloud root@你的服务器IP:/tmp/bsum

# 2. 登录服务器，执行安装脚本
ssh root@你的服务器IP
cd /tmp/bsum
bash deploy/install.sh
```

脚本会自动完成：装依赖 → 建用户 → 部署代码 → 建库 → 生成 `.env` → 装 systemd → 配 Nginx。

**跑完会打印管理员密码，只显示一次，立刻记下来。**

脚本是幂等的，失败了可以直接重跑。

---

## 脚本做了什么

### 1. 系统用户 `bsum`

不用 root 跑应用。系统用户 + `nologin` shell，只能被 systemd 拉起。

### 2. 目录结构

```
/opt/bsum/
├── app/                代码
├── .venv/              Python 虚拟环境
├── .env                配置（600 权限，含数据库密码）
├── data/               敏感数据（700 权限）
│   ├── cookies/        用户 Cookie
│   └── .cache/         yt-dlp 缓存
├── temp/               下载与转码的临时文件
├── logs/
│   ├── web.log / web.error.log
│   └── worker.log / worker.error.log
└── deploy/             systemd unit + nginx 配置
```

### 3. 两个 systemd 服务

| 服务 | 作用 | 内存上限 |
|---|---|---|
| `bsum-web` | FastAPI，监听 `127.0.0.1:8000` | 1 GB |
| `bsum-worker` | RQ Worker，跑下载/转码/ASR/总结 | 2.5 GB |

`bsum-web` 只监听回环地址，**对外只经 Nginx**，8000 端口不暴露。

### 4. 自动生成的密钥

- `SECRET_KEY` — Fernet 密钥，加密用户凭证
- `JWT_SECRET` — 会话签名
- `DATABASE_URL` — 含随机数据库密码
- `ADMIN_PASSWORD` — 随机 14 位

⚠️ **`SECRET_KEY` 一旦更换，已加密的用户凭证（DeepSeek Key / B站 Cookie）全部解不开。** 备份 `.env`。

---

## 部署后的必做项

### 1. 配密钥（最关键）

登录 `/admin` → 「密钥配置」：

| 配置项 | 从哪拿 |
|---|---|
| DeepSeek API Key | platform.deepseek.com |
| COS SecretId / SecretKey / 地域 / 存储桶 | 腾讯云控制台 → 对象存储 |
| ASR SecretId / SecretKey / 地域 | 腾讯云控制台 → 语音识别 |

**COS 地域必须和服务器地域一致。** 比如服务器在广州，COS 也要选广州。
跨地域会让 ASR 拉音频走公网，产生外网下行流量费。

### 2. 收紧安全组

云服务商控制台里，**只放行 80 和 443**。22 端口建议限制来源 IP。

8000 端口不用开 —— 它只监听 127.0.0.1。

### 3. 上 HTTPS

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

证书会自动续期。上完 HTTPS 记得把域名填进 `/opt/bsum/.env` 的 `APP_BASE_URL`：

```bash
sed -i 's|^APP_BASE_URL=.*|APP_BASE_URL=https://your-domain.com|' /opt/bsum/.env
systemctl restart bsum-web bsum-worker
```

---

## 日常运维

```bash
# 查状态
systemctl status bsum-web bsum-worker

# 看实时日志
journalctl -u bsum-web -f
tail -f /opt/bsum/logs/worker.log

# 重启
systemctl restart bsum-web bsum-worker

# 改代码后更新
cd /tmp/bsum && git pull          # 或重新 scp
bash deploy/install.sh             # 幂等，会同步代码并重启
```

### 升级 yt-dlp（重要）

B 站和 YouTube 接口经常变，yt-dlp 需要跟着升：

```bash
/opt/bsum/.venv/bin/pip install -U yt-dlp
systemctl restart bsum-worker
```

建议加个每月自动升级的定时任务：

```bash
cat > /etc/cron.monthly/bsum-ytdlp <<'EOF'
#!/bin/sh
/opt/bsum/.venv/bin/pip install -q -U yt-dlp && systemctl restart bsum-worker
EOF
chmod +x /etc/cron.monthly/bsum-ytdlp
```

---

## 排障

### SSE 进度不动

Nginx 缓冲没关干净。检查 `/etc/nginx/sites-available/bsum` 里 SSE 那个 location 块：

```nginx
proxy_buffering off;
proxy_cache off;
proxy_set_header X-Accel-Buffering no;
```

三行缺一不可。改完 `nginx -t && systemctl reload nginx`。

### 下载失败 / ffmpeg 报错

先确认 ffmpeg 在：

```bash
/opt/bsum/.venv/bin/python -c "from app.services.downloader import find_ffmpeg; print(find_ffmpeg())"
```

如果 Worker 日志出现 `Permission denied` 且路径含 `/root` 或 `/home`，
说明 `HOME` 没重设对 —— 检查 unit 里的：

```
Environment=HOME=/opt/bsum/data
Environment=XDG_CACHE_HOME=/opt/bsum/data/.cache
```

### 数据库连不上

```bash
# 手动测连通
PGPASSWORD=$(grep '^DATABASE_URL' /opt/bsum/.env | sed 's|.*://bsum:\([^@]*\)@.*|\1|') \
  psql -h 127.0.0.1 -U bsum -d bilibili_summary -c '\q'
```

失败通常是 `pg_hba.conf` 没放行 `127.0.0.1` 的密码认证，脚本会自动追加，
手动检查：`sudo -u postgres psql -tAc "SHOW hba_file"`。

### Worker 不起

```bash
journalctl -u bsum-worker -n 50 --no-pager
```

常见原因是 Redis 没起（`systemctl status redis-server`）或 `.env` 里的
`REDIS_URL` 不对。

### 内存不够 / 被 OOM Kill

```bash
dmesg | grep -i 'killed process'
```

如果是 ffmpeg 转码长视频被砍，把 `bsum-worker.service` 里的
`WORKER_CONCURRENCY` 降到 1：

```bash
sed -i 's|^Environment=WORKER_CONCURRENCY=.*|Environment=WORKER_CONCURRENCY=1|' \
  /etc/systemd/system/bsum-worker.service
systemctl daemon-reload && systemctl restart bsum-worker
```

---

## 想换回 Docker

随时可以。`docker-compose.yml` 一直在仓库里：

```bash
systemctl disable --now bsum-web bsum-worker nginx
docker compose up -d --build
```

代码是同一套，Dockerfile 只是打包方式不同。两种方式可以随时切换，
不影响数据（数据在 Postgres 和 COS 里，不在容器里）。
