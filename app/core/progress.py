"""
任务进度 — 高频进度写 Redis，终态落 Postgres。

设计原因：
进度更新可能每秒数次（下载百分比、轮询心跳），写库开销大。
Redis 存实时进度，前端通过 SSE 订阅；任务结束时把终态写回 jobs 表。
"""
import json
import time
from typing import Any

import redis

from app.core.config import settings
from app.core.constants import JobStage
from app.core.logging import get_logger

logger = get_logger(__name__)

_pool: redis.ConnectionPool | None = None
PROGRESS_TTL = 86400  # 24 小时后自动清理


def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=32,
            socket_keepalive=True,
            health_check_interval=30,
        )
    return redis.Redis(connection_pool=_pool)


def _progress_key(job_id: int) -> str:
    return f"bsum:progress:{job_id}"


def _channel(job_id: int) -> str:
    return f"bsum:channel:{job_id}"


def _user_active_key(user_id: int) -> str:
    return f"bsum:user_active:{user_id}"


# ═══════════════════════════════════════════
# 进度读写
# ═══════════════════════════════════════════

def set_progress(
    job_id: int,
    stage: str,
    message: str = "",
    percent: float | None = None,
    extra: dict | None = None,
) -> None:
    """
    更新任务进度并广播。

    stage: JobStage 常量
    percent: 显式进度 0-100，不传则取该阶段的基准值
    """
    data: dict[str, Any] = {
        "stage": stage,
        "message": message or JobStage.LABELS.get(stage, stage),
        "percent": percent if percent is not None else JobStage.PROGRESS.get(stage, 0),
        "ts": time.time(),
    }
    if extra:
        data["extra"] = extra

    try:
        r = get_redis()
        payload = json.dumps(data, ensure_ascii=False)
        r.setex(_progress_key(job_id), PROGRESS_TTL, payload)
        r.publish(_channel(job_id), payload)
    except Exception as exc:
        # 进度上报失败不应中断任务
        logger.warning("进度上报失败 job=%s: %s", job_id, exc)


def get_progress(job_id: int) -> dict | None:
    try:
        raw = get_redis().get(_progress_key(job_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def push_log(job_id: int, text: str) -> None:
    """推送一条日志型消息（用于 Map-Reduce 分段进度）"""
    set_progress(job_id, "", message=text)


# ═══════════════════════════════════════════
# 流式总结正文缓冲
# ═══════════════════════════════════════════
#
# 为什么不走 set_progress：进度值是「覆盖写」的单个 JSON，
# 而总结正文需要不断追加；且正文可能上万字，塞进进度 JSON 会让
# 每次进度更新都重传全文。这里单独用一个 Redis string 累积，
# SSE 侧按长度增量下发。

def _summary_key(job_id: int) -> str:
    return f"bsum:summary:{job_id}"


def append_summary_chunk(job_id: int, text: str) -> None:
    """追加一段总结正文（Worker 每收到一个流片段调用一次）"""
    if not text:
        return
    try:
        r = get_redis()
        key = _summary_key(job_id)
        r.append(key, text)
        r.expire(key, PROGRESS_TTL)
    except Exception as exc:
        # 实时显字失败不影响任务本身
        logger.warning("总结正文推送失败 job=%s: %s", job_id, exc)


def get_summary(job_id: int) -> str:
    """读取当前已生成的总结正文（供 SSE 增量下发）"""
    try:
        return get_redis().get(_summary_key(job_id)) or ""
    except Exception:
        return ""


def clear_summary(job_id: int) -> None:
    """清空缓冲（任务开始前调用，避免上一轮的正文串场）"""
    try:
        get_redis().delete(_summary_key(job_id))
    except Exception:
        pass


def mark_done(job_id: int, message: str = "完成") -> None:
    set_progress(job_id, JobStage.DONE, message, percent=100)


def mark_error(job_id: int, message: str) -> None:
    set_progress(job_id, JobStage.ERROR, message, percent=100)


# ═══════════════════════════════════════════
# SSE 订阅
# ═══════════════════════════════════════════

def subscribe(job_id: int, timeout: int = 600):
    """
    订阅任务进度事件流（供 SSE 使用，在生成器里迭代）。

    每收到一条事件返回 dict；超时或收到终态则结束迭代。
    """
    r = get_redis()
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(_channel(job_id))

    # 先推一次当前状态（避免订阅前已产生的进度丢失）
    current = get_progress(job_id)
    if current:
        yield current
        if current.get("stage") in (JobStage.DONE, JobStage.ERROR):
            pubsub.close()
            return

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            msg = pubsub.get_message(timeout=1.0)
            if msg is None:
                continue
            try:
                data = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue

            yield data
            if data.get("stage") in (JobStage.DONE, JobStage.ERROR):
                break
    finally:
        try:
            pubsub.unsubscribe(_channel(job_id))
            pubsub.close()
        except Exception:
            pass


# ═══════════════════════════════════════════
# 用户并发控制
# ═══════════════════════════════════════════

def acquire_user_slot(user_id: int, max_slots: int | None = None) -> bool:
    """
    尝试占用用户的执行槽位。返回是否成功。
    用于限制单用户同时进行的任务数，避免一个用户占满 Worker。
    """
    max_slots = max_slots or settings.max_tasks_per_user
    if max_slots <= 0:
        return True

    try:
        r = get_redis()
        key = _user_active_key(user_id)
        current = r.incr(key)
        if current == 1:
            r.expire(key, 7200)  # 兜底过期，防止异常退出导致槽位泄漏
        if current > max_slots:
            r.decr(key)
            return False
        return True
    except Exception as exc:
        logger.warning("并发槽位检查失败，放行: %s", exc)
        return True


def release_user_slot(user_id: int) -> None:
    try:
        r = get_redis()
        key = _user_active_key(user_id)
        if r.exists(key):
            remaining = r.decr(key)
            if remaining <= 0:
                r.delete(key)
    except Exception as exc:
        logger.warning("释放并发槽位失败: %s", exc)


def get_user_active(user_id: int) -> int:
    try:
        val = get_redis().get(_user_active_key(user_id))
        return int(val) if val else 0
    except Exception:
        return 0


# ═══════════════════════════════════════════
# 月度额度统计
# ═══════════════════════════════════════════

def _usage_key(user_id: int, year_month: str) -> str:
    return f"bsum:usage:{user_id}:{year_month}"


def current_month() -> str:
    return time.strftime("%Y-%m")


def add_asr_usage(user_id: int, seconds: int) -> int:
    """
    累加 ASR 用量（秒），返回本月累计值。
    以 Redis 为实时计数源，定期/结束时同步到数据库。
    """
    if seconds <= 0:
        return get_asr_usage(user_id)

    try:
        r = get_redis()
        key = _usage_key(user_id, current_month())
        total = r.incrby(key, seconds)
        r.expire(key, 60 * 60 * 24 * 62)  # 保留两个月
        return int(total)
    except Exception as exc:
        logger.warning("ASR 用量累加失败: %s", exc)
        return 0


def get_asr_usage(user_id: int, year_month: str | None = None) -> int:
    try:
        val = get_redis().get(_usage_key(user_id, year_month or current_month()))
        return int(val) if val else 0
    except Exception:
        return 0


def reset_usage(user_id: int, year_month: str | None = None) -> None:
    try:
        get_redis().delete(_usage_key(user_id, year_month or current_month()))
    except Exception:
        pass


def health_check() -> tuple[bool, str]:
    try:
        get_redis().ping()
        return True, "Redis 连接正常"
    except Exception as exc:
        return False, f"Redis 连接失败：{exc}"
