#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 宝塔面板 —— 启动脚本
#
# 为什么需要这个脚本：
#   宝塔的 Python 项目管理器通过「启动命令拉起的那个前台进程的 PID」
#   来判断项目是否存活。它不擅长管理「一个项目起多个进程」的场景。
#
#   而本项目需要两个进程：
#     Web    —— 处理 HTTP + SSE 推流
#     Worker —— 消费 Redis 队列，干下载/ASR/总结这些重活
#
#   做法：
#     Web    用 exec 前台接管（PID 与宝塔记录的一致，宝塔能正确判活）
#     Worker 用 nohup 后台拉起，PID 写进 .run/worker.pid，由 bt_stop.sh 负责收尸
#
# 用法（宝塔「启动命令」里填）：
#   bash /www/wwwroot/bsum/deploy/bt_start.sh
#
# 对应停止命令填：
#   bash /www/wwwroot/bsum/deploy/bt_stop.sh
# ═══════════════════════════════════════════════════════════════

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

RUN_DIR="$APP_DIR/.run"
LOG_DIR="$APP_DIR/logs"
VENV_BIN="$APP_DIR/.venv/bin"
PY="$VENV_BIN/python"

mkdir -p "$RUN_DIR" "$LOG_DIR"

WEB_PID_FILE="$RUN_DIR/web.pid"
WORKER_PID_FILE="$RUN_DIR/worker.pid"

log() { echo "[bt_start] $*"; }

# ─── 前置检查 ───
if [[ ! -x "$PY" ]]; then
    echo "[bt_start] 错误：找不到虚拟环境 $PY" >&2
    echo "[bt_start] 请先执行：" >&2
    echo "  cd $APP_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "[bt_start] 错误：缺少 .env，请先 cp .env.example .env 并填写" >&2
    exit 1
fi

# ─── 初始化数据库（幂等，失败不阻断启动）───
# 建表 + 写默认配置 + 建管理员账号。重复执行无害。
log "初始化数据库…"
if ! "$PY" -m app.bootstrap >>"$LOG_DIR/bootstrap.log" 2>&1; then
    echo "[bt_start] 警告：bootstrap 失败，详见 $LOG_DIR/bootstrap.log" >&2
    echo "[bt_start] 继续尝试启动，但数据库表可能不完整。" >&2
fi

# ─── 清理可能残留的旧 Worker ───
# 宝塔重启项目时不一定能清干净后台进程，先按 PID 文件收一遍
if [[ -f "$WORKER_PID_FILE" ]]; then
    OLD_PID="$(cat "$WORKER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "发现残留 Worker (pid=$OLD_PID)，先停止"
        kill "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 0.5
        done
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$WORKER_PID_FILE"
fi

# ─── 启动 Worker（后台，nohup 脱离终端）───
# 用 setsid/nohup 让它不随本脚本退出而终止。
WORKER_LOG="$LOG_DIR/worker.log"
log "启动 Worker…"
nohup "$PY" -m app.workers.worker >>"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" >"$WORKER_PID_FILE"
log "Worker 已启动 pid=$WORKER_PID，日志：$WORKER_LOG"

# 给它两秒确认没立刻崩（比如 Redis 连不上）
sleep 2
if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[bt_start] 错误：Worker 启动后立即退出，最后 30 行日志：" >&2
    tail -n 30 "$WORKER_LOG" >&2 || true
    rm -f "$WORKER_PID_FILE"
    exit 1
fi

# ─── 启动 Web（exec 前台接管）───
# 关键：exec 让 uvicorn 的 PID 与本脚本 PID 一致，
# 宝塔记录的就是它，判活/停止都能对上。
PORT="${APP_PORT:-8000}"
echo "$$" >"$WEB_PID_FILE"

log "启动 Web → 127.0.0.1:${PORT}（前台接管，宝塔可正确判活）"
exec "$VENV_BIN/uvicorn" app.main:app \
    --host 127.0.0.1 \
    --port "$PORT" \
    --proxy-headers \
    --forwarded-allow-ips='127.0.0.1' \
    --workers 1 \
    --log-level info
