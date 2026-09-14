"""管理端 — 数据管理与系统监控"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import CurrentAdmin, DbSession, PageParams
from app.api.schemas import Ok
from app.core.constants import JOB_TYPE_LABELS, JobStatus
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.progress import current_month, get_asr_usage, get_user_active
from app.core.queue import queue_stats, requeue_failed
from app.db.models import ASRTask, Article, Chat, Job, UsageMonthly, User, Video

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["管理-数据"])


# ═══════════════════════════════════════════
# 概览
# ═══════════════════════════════════════════

@router.get("/overview")
async def overview(admin: CurrentAdmin, db: DbSession):
    """系统概览，管理端首页用"""
    user_total = (await db.execute(select(func.count(User.id)))).scalar_one()
    user_active = (
        await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))
    ).scalar_one()
    video_total = (await db.execute(select(func.count(Video.id)))).scalar_one()
    article_total = (await db.execute(select(func.count(Article.id)))).scalar_one()

    job_running = (
        await db.execute(
            select(func.count(Job.id)).where(Job.status == JobStatus.RUNNING)
        )
    ).scalar_one()
    job_pending = (
        await db.execute(
            select(func.count(Job.id)).where(Job.status == JobStatus.PENDING)
        )
    ).scalar_one()
    job_error = (
        await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.ERROR))
    ).scalar_one()

    # 本月 ASR 用量合计
    ym = current_month()
    month_usage = (
        await db.execute(
            select(func.coalesce(func.sum(UsageMonthly.asr_seconds), 0)).where(
                UsageMonthly.year_month == ym
            )
        )
    ).scalar_one()

    asr_summary = (
        await db.execute(
            select(
                func.count(ASRTask.id),
                func.coalesce(func.sum(ASRTask.audio_duration_sec), 0),
            ).where(ASRTask.status == "success")
        )
    ).one()

    from app.core.config import settings
    from app.db.settings_repo import get_setting

    quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)

    return {
        "users": {"total": user_total, "active": user_active},
        "content": {"videos": video_total, "articles": article_total},
        "jobs": {"running": job_running, "pending": job_pending, "error": job_error},
        "asr": {
            "month": ym,
            "used_sec": int(month_usage),
            "quota_sec": quota,
            "usage_percent": round(month_usage / quota * 100, 1) if quota else 0,
            "total_count": asr_summary[0],
            "total_sec": int(asr_summary[1]),
        },
        "queue": queue_stats(),
    }


# ═══════════════════════════════════════════
# 内容数据管理
# ═══════════════════════════════════════════

@router.get("/videos")
async def admin_list_videos(admin: CurrentAdmin, db: DbSession, page: PageParams, user_id: int = 0):
    stmt = select(Video, User.username).join(User, Video.user_id == User.id)
    count_stmt = select(func.count(Video.id))

    conds = []
    if user_id:
        conds.append(Video.user_id == user_id)
    if page.search:
        like = f"%{page.search}%"
        conds.append(Video.title.ilike(like) | Video.uploader.ilike(like))

    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(Video.processed_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).all()

    items = [
        {
            "id": v.id,
            "vid": v.vid,
            "title": v.title,
            "uploader": v.uploader,
            "platform": v.platform,
            "duration_str": v.duration_str,
            "username": username,
            "user_id": v.user_id,
            "has_summary": bool(v.summary),
            "transcript_source": v.transcript_source,
            "summary_len": len(v.summary or ""),
            "processed_at": v.processed_at.strftime("%Y-%m-%d %H:%M") if v.processed_at else "",
        }
        for v, username in rows[: page.page_size]
    ]

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": len(rows) > page.page_size,
    }


@router.get("/videos/{video_id}")
async def admin_get_video(video_id: int, admin: CurrentAdmin, db: DbSession):
    """管理端查看任意视频详情（含原文转写，便于排查问题）"""
    video = await db.get(Video, video_id)
    if not video:
        raise NotFoundError("视频不存在")

    owner = await db.get(User, video.user_id)

    # 封面：给前端一个可直接 <img> 的临时地址（详情弹窗要展示）。
    # 同时给缩略图地址，弹窗里只显示 136px，没必要拉 130~210KB 的原图。
    thumb, thumb_small = "", ""
    try:
        from app.services.cos_service import get_cos

        cos = await get_cos(db)
        thumb, thumb_small = cos.display_urls(video.thumbnail_key, expires=3600)
    except Exception as exc:
        logger.warning("生成封面地址失败: %s", exc)

    return {
        "id": video.id,
        "vid": video.vid,
        "url": video.url,
        "title": video.title,
        "uploader": video.uploader,
        "platform": video.platform,
        "duration_str": video.duration_str,
        "owner": owner.username if owner else "已删除",
        "summary": video.summary,
        "transcript": video.transcript,
        "transcript_source": video.transcript_source,
        "thumbnail": thumb,
        "thumbnail_small": thumb_small,
        "thumbnail_key": video.thumbnail_key,
        "reused": bool(video.copied_from_video_id),
        "processed_at": video.processed_at.strftime("%Y-%m-%d %H:%M") if video.processed_at else "",
    }


@router.delete("/videos/{video_id}", response_model=Ok)
async def admin_delete_video(video_id: int, admin: CurrentAdmin, db: DbSession):
    from app.services.retrieval import clear_kb_cache, clear_rag_cache

    video = await db.get(Video, video_id)
    if not video:
        raise NotFoundError("视频不存在")

    if video.thumbnail_key:
        try:
            from app.services.cos_service import get_cos

            cos = await get_cos(db)
            cos.delete(video.thumbnail_key)
        except Exception as exc:
            logger.warning("删除封面失败: %s", exc)

    user_id, vid = video.user_id, video.vid
    await db.delete(video)

    clear_rag_cache(user_id, vid)
    clear_kb_cache(user_id)

    logger.info("管理员 %s 删除视频 %s", admin.username, vid)
    return Ok(message="已删除")


@router.get("/articles")
async def admin_list_articles(admin: CurrentAdmin, db: DbSession, page: PageParams, user_id: int = 0):
    stmt = select(Article, User.username).join(User, Article.user_id == User.id)
    count_stmt = select(func.count(Article.id))

    conds = []
    if user_id:
        conds.append(Article.user_id == user_id)
    if page.search:
        conds.append(Article.title.ilike(f"%{page.search}%"))

    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(Article.processed_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).all()

    items = [
        {
            "id": a.id,
            "title": a.title,
            "url": a.url,
            "username": username,
            "user_id": a.user_id,
            "summary_len": len(a.summary or ""),
            "processed_at": a.processed_at.strftime("%Y-%m-%d %H:%M") if a.processed_at else "",
        }
        for a, username in rows[: page.page_size]
    ]

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": len(rows) > page.page_size,
    }


@router.delete("/articles/{article_id}", response_model=Ok)
async def admin_delete_article(article_id: int, admin: CurrentAdmin, db: DbSession):
    from app.services.retrieval import clear_kb_cache

    article = await db.get(Article, article_id)
    if not article:
        raise NotFoundError("文章不存在")

    user_id = article.user_id
    await db.delete(article)
    clear_kb_cache(user_id)

    return Ok(message="已删除")


# ═══════════════════════════════════════════
# 任务管理
# ═══════════════════════════════════════════

@router.get("/jobs")
async def admin_list_jobs(admin: CurrentAdmin, db: DbSession, page: PageParams, status: str = ""):
    stmt = select(Job, User.username).join(User, Job.user_id == User.id)
    count_stmt = select(func.count(Job.id))

    if status:
        stmt = stmt.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(Job.created_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).all()

    items = [
        {
            "id": j.id,
            "type": j.type,
            "type_label": JOB_TYPE_LABELS.get(j.type, j.type),
            "status": j.status,
            "stage": j.stage,
            "progress": j.progress,
            "username": username,
            "user_id": j.user_id,
            "error": j.error,
            "video_id": j.video_id,
            "article_id": j.article_id,
            "created_at": j.created_at.strftime("%m-%d %H:%M") if j.created_at else "",
            "finished_at": j.finished_at.strftime("%m-%d %H:%M") if j.finished_at else "",
        }
        for j, username in rows[: page.page_size]
    ]

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": len(rows) > page.page_size,
    }


@router.delete("/jobs/{job_id}", response_model=Ok)
async def admin_delete_job(job_id: int, admin: CurrentAdmin, db: DbSession):
    job = await db.get(Job, job_id)
    if not job:
        raise NotFoundError("任务不存在")
    await db.delete(job)
    return Ok(message="已删除")


@router.post("/queue/requeue", response_model=Ok)
async def admin_requeue(admin: CurrentAdmin):
    count = requeue_failed(limit=20)
    return Ok(message=f"已重排 {count} 个失败任务")


# ═══════════════════════════════════════════
# ASR 任务与用量
# ═══════════════════════════════════════════

@router.get("/asr-tasks")
async def admin_list_asr(admin: CurrentAdmin, db: DbSession, page: PageParams):
    rows = (
        await db.execute(
            select(ASRTask, User.username)
            .join(User, ASRTask.user_id == User.id)
            .order_by(ASRTask.created_at.desc())
            .limit(page.page_size)
            .offset(page.offset)
        )
    ).all()

    total = (await db.execute(select(func.count(ASRTask.id)))).scalar_one()

    return {
        "items": [
            {
                "id": t.id,
                "tencent_task_id": t.tencent_task_id,
                "status": t.status,
                "audio_duration_sec": t.audio_duration_sec,
                "duration_str": _fmt_duration(t.audio_duration_sec),
                "engine": t.engine,
                "username": username,
                "error": t.error,
                "cos_audio_key": t.cos_audio_key,
                "created_at": t.created_at.strftime("%m-%d %H:%M") if t.created_at else "",
                "finished_at": t.finished_at.strftime("%m-%d %H:%M") if t.finished_at else "",
            }
            for t, username in rows
        ],
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": total > page.offset + len(rows),
    }


@router.get("/usage")
async def admin_usage(admin: CurrentAdmin, db: DbSession):
    """各用户本月用量明细"""
    ym = current_month()

    rows = (
        await db.execute(
            select(UsageMonthly, User.username)
            .join(User, UsageMonthly.user_id == User.id)
            .where(UsageMonthly.year_month == ym)
            .order_by(UsageMonthly.asr_seconds.desc())
        )
    ).all()

    from app.db.settings_repo import get_setting

    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)

    items = []
    for row, username in rows:
        user = await db.get(User, row.user_id)
        quota = (user.asr_quota_sec or default_quota) if user else default_quota

        items.append({
            "user_id": row.user_id,
            "username": username,
            "asr_seconds": row.asr_seconds,
            "asr_seconds_live": get_asr_usage(row.user_id, ym),
            "asr_duration_str": _fmt_duration(row.asr_seconds),
            "asr_count": row.asr_count,
            "summary_count": row.summary_count,
            "ai_tokens": row.ai_tokens,
            "quota_sec": quota,
            "quota_str": _fmt_duration(quota),
            "usage_percent": round(row.asr_seconds / quota * 100, 1) if quota else 0,
        })

    return {"year_month": ym, "items": items, "default_quota_sec": default_quota}


@router.get("/users/{user_id}/usage")
async def admin_user_usage(user_id: int, admin: CurrentAdmin, db: DbSession):
    """某用户的历史用量（近 6 个月）"""
    rows = (
        await db.execute(
            select(UsageMonthly)
            .where(UsageMonthly.user_id == user_id)
            .order_by(UsageMonthly.year_month.desc())
            .limit(6)
        )
    ).scalars().all()

    return {
        "items": [
            {
                "year_month": r.year_month,
                "asr_seconds": r.asr_seconds,
                "asr_duration_str": _fmt_duration(r.asr_seconds),
                "asr_count": r.asr_count,
                "summary_count": r.summary_count,
                "ai_tokens": r.ai_tokens,
            }
            for r in rows
        ]
    }


@router.get("/online")
async def admin_online(admin: CurrentAdmin, db: DbSession):
    """当前正在执行任务的用户"""
    rows = (
        await db.execute(
            select(Job, User.username)
            .join(User, Job.user_id == User.id)
            .where(Job.status.in_([JobStatus.RUNNING, JobStatus.PENDING]))
        )
    ).all()

    seen = {}
    for job, username in rows:
        if job.user_id not in seen:
            seen[job.user_id] = {
                "user_id": job.user_id,
                "username": username,
                "active_slots": get_user_active(job.user_id),
                "jobs": [],
            }
        seen[job.user_id]["jobs"].append({
            "id": job.id,
            "type": JOB_TYPE_LABELS.get(job.type, job.type),
            "status": job.status,
            "stage": job.stage,
        })

    return {"items": list(seen.values())}


@router.post("/maintenance/cleanup-asr-audio", response_model=Ok)
async def cleanup_orphan_audio(admin: CurrentAdmin, db: DbSession):
    """
    清理 COS 中残留的音频中转文件。
    正常情况下识别完成即删除，这里是兜底（例如 Worker 被强杀）。
    """
    from app.services.cos_service import get_cos

    rows = (
        await db.execute(
            select(ASRTask).where(ASRTask.cos_audio_key != "").limit(500)
        )
    ).scalars().all()

    keys = [r.cos_audio_key for r in rows if r.status in ("success", "failed")]
    if not keys:
        return Ok(message="没有需要清理的文件")

    try:
        cos = await get_cos(db)
        count = cos.delete_many(keys)
        for r in rows:
            if r.status in ("success", "failed"):
                r.cos_audio_key = ""
        await db.flush()
        logger.info("管理员 %s 清理残留音频 %d 个", admin.username, count)
        return Ok(message=f"已清理 {count} 个残留音频文件")
    except Exception as exc:
        return Ok(ok=False, message=f"清理失败：{exc}")


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return "0 分钟"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m} 分 {s} 秒" if s else f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"
