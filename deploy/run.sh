#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 宝塔 Python 项目管理器 —— 启动脚本
#
# 用途：宝塔的「启动方式」如果选「自定义命令」，直接指到这里，
#       免得每次都要敲一长串 venv 绝对路径。
#
# 用法：
#   ./deploy/run.sh web      # 启动 Web
#   ./deploy/run.sh worker   # 启动 Worker
#
# 会自动 cd 到项目根目录，并加载 .env。
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# 脚本所在目录的上一级 = 项目根
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

PY="$APP_DIR/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "找不到虚拟环境：$PY" >&2
    echo "请先执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

ROLE="${1:-web}"

case "$ROLE" in
    web)
        PORT="${APP_PORT:-8000}"
        echo "[run.sh] 启动 Web → 127.0.0.1:${PORT}"
        # 只监听回环，对外统一走宝塔的 Nginx 反代
        exec "$APP_DIR/.venv/bin/uvicorn" app.main:app \
            --host 127.0.0.1 \
            --port "$PORT" \
            --proxy-headers \
            --forwarded-allow-ips='127.0.0.1' \
            --workers 1 \
            --log-level info
        ;;
    worker)
        echo "[run.sh] 启动 Worker（队列 ${QUEUE_NAME:-bsum_tasks}）"
        exec "$PY" -m app.workers.worker
        ;;
    bootstrap)
        # 宝塔项目启动前可以先用这个模式初始化一次
        exec "$PY" -m app.bootstrap
        ;;
    *)
        echo "用法：$0 {web|worker|bootstrap}" >&2
        exit 1
        ;;
esac
