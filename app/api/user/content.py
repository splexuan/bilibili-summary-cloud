"""用户端内容接口 — 视频/文章提交、任务进度、历史列表"""
import json
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select

from app.api.deps import CurrentUser, DbSession, PageParams
from app.api.schemas import ArticleSubmitReq, Ok, VideoSubmitReq
from app.core.config import settings
from app.core.constants import JobStatus, JobType
from app.core.exceptions import ConflictError, NotFoundError, RateLimitError
from app.core.logging import get_logger
from app.core.progress import (
    acquire_user_slot,
    get_asr_usage,
    get_progress,
    get_summary,
    release_user_slot,
    subscribe,
)
from app.core.queue import cancel_job, enqueue_summarize, get_queue
from app.core.titling import generate_title
from app.db.models import Article, Chat, Job, Video
from app.db.session import AsyncSessionLocal
from app.db.settings_repo import get_setting
from app.services.cos_service import get_cos

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["内容"])


# ═══════════════════════════════════════════
# 任务创建
# ═══════════════════════════════════════════

async def _ensure_slot(db, user_id: int) -> None:
    """检查用户并发上限，避免一个用户占满 Worker"""
    active = (
        await db.execute(
            select(func.count(Job.id)).where(
                Job.user_id == user_id,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
    ).scalar_one()

    from app.core.config import settings as st

    if active >= st.max_tasks_per_user:
        raise RateLimitError(
            f"你已有 {active} 个任务正在处理，请等待完成后再提交"
        )


@router.post("/video")
async def submit_video(req: VideoSubmitReq, user: CurrentUser, db: DbSession):
    """提交视频总结任务，立即返回 job_id，通过 /api/job/{id}/stream 订阅进度"""
    await _ensure_slot(db, user.id)

    job = Job(
        user_id=user.id,
        type=JobType.SUMMARIZE_VIDEO,
        status=JobStatus.PENDING,
        stage="queued",
        params=json.dumps({"url": req.url.strip(), "force": req.force}, ensure_ascii=False),
    )
    db.add(job)
    await db.flush()

    rq_id = enqueue_summarize(job.id, user.id)
    job.rq_job_id = rq_id
    await db.flush()

    logger.info("提交视频任务 job_id=%s user=%s", job.id, user.username)
    return {"job_id": job.id, "rq_id": rq_id, "status": JobStatus.PENDING}


@router.post("/article")
async def submit_article(req: ArticleSubmitReq, user: CurrentUser, db: DbSession):
    """提交文章总结任务。标题留空则尝试从正文提取。"""
    await _ensure_slot(db, user.id)

    title = (req.title or "").strip()
    if not title:
        # 与本地版一致：扫全行 + 标点处截断（见 app/core/titling.py）
        title = generate_title(req.text, fallback="")

    article = Article(
        user_id=user.id,
        title=title or "未命名文章",
        url=req.url.strip(),
        text=req.text,
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

    rq_id = enqueue_summarize(job.id, user.id)
    job.rq_job_id = rq_id
    await db.flush()

    return {
        "job_id": job.id,
        "article_id": article.id,
        "rq_id": rq_id,
        "status": JobStatus.PENDING,
    }


# ═══════════════════════════════════════════
# 重新总结
# ═══════════════════════════════════════════

async def _enqueue_resummarize(
    db, user_id: int, *, video_id: int | None = None, article_id: int | None = None
) -> dict:
    """新建一个 RE_SUMMARIZE 任务：复用已有转写/正文，只重跑 AI"""
    job = Job(
        user_id=user_id,
        type=JobType.RE_SUMMARIZE,
        status=JobStatus.PENDING,
        stage="queued",
        video_id=video_id,
        article_id=article_id,
        params="{}",
    )
    db.add(job)
    await db.flush()

    rq_id = enqueue_summarize(job.id, user_id)
    job.rq_job_id = rq_id
    await db.flush()

    logger.info(
        "提交重新总结 job_id=%s user_id=%s video_id=%s article_id=%s",
        job.id, user_id, video_id, article_id,
    )
    return {"job_id": job.id, "rq_id": rq_id, "status": JobStatus.PENDING}


@router.post("/videos/{video_id}/resummarize")
async def resummarize_video(video_id: int, user: CurrentUser, db: DbSession):
    """
    重新生成总结（不重新下载、不重新转写）。
    适用于换了模型、调整了提示词、或对上次结果不满意的场景。
    """
    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")
    if not (video.transcript or "").strip():
        raise ConflictError("该视频还没有转写内容，请先完成一次完整处理")

    await _ensure_slot(db, user.id)
    out = await _enqueue_resummarize(db, user.id, video_id=video_id)
    out["video_id"] = video_id
    return out


@router.post("/articles/{article_id}/resummarize")
async def resummarize_article(article_id: int, user: CurrentUser, db: DbSession):
    """重新生成文章总结（复用已保存的正文）"""
    article = await db.get(Article, article_id)
    if not article or article.user_id != user.id:
        raise NotFoundError("文章不存在")
    if not (article.text or "").strip():
        raise ConflictError("文章正文为空，无法重新总结")

    await _ensure_slot(db, user.id)
    out = await _enqueue_resummarize(db, user.id, article_id=article_id)
    out["article_id"] = article_id
    return out


# ═══════════════════════════════════════════
# 任务进度（SSE）
# ═══════════════════════════════════════════

@router.get("/job/{job_id}")
async def job_status(job_id: int, user: CurrentUser, db: DbSession):
    """查询任务当前状态（非流式，用于轮询兜底）"""
    job = await db.get(Job, job_id)
    if not job or job.user_id != user.id:
        raise NotFoundError("任务不存在")

    progress = get_progress(job_id) or {}
    return {
        "job_id": job.id,
        "type": job.type,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "error": job.error,
        "video_id": job.video_id,
        "article_id": job.article_id,
        "live": progress,
    }


@router.get("/job/{job_id}/stream")
async def job_stream(job_id: int, user: CurrentUser, db: DbSession):
    """
    SSE 订阅任务进度。

    【部署注意】反向代理必须关闭缓冲，否则进度不会实时推送：
        Nginx:  proxy_buffering off;  proxy_cache off;  X-Accel-Buffering: no
        Caddy:  默认支持流式
    """
    job = await db.get(Job, job_id)
    if not job or job.user_id != user.id:
        raise NotFoundError("任务不存在")

    async def event_gen():
        last_stage = None
        last_summary = ""
        try:
            while True:
                # RQ 在同步线程池中执行，这里用轮询 + 广播混合方式，
                # 兼顾实时性与 Worker 崩溃时的兜底
                progress = get_progress(job_id)

                if progress:
                    stage = progress.get("stage")
                    if stage != last_stage or progress.get("message"):
                        yield _sse("progress", progress)
                        last_stage = stage

                # AI 总结正文增量（Worker 边生成边追加到 Redis）：
                # 本地版能边写边看，云端版此前只有干等，这里补上
                live = get_summary(job_id)
                if live and live != last_summary:
                    yield _sse("summary", {"text": live})
                    last_summary = live

                # 终态一：Redis 进度宣告结束。
                # 这里必须回数据库取终态再下发 —— Worker 里 mark_done() 是
                # **先写 Redis、后写库**（tasks.py:143 与 144），所以单看
                # Redis 拿不到 video_id，前端就会因为定位不到结果而把标题
                # 卡在「AI 正在生成…」、闪烁光标也不消失。
                if progress and progress.get("stage") in ("done", "error"):
                    async for event in _terminal_events(job_id, final=True):
                        yield event
                    return

                # 终态二：Redis 事件丢失时用数据库兜底
                # 必须用独立会话：请求级 session 的 identity map 会缓存 Job，
                # 读不到 Worker 在其他会话里写入的最新状态
                async for event in _terminal_events(job_id, probes=1):
                    yield event
                    return

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("SSE 连接断开 job_id=%s", job_id)
            raise

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _terminal_events(
    job_id: int, probes: int = 12, interval: float = 0.2, final: bool = False
):
    """
    读取任务终态，产出对应的 SSE 事件；尚未结束时不产出任何事件。

    Worker 的收尾顺序是「先写 Redis 的 mark_done()，再写数据库」
    （tasks.py:143 → 144），两者之间有短暂窗口，所以这里允许重试几次
    再取 video_id/article_id。取不到时前端还会用 /api/job/{id} 兜底，
    不会出现「总结完成了但页面停留在 AI 正在生成…」。

    失败只发 error 事件：旧实现发的是 end{"stage":"error"}，而前端把
    end 一律当成功处理，导致任务失败也弹「处理完成」。

    final=True 表示调用方已确认任务结束，实在读不到也要发个 end 收尾，
    避免流在没有终止事件的情况下断掉（前端会误判成连接异常）。
    """
    for i in range(max(1, probes)):
        # 独立会话：请求级 session 的 identity map 会缓存 Job，
        # 读不到 Worker 在其他会话里写入的最新状态
        async with AsyncSessionLocal() as check_db:
            cur = await check_db.get(Job, job_id)

        if cur and cur.status in (JobStatus.DONE, JobStatus.ERROR):
            if cur.status == JobStatus.ERROR:
                yield _sse("error", {"message": cur.error or "任务失败"})
            else:
                yield _sse("end", {
                    "stage": "done",
                    "video_id": cur.video_id,
                    "article_id": cur.article_id,
                })
            return

        if i + 1 < probes:
            await asyncio.sleep(interval)

    if final:
        yield _sse("end", {"stage": "done"})


@router.post("/job/{job_id}/cancel", response_model=Ok)
async def cancel_job_api(job_id: int, user: CurrentUser, db: DbSession):
    job = await db.get(Job, job_id)
    if not job or job.user_id != user.id:
        raise NotFoundError("任务不存在")

    if job.status in (JobStatus.DONE, JobStatus.ERROR):
        return Ok(message="任务已结束")

    if cancel_job(job.rq_job_id):
        job.status = JobStatus.CANCELED
        release_user_slot(user.id)
        return Ok(message="任务已取消")

    return Ok(ok=False, message="任务已开始执行，无法取消")


# ═══════════════════════════════════════════
# 历史列表
# ═══════════════════════════════════════════

@router.get("/videos")
async def list_videos(user: CurrentUser, db: DbSession, page: PageParams):
    stmt = select(Video).where(Video.user_id == user.id)
    count_stmt = select(func.count(Video.id)).where(Video.user_id == user.id)

    if page.search:
        like = f"%{page.search}%"
        cond = Video.title.ilike(like) | Video.uploader.ilike(like)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(Video.processed_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).scalars().all()

    has_more = len(rows) > page.page_size

    cos = await get_cos(db)
    items = []
    for v in rows[: page.page_size]:
        # 原图 + COS 压缩后的展示图（列表里最多显示 96px，用原图纯属浪费）
        thumb, thumb_small = cos.display_urls(v.thumbnail_key, expires=3600)
        items.append({
            "id": v.id,
            "vid": v.vid,
            "title": v.title,
            "uploader": v.uploader,
            "duration_str": v.duration_str,
            "platform": v.platform,
            "thumbnail": thumb,
            "thumbnail_small": thumb_small,
            "has_summary": bool(v.summary),
            "transcript_source": v.transcript_source,
            "reused": bool(v.copied_from_video_id),
            "processed_at": v.processed_at.strftime("%Y-%m-%d %H:%M") if v.processed_at else "",
        })

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": has_more,
    }


@router.get("/videos/{video_id}")
async def get_video(video_id: int, user: CurrentUser, db: DbSession):
    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")

    thumb, thumb_small = "", ""
    try:
        cos = await get_cos(db)
        thumb, thumb_small = cos.display_urls(video.thumbnail_key, expires=3600)
    except Exception:
        pass

    return {
        "id": video.id,
        "vid": video.vid,
        "url": video.url,
        "title": video.title,
        "uploader": video.uploader,
        "duration_str": video.duration_str,
        "platform": video.platform,
        "thumbnail": thumb,
        "thumbnail_small": thumb_small,
        "summary": video.summary,
        "has_transcript": bool(video.transcript),
        "transcript_source": video.transcript_source,
        "reused": bool(video.copied_from_video_id),
        "processed_at": video.processed_at.strftime("%Y-%m-%d %H:%M") if video.processed_at else "",
    }


@router.get("/videos/{video_id}/transcript")
async def get_transcript(video_id: int, user: CurrentUser, db: DbSession):
    """查看原文转写（RAG 问答的素材，也可供人工核对）"""
    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")
    return {"transcript": video.transcript or "", "source": video.transcript_source}


@router.delete("/videos/{video_id}", response_model=Ok)
async def delete_video(video_id: int, user: CurrentUser, db: DbSession):
    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")

    # 清理 COS 封面
    if video.thumbnail_key:
        try:
            cos = await get_cos(db)
            await asyncio.to_thread(cos.delete, video.thumbnail_key)
        except Exception as exc:
            logger.warning("删除封面失败: %s", exc)

    await db.execute(delete(Chat).where(Chat.video_id == video_id))
    await db.delete(video)

    from app.services.retrieval import clear_kb_cache, clear_rag_cache

    clear_rag_cache(user.id, video.vid)
    clear_kb_cache(user.id)

    return Ok(message="已删除")


# ═══════════════════════════════════════════
# 文章列表
# ═══════════════════════════════════════════

@router.get("/articles")
async def list_articles(user: CurrentUser, db: DbSession, page: PageParams):
    stmt = select(Article).where(Article.user_id == user.id)
    count_stmt = select(func.count(Article.id)).where(Article.user_id == user.id)

    if page.search:
        like = f"%{page.search}%"
        stmt = stmt.where(Article.title.ilike(like))
        count_stmt = count_stmt.where(Article.title.ilike(like))

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(Article.processed_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).scalars().all()

    has_more = len(rows) > page.page_size
    items = [
        {
            "id": a.id,
            "title": a.title,
            "url": a.url,
            "has_summary": bool(a.summary),
            "processed_at": a.processed_at.strftime("%Y-%m-%d %H:%M") if a.processed_at else "",
        }
        for a in rows[: page.page_size]
    ]

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": has_more,
    }


@router.get("/articles/{article_id}")
async def get_article(article_id: int, user: CurrentUser, db: DbSession):
    article = await db.get(Article, article_id)
    if not article or article.user_id != user.id:
        raise NotFoundError("文章不存在")

    return {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "summary": article.summary,
        "has_text": bool(article.text),
        "processed_at": article.processed_at.strftime("%Y-%m-%d %H:%M") if article.processed_at else "",
    }


@router.delete("/articles/{article_id}", response_model=Ok)
async def delete_article(article_id: int, user: CurrentUser, db: DbSession):
    article = await db.get(Article, article_id)
    if not article or article.user_id != user.id:
        raise NotFoundError("文章不存在")

    await db.delete(article)

    from app.services.retrieval import clear_kb_cache

    clear_kb_cache(user.id)
    return Ok(message="已删除")


# ═══════════════════════════════════════════
# 用量
# ═══════════════════════════════════════════

@router.get("/usage")
async def usage(user: CurrentUser, db: DbSession):
    """当前用户本月用量与额度"""
    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)
    quota = user.asr_quota_sec or default_quota
    used = get_asr_usage(user.id)

    video_count = (
        await db.execute(select(func.count(Video.id)).where(Video.user_id == user.id))
    ).scalar_one()
    article_count = (
        await db.execute(select(func.count(Article.id)).where(Article.user_id == user.id))
    ).scalar_one()

    return {
        "asr_used_sec": used,
        "asr_quota_sec": quota,
        "asr_remaining_sec": max(0, quota - used) if quota > 0 else -1,
        "video_count": video_count,
        "article_count": article_count,
    }
