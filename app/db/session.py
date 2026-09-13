"""
异步数据库会话管理。

用法（FastAPI 依赖注入）：
    async def endpoint(db: AsyncSession = Depends(get_db)):
        ...
"""
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ─── 按后端选择引擎参数 ───
# SQLite：不接受连接池参数，且要允许跨线程
# MySQL：aiomysql 的长连接会被服务端 wait_timeout 掐断，需要 pool_recycle
# Postgres：常规连接池 + pool_pre_ping
_backend = settings.db_backend

if _backend == "sqlite":
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
elif _backend == "mysql":
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,      # 取连接前先探活，防 MySQL 8 小时断连
        pool_recycle=3600,       # 连接复用 1 小时后强制重建
        pool_size=10,
        max_overflow=20,
    )
else:
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

# ─── SQLite 的写锁优化（仅开发用）───
# WAL 模式下读写不互斥；busy_timeout 让并发写等待而不是立刻报 database is locked
if _backend == "sqlite":
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：请求级会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class session_scope:
    """
    Worker / 脚本中的会话上下文：

        async with session_scope() as db:
            ...
    """

    def __init__(self):
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = AsyncSessionLocal()
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        if self._session is None:
            return
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None
