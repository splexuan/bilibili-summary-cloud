"""
Alembic 迁移环境。

数据库地址从应用配置读取（数据库级配置存库并由 settings_repo 提供），
这里直接用 settings.database_url，保证迁移与应用始终指向同一库。
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 模型元数据 —— autogenerate 的依据
target_metadata = Base.metadata


def _sync_url() -> str:
    """
    Alembic 的 offline 模式用同步驱动，把异步驱动换成对应的同步驱动。
    按后端分别处理，不能只替换 Postgres 那一种。
    """
    url = settings.database_url
    if url.startswith("postgresql"):
        return url.replace("+asyncpg", "+psycopg")
    if url.startswith("mysql") or url.startswith("mariadb"):
        # mysql+aiomysql:// → mysql+pymysql://
        return url.replace("+aiomysql", "+pymysql")
    # sqlite+aiosqlite:// → sqlite://
    if url.startswith("sqlite"):
        return url.replace("+aiosqlite", "")
    return url


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连接数据库"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=settings.database_url,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
