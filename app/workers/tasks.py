"""
任务流水线 — 视频/文章总结的完整执行逻辑。

视频流程（核心）：
    解析信息 → 尝试字幕 → 有字幕则直接用
                        ↓ 无字幕
                    下载音频 → 转码 → 上传 COS → 提交 ASR → 轮询 → 落库 → 删音频
    → AI 总结（流式进度）→ 保存 → 更新索引

所有函数都是 async，在 RQ 同步 Worker 中通过 asyncio.run 驱动。
"""
import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.constants import ASRStatus, JobStage, JobStatus, JobType, TranscriptSource
from app.core.cookies import to_netscape
from app.core.exceptions import (
    AIError,
    AppError,
    ASRError,
    ConfigError,
    COSError,
    DownloadError,
    QuotaExceededError,
    SubtitleUnavailable,
)
from app.core.logging import get_logger
from app.core.titling import generate_title, is_placeholder_title
from app.core.progress import (
    add_asr_usage,
    append_summary_chunk,
    clear_summary,
    get_asr_usage,
    mark_done,
    mark_error,
    release_user_slot,
    set_progress,
)
from app.db.models import ASRTask, Article, Job, User, Video
from app.db.session import session_scope
from app.db.settings_repo import (
    get_deepseek_config,
    get_setting,
    get_settings_map,
    is_enabled,
)
from app.services.asr_service import get_asr
from app.services.cos_service import get_cos
from app.services.downloader import (
    SUBTITLE_PLATFORMS,
    YtDlpRunner,
    audio_duration,
    find_ffmpeg,
    is_blocking_reason,
    transcode_for_asr,
    validate_vid,
)
from app.services.retrieval import clear_kb_cache, clear_rag_cache
from app.services.summarizer import DeepSeekClient, summarize_stream_collect

logger = get_logger(__name__)

# 字幕提取失败后的重试间隔（秒）。比直接下载整个音频划算得多：
# 一次音频下载 + ASR 要花额度、时间和 COS 流量，重试字幕只要几十秒。
SUBTITLE_RETRY_DELAY = 3


# ═══════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════

def run_summarize_job(task_id: int, user_id: int) -> None:
    """
    RQ 任务入口（同步函数，内部驱动 async）。

    参数名用 task_id 而不是 job_id：
    RQ 的 enqueue 会把名为 job_id 的关键字参数当作自己的任务 ID（必须 str），
    业务主键是 int，撞名会直接抛 TypeError。
    """
    logger.info("开始执行任务 task_id=%s user_id=%s", task_id, user_id)
    try:
        asyncio.run(_run_summarize_job(task_id, user_id))
    except Exception as exc:
        logger.exception("任务执行异常 task_id=%s", task_id)
        try:
            asyncio.run(_finalize_error(task_id, user_id, str(exc)))
        except Exception:
            logger.exception("写入失败状态时再次出错 task_id=%s", task_id)
    finally:
        release_user_slot(user_id)


async def _finalize_error(job_id: int, user_id: int, message: str) -> None:
    mark_error(job_id, message)
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if job:
            job.status = JobStatus.ERROR
            job.error = message[:2000]
            job.finished_at = datetime.now(timezone.utc)


# ═══════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════

async def _run_summarize_job(job_id: int, user_id: int) -> None:
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if not job:
            logger.error("任务不存在: %s", job_id)
            return

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        await db.flush()

        params = json.loads(job.params or "{}")

    try:
        if job.type == JobType.RE_SUMMARIZE:
            await _process_resummarize(job_id, user_id)
        elif job.type == JobType.SUMMARIZE_ARTICLE:
            await _process_article(job_id, user_id)
        else:
            await _process_video(job_id, user_id, params)
    except AppError as exc:
        logger.warning("任务失败 job_id=%s: %s", job_id, exc.message)
        await _finalize_error(job_id, user_id, exc.message)
        return
    except Exception as exc:
        logger.exception("任务未预期失败 job_id=%s", job_id)
        await _finalize_error(job_id, user_id, f"处理失败：{exc}")
        return

    mark_done(job_id)
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if job:
            job.status = JobStatus.DONE
            job.stage = JobStage.DONE
            job.progress = 100.0
            job.finished_at = datetime.now(timezone.utc)

    # 内容变更 → 清索引缓存
    clear_kb_cache(user_id)
    logger.info("任务完成 job_id=%s", job_id)


# ═══════════════════════════════════════════
# 文章
# ═══════════════════════════════════════════

async def _process_article(job_id: int, user_id: int) -> None:
    set_progress(job_id, JobStage.SUMMARIZING, "正在总结文章…")

    async with session_scope() as db:
        job = await db.get(Job, job_id)
        article = await db.get(Article, job.article_id) if job.article_id else None
        if not article:
            raise AppError("文章记录不存在")

        user = await db.get(User, user_id)
        client = await _build_ai_client(db, user)

        title = article.title or "未命名文章"
        text = article.text or ""
        if not text.strip():
            raise AppError("文章内容为空")

        result = await _run_summary(job_id, client, text, title)

        article.summary = result["summary"]
        article.processed_at = datetime.now(timezone.utc)

        # 仍是占位名 → 提交时没能提取到（用户也没填），总结完成后按本地版
        # 再提取一次（纯字符串处理，不额外调用 AI）
        if is_placeholder_title(article.title):
            better = generate_title(article.text or "", fallback="")
            if better:
                article.title = better

        await _record_ai_usage(db, user_id, result.get("tokens", 0))


async def _process_resummarize(job_id: int, user_id: int) -> None:
    """
    重新总结：复用库里已有的转写 / 正文，只重跑 AI。

    不重新下载、不重新走 ASR —— 换了模型或提示词时，能省下一次
    语音识别的钱和几十秒等待（本地版 resummarize 的等价实现）。
    """
    video_vid = ""

    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if not job:
            raise AppError("任务不存在")

        user = await db.get(User, user_id)
        client = await _build_ai_client(db, user)

        if job.article_id:
            article = await db.get(Article, job.article_id)
            if not article:
                raise AppError("文章记录不存在")
            text = article.text or ""
            if not text.strip():
                raise AppError("文章内容为空，无法重新总结")

            set_progress(job_id, JobStage.SUMMARIZING, "正在重新总结…")
            result = await _run_summary(job_id, client, text, article.title or "未命名文章")

            article.summary = result["summary"]
            article.processed_at = datetime.now(timezone.utc)
            if is_placeholder_title(article.title):
                better = generate_title(article.text or "", fallback="")
                if better:
                    article.title = better
            await _record_ai_usage(db, user_id, result.get("tokens", 0))
            return

        video = await db.get(Video, job.video_id) if job.video_id else None
        if not video:
            raise AppError("视频记录不存在")

        transcript = video.transcript or ""
        if not transcript.strip():
            raise AppError("该视频没有转写内容，请重新提交完整处理")

        set_progress(job_id, JobStage.SUMMARIZING, "正在重新总结…")
        result = await _run_summary(job_id, client, transcript, video.title)

        video.summary = result["summary"]
        video.processed_at = datetime.now(timezone.utc)
        await _record_ai_usage(db, user_id, result.get("tokens", 0))
        video_vid = video.vid

    if video_vid:
        clear_rag_cache(video_vid)


# ═══════════════════════════════════════════
# 视频
# ═══════════════════════════════════════════

async def _process_video(job_id: int, user_id: int, params: dict) -> None:
    url = params.get("url", "").strip()
    if not url:
        raise AppError("缺少视频链接")

    # ─── 1. 解析信息 ───
    set_progress(job_id, JobStage.PARSING)

    async with session_scope() as db:
        job = await db.get(Job, job_id)
        video = await db.get(Video, job.video_id) if job.video_id else None
        user = await db.get(User, user_id)

        runner = YtDlpRunner(
            cookie_file=await _resolve_cookie_file(db, user),
            proxy=await get_setting(db, "http_proxy", ""),
        )

        if video is None:
            # 首次创建
            info = await asyncio.to_thread(runner.fetch_info, url)
            vid = validate_vid(info["vid"])
            video = Video(
                user_id=user_id,
                vid=vid,
                url=info["url"],
                title=info["title"],
                uploader=info["uploader"],
                duration=info["duration"],
                duration_str=info["duration_str"],
                platform=info["platform"],
            )
            db.add(video)
            await db.flush()
            job.video_id = video.id
            await db.flush()
        else:
            info = {
                "vid": video.vid,
                "url": video.url,
                "title": video.title,
                "duration": video.duration,
                "thumbnail": "",
                "platform": video.platform,
            }

        video_id = video.id
        vid = video.vid
        platform = video.platform or "bilibili"
        title = video.title
        duration_sec = _to_int(video.duration)

        # 缓存命中：已有转写与总结
        if video.transcript and video.summary and not params.get("force"):
            set_progress(job_id, JobStage.SUMMARIZING, "已存在总结，跳过处理")
            return

        # ─── 2. 封面 ───
        thumbnail_url = info.get("thumbnail") or ""
        if thumbnail_url and not video.thumbnail_key:
            try:
                data = await asyncio.to_thread(runner.download_thumbnail, thumbnail_url)
                if data:
                    cos = await get_cos(db)
                    key = cos.thumb_key(user_id, vid)
                    await asyncio.to_thread(cos.upload_bytes, key, data, "image/jpeg")
                    video.thumbnail_key = key
            except Exception as exc:
                logger.warning("封面上传失败，忽略: %s", exc)

    # ─── 3. 获取文本：字幕优先，ASR 兜底 ───
    transcript = None
    source = ""

    async with session_scope() as db:
        video = await db.get(Video, video_id)
        if video.transcript:
            transcript = video.transcript
            source = video.transcript_source
            set_progress(job_id, JobStage.FETCHING_SUBTITLE, "使用已有转写内容")
        else:
            set_progress(job_id, JobStage.FETCHING_SUBTITLE)
            user = await db.get(User, user_id)
            runner = YtDlpRunner(
                cookie_file=await _resolve_cookie_file(db, user),
                proxy=await get_setting(db, "http_proxy", ""),
            )
            subtitle = await _try_fetch_subtitle(runner, url, platform, job_id)

            if subtitle and subtitle.strip():
                transcript = subtitle
                source = TranscriptSource.SUBTITLE
                set_progress(job_id, JobStage.FETCHING_SUBTITLE, "已提取到字幕")
            else:
                # 无字幕 → ASR
                enabled = await is_enabled(db, "enable_asr", True)
                if not enabled:
                    raise AppError("该视频没有字幕，且语音识别已被管理员关闭")
                set_progress(
                    job_id, JobStage.DOWNLOADING,
                    "无字幕，需下载音频进行语音识别…",
                )

        if transcript:
            video.transcript = transcript
            video.transcript_source = source
            await db.flush()

    # ─── 4. 走 ASR ───
    if not transcript:
        transcript = await _transcribe_via_asr(
            job_id, user_id, video_id, url, vid, duration_sec, params
        )
        async with session_scope() as db:
            video = await db.get(Video, video_id)
            video.transcript = transcript
            video.transcript_source = TranscriptSource.ASR

    # ─── 5. AI 总结 ───
    set_progress(job_id, JobStage.SUMMARIZING, "正在生成总结…")

    async with session_scope() as db:
        user = await db.get(User, user_id)
        client = await _build_ai_client(db, user)

        result = await _run_summary(job_id, client, transcript, title)

        video = await db.get(Video, video_id)
        video.summary = result["summary"]
        video.processed_at = datetime.now(timezone.utc)
        await _record_ai_usage(db, user_id, result.get("tokens", 0))

    clear_rag_cache(vid)


# ═══════════════════════════════════════════
# ASR 子流程
# ═══════════════════════════════════════════

async def _transcribe_via_asr(
    job_id: int,
    user_id: int,
    video_id: int,
    url: str,
    vid: str,
    duration_sec: int,
    params: dict,
) -> str:
    """下载音频 → 转码 → 上传 COS → 提交识别 → 轮询 → 落库 → 清理"""

    # 额度前置检查
    async with session_scope() as db:
        quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)

    used = get_asr_usage(user_id)
    if quota > 0 and duration_sec and (used + duration_sec) > quota:
        remain_min = max(0, quota - used) // 60
        raise QuotaExceededError(
            f"本月语音识别额度不足（剩余约 {remain_min} 分钟），"
            "该视频需要转写约 "
            f"{duration_sec // 60} 分钟。请等待下月额度重置或联系管理员。"
        )

    with tempfile.TemporaryDirectory(prefix="bsum_job_") as tmp:
        tmp_dir = Path(tmp)

        # ─── 下载 ───
        async with session_scope() as db:
            user = await db.get(User, user_id)
            runner = YtDlpRunner(
                cookie_file=await _resolve_cookie_file(db, user),
                proxy=await get_setting(db, "http_proxy", ""),
            )

        set_progress(job_id, JobStage.DOWNLOADING, "正在下载音频…")

        def on_dl(p: dict) -> None:
            set_progress(
                job_id, JobStage.DOWNLOADING,
                f"下载中 {p.get('percent', 0):.1f}%（{p.get('speed', '')}）",
                percent=JobStage.PROGRESS[JobStage.DOWNLOADING] + min(p.get("percent", 0) / 100 * 15, 15),
            )

        audio_path = await asyncio.to_thread(
            runner.download_audio, url, vid, tmp_dir, on_dl
        )

        # ─── 转码 ───
        set_progress(job_id, JobStage.TRANSCODING, "正在转码音频…")
        await asyncio.to_thread(find_ffmpeg)
        asr_audio = await asyncio.to_thread(
            transcode_for_asr, audio_path, tmp_dir / f"{vid}_asr.m4a"
        )

        real_duration = await asyncio.to_thread(audio_duration, asr_audio)
        if real_duration and real_duration > 5 * 3600:
            raise ASRError(
                f"音频时长 {real_duration / 3600:.1f} 小时超过腾讯云单次上限 5 小时"
            )
        duration_sec = int(real_duration or duration_sec)

        # ─── 上传 COS ───
        set_progress(job_id, JobStage.UPLOADING, "正在上传音频…")
        async with session_scope() as db:
            cos = await get_cos(db)

        cos_key = await asyncio.to_thread(
            cos.upload_file,
            cos.audio_key(user_id, vid, "m4a"),
            asr_audio,
            "audio/mp4",
        )

        asr_row_id = None
        try:
            audio_url = await asyncio.to_thread(cos.presigned_url, cos_key)

            # ─── 提交任务 ───
            set_progress(job_id, JobStage.ASR_SUBMITTING, "正在提交识别任务…")
            async with session_scope() as db:
                asr = await get_asr(db)
                task_id = await asyncio.to_thread(asr.create_task, audio_url, duration_sec)

                row = ASRTask(
                    user_id=user_id,
                    video_id=video_id,
                    tencent_task_id=task_id,
                    status=ASRStatus.WAITING,
                    audio_duration_sec=duration_sec,
                    cos_audio_key=cos_key,
                    engine=asr.engine,
                )
                db.add(row)
                await db.flush()
                asr_row_id = row.id

            # ─── 轮询 ───
            set_progress(job_id, JobStage.ASR_POLLING, "语音识别中，请稍候…")

            def on_poll(elapsed: int, status: str) -> None:
                label = "排队中" if status == ASRStatus.WAITING else "识别中"
                set_progress(
                    job_id, JobStage.ASR_POLLING,
                    f"语音识别{label}…已等待 {elapsed} 秒",
                    percent=min(JobStage.PROGRESS[JobStage.ASR_POLLING] + elapsed / 10, 78),
                )

            async with session_scope() as db:
                asr = await get_asr(db)

            result = await asyncio.to_thread(
                asr.wait_for_result, task_id, on_poll
            )

            # ─── 落库（腾讯云结果仅保留 24 小时，必须立即写入）───
            text = result["text"]
            real_dur = result.get("audio_duration") or duration_sec

            async with session_scope() as db:
                video = await db.get(Video, video_id)
                if video:
                    video.transcript = text
                    video.transcript_source = TranscriptSource.ASR

                if asr_row_id:
                    row = await db.get(ASRTask, asr_row_id)
                    if row:
                        row.status = ASRStatus.SUCCESS
                        row.audio_duration_sec = real_dur
                        row.finished_at = datetime.now(timezone.utc)

            add_asr_usage(user_id, real_dur)
            await _sync_usage_to_db(user_id, real_dur)

            logger.info(
                "ASR 转写完成 video_id=%s，%d 字，音频 %d 秒",
                video_id, len(text), real_dur,
            )
            return text

        finally:
            # ─── 清理 COS 音频（无论成败都删，避免存储堆积）───
            try:
                await asyncio.to_thread(cos.delete, cos_key)
            except Exception as exc:
                logger.warning("清理 COS 音频失败 %s: %s", cos_key, exc)


# ═══════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════

async def _try_fetch_subtitle(
    runner: YtDlpRunner, url: str, platform: str, job_id: int
) -> str | None:
    """
    尝试取字幕。返回字幕文本；没有字幕返回 None（由调用方走 ASR）。

    为什么要分这么细 —— 下载音频 + ASR 是整个流程里最贵的一步
    （额度、时间、COS 流量），不该被一个可重试的网络抖动或一个
    注定失败的场景拖下水：

    - SubtitleUnavailable：确认没字幕 → 直接转 ASR，这是正常分支
    - blocking 类错误（Cookie 失效 / 风控 / 视频已失效 / 地区限制）：
      音频下载必然撞同一堵墙 → 立刻抛出，让用户看到可操作的原因，
      而不是白等一场再收到同样的报错
    - 其他错误（网络抖动等）：重试一次，仍失败才退到 ASR

    全程 --skip-download，不会下载音视频。
    """
    if platform not in SUBTITLE_PLATFORMS:
        logger.info("平台 %s 无字幕接口，直接走 ASR", platform)
        return None

    for attempt in (1, 2):
        try:
            return await asyncio.to_thread(runner.fetch_subtitle, url, platform)
        except SubtitleUnavailable as exc:
            logger.info("未找到字幕，转 ASR：%s", exc)
            return None
        except DownloadError as exc:
            if is_blocking_reason(str(exc)):
                logger.warning("字幕提取遇到无法恢复的错误，不再尝试 ASR：%s", exc)
                raise

            if attempt == 1:
                logger.warning("字幕提取失败，%d 秒后重试：%s", SUBTITLE_RETRY_DELAY, exc)
                set_progress(
                    job_id, JobStage.FETCHING_SUBTITLE, "字幕提取失败，正在重试…"
                )
                await asyncio.sleep(SUBTITLE_RETRY_DELAY)
                continue

            logger.warning("字幕重试仍失败，转 ASR 兜底：%s", exc)
            return None

    return None


async def _run_summary(
    job_id: int,
    client: DeepSeekClient,
    text: str,
    title: str,
) -> dict:
    """
    执行一次 AI 总结（流式）。

    - 进度文案走 set_progress（阶段进度条）
    - 正文增量走 append_summary_chunk（前端实时显字）
    返回 {"summary", "tokens", "mode"}，与旧的 summarize_sync 一致。
    """
    clear_summary(job_id)

    def on_progress(msg: str) -> None:
        set_progress(job_id, JobStage.SUMMARIZING, msg)

    def on_chunk(chunk: str) -> None:
        append_summary_chunk(job_id, chunk)

    return await asyncio.to_thread(
        summarize_stream_collect, client, text, title, on_progress, on_chunk
    )


async def _build_ai_client(db, user: User | None) -> DeepSeekClient:
    """
    AI 客户端优先级：
    1. 用户自带 Key（若允许且已配置）
    2. 管理员配置的全局 Key
    """
    api_url, global_key, model = await get_deepseek_config(db)

    if user and user.deepseek_key_enc and user.can_use_own_key:
        from app.core.security import decrypt_secret

        own_key = decrypt_secret(user.deepseek_key_enc)
        if own_key:
            return DeepSeekClient(own_key, api_url, model)

    if not global_key:
        raise ConfigError(
            "未配置 DeepSeek API Key。请在「设置」中填写你自己的 Key，"
            "或联系管理员在后台配置全局 Key。"
        )
    return DeepSeekClient(global_key, api_url, model)


async def _resolve_cookie_file(db, user: User | None) -> str:
    """
    Cookie 优先级：用户自带 > 全局兜底。
    取到的原始字符串统一转成 Netscape 格式后落盘，供 yt-dlp 使用。
    """
    content = ""
    filename = "global.txt"

    if user and user.bili_cookie_enc:
        from app.core.security import decrypt_secret

        content = decrypt_secret(user.bili_cookie_enc) or ""
        filename = f"user_{user.id}.txt"

    if not content.strip():
        content = await get_setting(db, "global_bili_cookie", "") or ""

    netscape = to_netscape(content)
    if not netscape:
        return ""

    path = settings.cookie_dir / filename
    path.write_text(netscape, encoding="utf-8")
    return str(path)


async def _record_ai_usage(db, user_id: int, tokens: int) -> None:
    if tokens <= 0:
        return
    from app.db.models import UsageMonthly

    ym = time.strftime("%Y-%m")
    row = (
        await db.execute(
            select(UsageMonthly).where(
                UsageMonthly.user_id == user_id,
                UsageMonthly.year_month == ym,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = UsageMonthly(user_id=user_id, year_month=ym, summary_count=0, ai_tokens=0)
        db.add(row)

    # (row.xxx or 0)：新建对象在 flush 前属性仍是 None，
    # 不能直接 +=，否则抛 NoneType 运算错误
    row.summary_count = (row.summary_count or 0) + 1
    row.ai_tokens = (row.ai_tokens or 0) + tokens


async def _sync_usage_to_db(user_id: int, seconds: int) -> None:
    """把 Redis 中的 ASR 实时用量同步到数据库（用于统计与报表）"""
    from app.db.models import UsageMonthly

    ym = time.strftime("%Y-%m")
    async with session_scope() as db:
        row = (
            await db.execute(
                select(UsageMonthly).where(
                    UsageMonthly.user_id == user_id,
                    UsageMonthly.year_month == ym,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            row = UsageMonthly(user_id=user_id, year_month=ym, asr_seconds=0, asr_count=0)
            db.add(row)

        row.asr_seconds = (row.asr_seconds or 0) + seconds
        row.asr_count = (row.asr_count or 0) + 1


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0
