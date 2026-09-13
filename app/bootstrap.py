"""
Docker 启动脚本 — 等数据库就绪后自动迁移并建表。

用途：容器首次启动或升级时，保证表结构与模型一致，
然后再启动 Web / Worker 进程。

调用方式（在 docker-compose command 中）：
    python -m app.bootstrap
"""
import asyncio
import sys

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.models import Base

logger = get_logger(__name__)


async def wait_for_db(max_tries: int = 30, delay: float = 2.0) -> bool:
    """等待数据库可连接，避免 compose 启动顺序导致的失败"""
    from sqlalchemy import text

    from app.db.session import engine

    for i in range(1, max_tries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("数据库已就绪（第 %d 次尝试）", i)
            return True
        except Exception as exc:
            if i == max_tries:
                logger.error("数据库连接失败，已重试 %d 次：%s", max_tries, exc)
                return False
            if i == 1:
                logger.info("等待数据库启动…")
            await asyncio.sleep(delay)
    return False


async def create_tables() -> None:
    """按模型建表（幂等）"""
    from app.db.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("表结构已同步")


async def main() -> int:
    setup_logging()
    logger.info("初始化 %s（%s）", settings.app_name, settings.app_env)

    if not await wait_for_db():
        return 1

    await create_tables()

    # 复用应用内的 bootstrap：写默认配置 + 建管理员账号
    from app.main import _bootstrap

    await _bootstrap()
    logger.info("初始化完成")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
