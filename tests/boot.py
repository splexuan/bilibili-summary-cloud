"""验证 python -m app.bootstrap 能独立跑通（Docker 入口依赖它）"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="bsum_boot_"))
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP / 'boot.db'}"
os.environ["SECRET_KEY"] = "uL9k2YqWm3nR8tVx5zA7bC1dE4fG6hJ0kL2mN4oP6qR="
os.environ["JWT_SECRET"] = "bootstrap_test_secret_long_enough_hs256"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test123456"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
logging.disable(logging.CRITICAL)


async def _create():
    from app.db.models import Base
    from app.db.session import engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

asyncio.run(_create())

from app.bootstrap import create_tables, wait_for_db  # noqa: E402
from app.main import _bootstrap  # noqa: E402

print("1) wait_for_db ...")
asyncio.run(wait_for_db(max_tries=3, delay=0.1))
print("   OK")

print("2) create_tables ...")
asyncio.run(create_tables())
print("   OK")

print("3) _bootstrap 第一次 ...")
asyncio.run(_bootstrap())
print("   OK")

print("4) _bootstrap 第二次（幂等性验证，容器重启会重复调用）...")
asyncio.run(_bootstrap())
print("   OK")


async def _verify():
    from sqlalchemy import func, select

    from app.db.models import SystemSetting, User
    from app.db.session import session_scope

    async with session_scope() as db:
        n_users = (await db.execute(select(func.count(User.id)))).scalar_one()
        n_settings = (await db.execute(select(func.count(SystemSetting.key)))).scalar_one()
        admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        return n_users, n_settings, admin is not None, admin.role if admin else None


n_users, n_settings, has_admin, role = asyncio.run(_verify())
print(f"5) users={n_users} settings={n_settings} admin存在={has_admin} role={role}")

ok = (n_users == 1 and has_admin and role == "admin" and n_settings >= 10)
print("\n结果：", "通过" if ok else "失败")
print("   （重启两次后用户仍为 1，说明 bootstrap 幂等）")
sys.exit(0 if ok else 1)
