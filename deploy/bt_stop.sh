#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# 宝塔面板 —— 停止脚本
#
# 与 bt_start.sh 配对使用。
#
# 宝塔停止项目时会 kill 掉它记录的那个前台 PID（即 bt_start.sh
# exec 出来的 uvicorn）。但 Worker 是 nohup 后台拉起的，宝塔不知道
# 它的存在，所以必须在这里显式收掉，否则会变成孤儿进程一直占着
# Redis 队列、内存和数据库连接。
#
# 用法：
#   bash /www/wwwroot/bsum/deploy/bt_stop.sh
# ═══════════════════════════════════════════════════════════════

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$APP_DIR/.run"
WORKER_PID_FILE="$RUN_DIR/worker.pid"
WEB_PID_FILE="$RUN_DIR/web.pid"

log() { echo "[bt_stop] $*"; }

# ─── 停止 Worker ───
stop_by_pidfile() {
    local name="$1" pidfile="$2" sig="${3:-TERM}"

    [[ -f "$pidfile" ]] || { log "$name 无 PID 文件，跳过"; return 0; }

    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    rm -f "$pidfile"

    if [[ -z "${pid:-}" ]]; then
        log "$name PID 文件为空，跳过"
        return 0
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        log "$name (pid=$pid) 已不存在"
        return 0
    fi

    log "停止 $name (pid=$pid)…"
    kill "-$sig" "$pid" 2>/dev/null || true

    # 最多等 15 秒优雅退出
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || { log "$name 已退出"; return 0; }
        sleep 0.5
    done

    log "$name 未响应，强制 kill -9"
    kill -9 "$pid" 2>/dev/null || true
    sleep 0.5
    return 0
}

stop_by_pidfile "Worker" "$WORKER_PID_FILE" TERM

# ─── 兜底：清理子进程 ───
# Worker 用 RQ WorkerPool 时会 fork 子进程。父进程被 kill 后
# 子进程理论上会跟着退，但异常情况下可能残留，这里兜一遍。
sleep 1
ORPHANS="$(pgrep -f "app\.workers\.worker" 2>/dev/null || true)"
if [[ -n "$ORPHANS" ]]; then
    log "发现残留 Worker 进程，清理：$(echo "$ORPHANS" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill $ORPHANS 2>/dev/null || true
    sleep 1
    ORPHANS2="$(pgrep -f "app\.workers\.worker" 2>/dev/null || true)"
    if [[ -n "$ORPHANS2" ]]; then
        # shellcheck disable=SC2086
        kill -9 $ORPHANS2 2>/dev/null || true
    fi
fi

# ─── Web ───
# Web 是宝塔自己 kill 的前台进程，通常不需要我们动手。
# 但如果有人手动调这个脚本（比如排障），这里也清一下。
if [[ -f "$WEB_PID_FILE" ]]; then
    WEB_PID="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
    rm -f "$WEB_PID_FILE"
    if [[ -n "${WEB_PID:-}" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
        # 注意：如果当前就是宝塔在 kill 我们，这里可能误伤自己，
        # 所以只处理「PID 不是自己也不是父进程」的情况。
        if [[ "$WEB_PID" != "$$" && "$WEB_PID" != "$PPID" ]]; then
            log "停止 Web (pid=$WEB_PID)…"
            kill -TERM "$WEB_PID" 2>/dev/null || true
        fi
    fi
fi

rm -f "$WEB_PID_FILE"

# ─── 顺带清理运行期临时目录 ───
# temp/ 存下载中转的音频/视频，正常跑完会自己删，
# 崩溃时可能留下大文件占磁盘。
if [[ -d "$APP_DIR/temp" ]]; then
    log "清理临时文件：$APP_DIR/temp"
    find "$APP_DIR/temp" -mindepth 1 -delete 2>/dev/null || true
fi

log "已停止"
