"""
任务队列封装（RQ）。

设计：
- Web 层只负责 create_job（写库） + enqueue（入队），立即返回
- Worker 执行真正的重活
- 进度通过 Redis 广播，前端 SSE 订阅
"""
from datetime import datetime, timezone

from redis import Redis
from rq import Queue, Retry

from app.core.config import settings
from app.core.constants import JobStatus
from app.core.logging import get_logger
from app.core.progress import get_redis

logger = get_logger(__name__)

_queue: Queue | None = None


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(
            name=settings.queue_name,
            connection=get_redis(),
            default_timeout=3600,  # 单任务最长 1 小时
        )
    return _queue


def enqueue_summarize(job_id: int, user_id: int, **kwargs) -> str:
    """
    入队一个总结任务。返回 RQ job id。

    注意 1：这里只传 job_id / user_id 等标识，真正的参数从数据库读，
            避免把大文本塞进 Redis。
    注意 2：业务字段必须以 **关键字参数** 传给 enqueue()，且参数名不能叫
            `job_id` —— RQ 的 enqueue 会把名为 job_id 的 kwarg 当成
            RQ 自己的任务 ID（要求 str），撞名就报
            `TypeError: Job ID must be a string, not <class 'int'>`。
            所以这里统一改叫 `task_id`，与 run_summarize_job 的签名保持一致。
    """
    from app.workers.tasks import run_summarize_job

    job = get_queue().enqueue(
        run_summarize_job,
        task_id=job_id,
        user_id=user_id,
        job_timeout=3600,
        result_ttl=3600,
        failure_ttl=86400,
        retry=Retry(max=1, interval=[60]),
    )
    logger.info("任务已入队: rq_id=%s, task_id=%s", job.id, job_id)
    return job.id


def cancel_job(rq_job_id: str) -> bool:
    """尝试取消排队中的任务"""
    if not rq_job_id:
        return False
    try:
        from rq.job import Job

        job = Job.fetch(rq_job_id, connection=get_redis())
        if job.get_status() in ("queued",):
            job.cancel()
            return True
    except Exception as exc:
        logger.warning("取消任务失败 %s: %s", rq_job_id, exc)
    return False


def queue_stats() -> dict:
    """队列概览，供管理端监控"""
    try:
        q = get_queue()
        return {
            "name": q.name,
            "queued": len(q),
            "started": len(q.started_job_registry),
            "failed": len(q.failed_job_registry),
            "finished": len(q.finished_job_registry),
            "deferred": len(q.deferred_job_registry),
        }
    except Exception as exc:
        logger.warning("读取队列状态失败: %s", exc)
        return {"name": settings.queue_name, "error": str(exc)}


def requeue_failed(limit: int = 20) -> int:
    """重排失败任务（管理端操作）"""
    try:
        from rq.job import Job

        q = get_queue()
        count = 0
        for job_id in list(q.failed_job_registry.get_job_ids())[:limit]:
            try:
                job = Job.fetch(job_id, connection=get_redis())
                q.enqueue_job(job)
                q.failed_job_registry.remove(job, delete_job=False)
                count += 1
            except Exception as exc:
                logger.warning("重排任务失败 %s: %s", job_id, exc)
        return count
    except Exception as exc:
        logger.warning("重排失败任务出错: %s", exc)
        return 0
