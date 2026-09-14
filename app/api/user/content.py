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
    release_user_slot,
    subscribe,
)
from app.core.queue import cancel_job, enqueue_summarize, get_queue
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
        title = _extract_title(req.text)

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


def _extract_title(text: str, max_len: int = 60) -> str:
    """从正文首行提取标题"""
    for line in text.split("\n")[:5]:
        line = line.strip().lstrip("#").strip()
        if 2 <= len(line) <= max_len:
            return line
    return ""


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

                    if stage in ("done", "error"):
                        yield _sse("end", {"stage": stage})
                        return

                # 检查数据库终态（防止 Redis 事件丢失）
                # 必须用独立会话：请求级 session 的 identity map 会缓存 Job，
                # 读不到 Worker 在其他会话里写入的最新状态
                async with AsyncSessionLocal() as check_db:
                    cur = await check_db.get(Job, job_id)
                if cur and cur.status in (JobStatus.DONE, JobStatus.ERROR):
                    if cur.status == JobStatus.ERROR:
                        yield _sse("error", {"message": cur.error or "任务失败"})
                    else:
                        yield _sse("end", {"stage": "done", "video_id": cur.video_id})
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
        thumb = ""
        if v.thumbnail_key:
            try:
                thumb = cos.public_url(v.thumbnail_key, expires=3600)
            except Exception:
                thumb = ""
        items.append({
            "id": v.id,
            "vid": v.vid,
            "title": v.title,
            "uploader": v.uploader,
            "duration_str": v.duration_str,
            "platform": v.platform,
            "thumbnail": thumb,
            "has_summary": bool(v.summary),
            "transcript_source": v.transcript_source,
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

    thumb = ""
    if video.thumbnail_key:
        try:
            cos = await get_cos(db)
            thumb = cos.public_url(video.thumbnail_key, expires=3600)
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
        "summary": video.summary,
        "has_transcript": bool(video.transcript),
        "transcript_source": video.transcript_source,
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

    clear_rag_cache(video.vid)
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
