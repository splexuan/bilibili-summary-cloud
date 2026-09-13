#!/usr/bin/env python
"""
排障脚本 —— 定位任务失败原因。

用法（在项目根目录）：
    .venv/bin/python deploy/doctor.py            # 全面体检
    .venv/bin/python deploy/doctor.py 12         # 只看 job 12 的详情

它会依次检查：
  1. 配置与依赖（.env / DB / Redis / ffmpeg / 日志目录）
  2. Web 与 Worker 进程是否都在跑
  3. 最近失败的 job，以及具体错误信息
  4. Redis 里的实时进度（能看到「卡在哪一步」）
  5. 日志文件里的异常堆栈
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 非 TTY（重定向到文件、宝塔日志面板）时关闭颜色，
# 否则日志里会混进一堆 \033[32m 之类的转义序列，很难看。
if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
    G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; D = "\033[2m"; B = "\033[1m"; X = "\033[0m"
else:
    G = R = Y = D = B = X = ""

OK = f"{G}OK{X}"; BAD = f"{R}!!{X}"; WARN = f"{Y}~{X}"


def head(t: str) -> None:
    print(f"\n{B}{'─' * 60}{X}\n{B}{t}{X}\n{B}{'─' * 60}{X}")


# ═══════════════════════════════════════════
# 1. 环境
# ═══════════════════════════════════════════

def check_env() -> dict:
    head("1. 配置与依赖")
    info: dict = {}

    env_file = ROOT / ".env"
    if env_file.exists():
        print(f"  {OK}  .env 存在")
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k in ("DATABASE_URL", "REDIS_URL", "APP_PORT", "APP_ENV"):
                # 密码脱敏
                v = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", v)
                print(f"       {D}{k}={v}{X}")
            info[k] = v
    else:
        print(f"  {BAD}  缺少 .env")
        return info

    # 关键配置
    for key in ("DEEPSEEK_API_KEY",):
        pass

    # ffmpeg
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            print(f"  {OK}  ffmpeg 可用：{out.stdout.splitlines()[0][:60]}")
        else:
            print(f"  {BAD}  ffmpeg 返回非零：{out.stderr[:200]}")
    except FileNotFoundError:
        print(f"  {BAD}  ffmpeg 不在 PATH 里 —— 无字幕视频会直接失败")
    except Exception as exc:
        print(f"  {WARN}  ffmpeg 检查异常：{exc}")

    # 日志目录
    log_dir = ROOT / "logs"
    if log_dir.exists():
        files = sorted(log_dir.glob("*.log"))
        print(f"  {OK}  logs/ 存在，{len(files)} 个日志文件")
        for f in files:
            size = f.stat().st_size
            print(f"       {D}{f.name}  {size / 1024:.1f} KB{X}")
    else:
        print(f"  {WARN}  logs/ 不存在（非生产环境不写文件日志）")

    info["_log_dir"] = str(log_dir)
    return info


# ═══════════════════════════════════════════
# 2. 进程
# ═══════════════════════════════════════════

def check_processes() -> None:
    head("2. 进程状态")

    def pgrep(pattern: str) -> list[str]:
        try:
            out = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True, timeout=10)
            return [l for l in out.stdout.splitlines() if l.strip()]
        except Exception:
            return []

    web = pgrep(r"uvicorn app\.main")
    worker = pgrep(r"app\.workers\.worker")

    if web:
        print(f"  {OK}  Web（uvicorn）{len(web)} 个进程")
        for l in web[:3]:
            print(f"       {D}{l[:110]}{X}")
    else:
        print(f"  {BAD}  没找到 Web 进程（uvicorn app.main）")

    if worker:
        print(f"  {OK}  Worker {len(worker)} 个进程")
        for l in worker[:5]:
            print(f"       {D}{l[:110]}{X}")
    else:
        print(f"  {BAD}  ！没找到 Worker 进程 —— 任务会一直停在「排队」不动")
        print(f"       {Y}这是最常见的「提交后无反应」原因{X}")

    # PID 文件
    run_dir = ROOT / ".run"
    if run_dir.exists():
        for f in sorted(run_dir.glob("*.pid")):
            pid = f.read_text(encoding="utf-8").strip() if f.exists() else ""
            alive = False
            if pid.isdigit():
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except Exception:
                    alive = False
            mark = OK if alive else BAD
            print(f"  {mark}  .run/{f.name} = {pid or '(空)'} {'存活' if alive else '已死'}")


# ═══════════════════════════════════════════
# 3. 数据库里的任务
# ═══════════════════════════════════════════

async def check_jobs(only_id: int | None = None) -> None:
    head("3. 最近的任务" + (f"（job_id={only_id}）" if only_id else ""))

    from sqlalchemy import desc, select

    from app.db.models import Job
    from app.db.session import session_scope

    async with session_scope() as db:
        if only_id:
            rows = [await db.get(Job, only_id)]
        else:
            rows = list(
                (await db.execute(select(Job).order_by(desc(Job.id)).limit(12))).scalars()
            )

        if not rows or rows[0] is None:
            print("  （没有任务记录）")
            return

        for job in rows:
            if job is None:
                continue
            status = job.status or "?"
            icon = {"done": OK, "error": BAD, "running": WARN}.get(status, D)
            print(
                f"  {icon}  #{job.id}  [{status}]  stage={job.stage}  "
                f"progress={job.progress or 0:.0f}%  type={job.type}"
            )
            if job.error:
                print(f"       {R}错误：{job.error[:400]}{X}")
            if job.rq_job_id:
                print(f"       {D}rq_job_id={job.rq_job_id}{X}")


# ═══════════════════════════════════════════
# 4. Redis 实时进度
# ═══════════════════════════════════════════

def check_redis() -> None:
    head("4. Redis 与实时进度")

    try:
        from app.core.progress import get_progress, get_redis

        r = get_redis()
        r.ping()
        print(f"  {OK}  Redis 连接正常")
    except Exception as exc:
        print(f"  {BAD}  Redis 连接失败：{exc}")
        print(f"       {Y}Worker 无法取任务，前端会一直「排队中」{X}")
        return

    try:
        keys = list(r.scan_iter("bsum:progress:*", count=200))
        print(f"  {OK}  Redis 里有 {len(keys)} 条进度记录")

        # 按时间倒序显示最近几条
        items = []
        for k in keys:
            raw = r.get(k)
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            jid = k.rsplit(":", 1)[-1]
            items.append((d.get("ts") or 0, jid, d))
        items.sort(reverse=True)

        for _, jid, d in items[:6]:
            stage = d.get("stage") or "(空)"
            msg = d.get("message") or ""
            pct = d.get("percent") or 0
            print(f"       {D}#{jid}  stage={stage}  {pct:.0f}%  {msg[:60]}{X}")
            if stage == "error":
                print(f"         {R}↑ 这就是报错内容{X}")
    except Exception as exc:
        print(f"  {WARN}  扫描进度 key 失败：{exc}")

    # 队列长度
    try:
        from app.core.config import settings

        qlen = r.llen(f"rq:queue:{settings.queue_name}")
        print(f"  {OK}  队列 {settings.queue_name} 长度 = {qlen}")
        if qlen > 0:
            print(f"       {Y}队列有积压 → Worker 没在消费，或并发不够{X}")
        failed = r.zcard(f"rq:queue:{settings.queue_name}:failed")
        print(f"  {'~' if failed else 'OK'}  RQ 失败任务数 = {failed}")
    except Exception as exc:
        print(f"  {D}  队列检查跳过：{exc}{X}")


# ═══════════════════════════════════════════
# 5. 日志里的异常
# ═══════════════════════════════════════════

def check_logs() -> None:
    head("5. 日志中的异常（最近 20 条）")

    log_dir = ROOT / "logs"
    if not log_dir.exists():
        print(f"  {WARN}  没有 logs/ 目录")
        print(f"       {D}如果宝塔里配了「日志」位置，去那里看：{X}")
        print(f"       {D}宝塔 → 网站/Python 项目 → 该项目 → 日志{X}")
        return

    pats = re.compile(
        r"(Traceback|ERROR|CRITICAL|Exception|任务失败|任务未预期失败|"
        r"ImportError|ModuleNotFoundError|OperationalError|ConnectionError|"
        r"ConfigError|DownloadError|ASRError|COSError|AIError)",
        re.I,
    )

    found = 0
    for f in sorted(log_dir.glob("*.log")):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        hits = [(i, l) for i, l in enumerate(lines) if pats.search(l)]
        if not hits:
            continue
        print(f"\n  {B}{f.name}{X}  （命中 {len(hits)} 行，显示最后 20 行）")
        for i, l in hits[-20:]:
            print(f"       {D}{i + 1}:{X} {l[:150]}")
            found += 1

    if found == 0:
        print(f"  {OK}  没发现异常行")


# ═══════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════

def main() -> None:
    print(f"{B}╔{'═' * 58}╗{X}")
    print(f"{B}║{'bilibili-summary-cloud  排障体检'.center(46)}║{X}")
    print(f"{B}╚{'═' * 58}╝{X}")
    print(f"  项目目录：{ROOT}")

    only_id = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None

    check_env()
    check_processes()

    try:
        asyncio.run(check_jobs(only_id))
    except Exception as exc:
        print(f"\n  {BAD}  数据库查询失败：{exc}")
        print(f"       {D}检查 .env 里的 DATABASE_URL 是否正确、数据库是否已启动{X}")

    check_redis()
    check_logs()

    head("快速结论")
    print("  前端显示「连接中断」= 任务进 error 态，具体原因在上面的 3/4/5 节。")
    print("  前端一直「排队中」不动  = Worker 没起来，看第 2 节。")
    print("  进度条卡住     = Nginx 没关缓冲，或 Worker 卡在下载/ASR。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断")
