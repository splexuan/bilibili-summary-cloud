"""
RQ Worker 启动入口。

用法：
    python -m app.workers.worker          # 前台运行
    docker compose up worker              # 容器运行

与 Web 服务共用同一镜像，只是启动命令不同。
"""
import os
import sys

from redis import Redis
from rq import Worker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    from app.core.logging import setup_logging

    setup_logging()

    concurrency = max(1, int(settings.worker_concurrency))
    logger.info(
        "启动 Worker：队列=%s 并发=%s Redis=%s",
        settings.queue_name,
        concurrency,
        settings.redis_url,
    )

    conn = Redis.from_url(settings.redis_url, decode_responses=False)

    if concurrency <= 1:
        # 单进程：直接用 Worker
        Worker(
            [settings.queue_name],
            connection=conn,
            log_job_description=True,
        ).work(with_scheduler=True, logging_level="INFO")
    else:
        # 多进程：用 RQ 官方的 worker pool，每个子进程一个 Worker。
        # 注意 RQ 2.12 的 WorkerPool.start() 只接受 burst / logging_level，
        # 没有 with_scheduler 参数；调度器由 pool 内部自行处理。
        # log_job_description / logging_level 通过 **kwargs 透传给子 Worker。
        from rq.worker_pool import WorkerPool

        pool = WorkerPool(
            [settings.queue_name],
            connection=conn,
            num_workers=concurrency,
            log_job_description=True,
            logging_level="INFO",
        )
        pool.start(burst=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Worker 已停止")
        sys.exit(0)
