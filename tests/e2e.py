"""
端到端流水线测试 —— 真的跑一遍 Worker + 任务函数。

用「文章总结」这条链路，因为它不依赖 yt-dlp / ffmpeg / COS / ASR，
只需要一个 AI 客户端（这里用打桩的假客户端），能在本机完整验证：

    入队 → Worker 取任务 → 跑 _run_summarize_job → 写库 → 标记完成

这能覆盖冒烟测试覆盖不到的部分：
- session_scope 在 worker 里能不能正常用
- 进度写入 / 完成标记是否正确
- UseageMonthly 记账是否落库
"""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bsum_e2e_"))
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP / 'e2e.db'}"
os.environ["SECRET_KEY"] = "uL9k2YqWm3nR8tVx5zA7bC1dE4fG6hJ0kL2mN4oP6qR="
os.environ["JWT_SECRET"] = "e2e_test_jwt_secret_long_enough_for_hs256"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test123456"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.disable(logging.CRITICAL)

import fakeredis  # noqa: E402

import app.core.progress as progress_mod  # noqa: E402

_fake = fakeredis.FakeStrictRedis(decode_responses=False)
progress_mod._redis = _fake
progress_mod.get_redis = lambda: _fake

RESULTS = []


def chk(cond, msg):
    RESULTS.append((cond, msg))
    print(("  OK   " if cond else "  FAIL ") + msg)


# ══════════════════════════════════════════
# 打桩：假 AI 客户端（不联网）
# ══════════════════════════════════════════

FAKE_SUMMARY = "## 核心观点\n\n这是一份由测试桩生成的总结。\n\n- 要点一\n- 要点二\n"
CALLS = {"n": 0}


def fake_summarize_sync(client, text, title, on_progress=None, **kw):
    CALLS["n"] += 1
    if on_progress:
        on_progress("正在分析分段 1/2…")
        on_progress("正在汇总…")
    return {"summary": f"# {title}\n\n{FAKE_SUMMARY}", "tokens": 1234}


print("=== A. 建表 ===")


async def _prepare():
    from app.db.models import Base
    from app.db.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


asyncio.run(_prepare())
chk(True, "SQLite 建表完成")


async def _bootstrap():
    from app.main import _bootstrap

    await _bootstrap()


asyncio.run(_bootstrap())
chk(True, "bootstrap 完成（管理员 + 默认配置）")


print("\n=== B. 造数据：用户 + 文章 + 任务 ===")


async def _seed():
    from sqlalchemy import select

    from app.core.constants import JobStatus, JobType
    from app.core.security import hash_password
    from app.db.models import Article, Job, User
    from app.db.session import session_scope

    async with session_scope() as db:
        user = (
            await db.execute(select(User).where(User.username == "admin"))
        ).scalar_one()
        user.can_use_own_key = False

        article = Article(
            user_id=user.id,
            title="端到端测试文章",
            url="",
            text="这是一段用于端到端测试的正文。" * 100,
        )
        db.add(article)
        await db.flush()

        job = Job(
            user_id=user.id,
            type=JobType.SUMMARIZE_ARTICLE,
            status=JobStatus.PENDING,
            stage="queued",
            article_id=article.id,
            params="{}",
        )
        db.add(job)
        await db.flush()

        return user.id, article.id, job.id


UID, AID, JID = asyncio.run(_seed())
chk(JID > 0, f"已创建 user={UID} article={AID} job={JID}")


print("\n=== C. 入队（真 RQ）+ Worker 执行 ===")

# 打桩 AI 客户端构造，避免真的调用 DeepSeek
import app.workers.tasks as tasks_mod  # noqa: E402

tasks_mod.summarize_sync = fake_summarize_sync


async def _fake_build_client(db, user):
    return object()  # 假的，反正 summarize_sync 被打了桩


tasks_mod._build_ai_client = _fake_build_client

from app.core.queue import enqueue_summarize, get_queue  # noqa: E402

rq_id = enqueue_summarize(JID, UID)
chk(bool(rq_id), f"入队成功 rq_id={rq_id}")
chk(len(get_queue()) == 1, f"队列长度 = {len(get_queue())}")

# 用 RQ 的 SimpleWorker 同步跑一次（不阻塞、不常驻）
from rq import SimpleWorker  # noqa: E402

from app.workers.tasks import run_summarize_job  # noqa: E402

q = get_queue()
w = SimpleWorker([q], connection=_fake)
w.work(burst=True)  # burst=True：把队列里的任务跑完就退出

chk(True, "Worker burst 执行完毕")
chk(CALLS["n"] >= 1, f"AI 总结函数被调用 {CALLS['n']} 次")


print("\n=== D. 校验落库结果 ===")


async def _check():
    from app.db.models import Article, Job, UsageMonthly, Video
    from app.db.session import session_scope

    out = {}
    async with session_scope() as db:
        job = await db.get(Job, JID)
        art = await db.get(Article, AID)
        out["job_status"] = job.status
        out["job_stage"] = job.stage
        out["job_progress"] = job.progress
        out["job_error"] = job.error
        out["job_finished"] = job.finished_at
        out["summary"] = art.summary or ""

        ym_rows = (await db.execute(
            __import__("sqlalchemy").select(UsageMonthly)
        )).scalars().all()
        out["usage"] = [(r.year_month, r.summary_count, r.ai_tokens) for r in ym_rows]
    return out


res = asyncio.run(_check())

chk(res["job_status"] == "done", f"任务状态 = {res['job_status']}")
chk(res["job_stage"] == "done", f"任务阶段 = {res['job_stage']}")
chk(res["job_progress"] == 100.0, f"进度 = {res['job_progress']}")
chk(not res["job_error"], f"无错误信息（error={res['job_error']!r}）")
chk(bool(res["job_finished"]), f"完成时间已写入 = {res['job_finished']}")
chk("测试桩生成的总结" in res["summary"], f"总结已落库（{len(res['summary'])} 字）")
chk(res["summary"].startswith("# 端到端测试文章"), "总结标题取自文章标题")
chk(len(res["usage"]) == 1 and res["usage"][0][2] == 1234,
    f"用量记账正确：{res['usage']}")


print("\n=== E. 进度键（Redis）===")
prog = progress_mod.get_progress(JID)
chk(prog is not None, f"Redis 中存在进度数据：{prog}")


print("\n=== F. 失败路径：AI 报错时应落 error 状态 ===")


async def _seed_fail():
    from app.core.constants import JobStatus, JobType
    from app.db.models import Article, Job
    from app.db.session import session_scope

    async with session_scope() as db:
        art = Article(user_id=UID, title="会失败的文章", url="", text="正文" * 200)
        db.add(art)
        await db.flush()
        job = Job(
            user_id=UID,
            type=JobType.SUMMARIZE_ARTICLE,
            status=JobStatus.PENDING,
            stage="queued",
            article_id=art.id,
            params="{}",
        )
        db.add(job)
        await db.flush()
        return job.id


JID2 = asyncio.run(_seed_fail())


def exploding_summarize(client, text, title, on_progress=None, **kw):
    raise RuntimeError("模拟 AI 服务 500")


tasks_mod.summarize_sync = exploding_summarize

enqueue_summarize(JID2, UID)
SimpleWorker([get_queue()], connection=_fake).work(burst=True)


async def _check_fail():
    from app.db.models import Job
    from app.db.session import session_scope

    async with session_scope() as db:
        job = await db.get(Job, JID2)
        return job.status, job.error


st2, err2 = asyncio.run(_check_fail())
chk(st2 == "error", f"失败任务状态 = {st2}")
chk(bool(err2) and "模拟 AI 服务 500" in err2, f"错误信息已落库：{err2!r}")


print("\n=== G. RQ 注册表 ===")
# 注意：run_summarize_job 内部已经 try/except 兜住所有异常，
# 并把失败写进我们自己的 jobs/Redis —— 所以 RQ 视角下任务都是 finished，
# failed 注册表为空是【设计如此】，不是 bug。
q = get_queue()
chk(len(q.finished_job_registry) >= 1, f"finished 注册表有 {len(q.finished_job_registry)} 个")
chk(len(q.failed_job_registry) == 0,
    f"failed 注册表为 {len(q.failed_job_registry)} 个（异常已在任务内兜住，符合设计）")


print("\n" + "=" * 56)
failed = [m for okk, m in RESULTS if not okk]
print(f"合计 {len(RESULTS)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败清单：")
    for f in failed:
        print("  - " + f)
print("=" * 56)
sys.exit(1 if failed else 0)
