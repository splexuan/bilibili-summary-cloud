"""
应用入口 — FastAPI 实例、路由注册、异常处理、启动初始化。

启动命令：
    开发：uvicorn app.main:app --reload --port 8000
    生产：uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.logging import get_logger, setup_logging
from app.db.session import engine
from app.db.models import Base

logger = get_logger(__name__)


# ═══════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("启动 %s v%s（%s）", settings.app_name, __version__, settings.app_env)

    # 开发环境自动建表；生产环境应使用 Alembic 迁移
    if not settings.is_production:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # 初始化默认配置与管理员账号
    try:
        await _bootstrap()
    except Exception as exc:
        logger.exception("初始化失败: %s", exc)

    _warn_region_mismatch()

    yield

    await engine.dispose()
    logger.info("应用已关闭")


async def _bootstrap() -> None:
    """
    首次启动：写入默认配置项、创建管理员账号。

    必须可重复执行（每次容器启动都会调用）：
    - 配置项只补缺失的，不覆盖已有值
    - 布尔开关并入 SETTING_DEFS 一起处理，避免重复插入同 key
    """
    from sqlalchemy import select

    from app.core.security import encrypt_secret, hash_password
    from app.db.models import InviteCode, SystemSetting, User
    from app.db.session import session_scope
    from app.db.settings_repo import SETTING_DEFS

    async with session_scope() as db:
        # 一次性读出已有 key，避免逐条查询 + 未 flush 导致的重名校验失效
        existing_keys = set(
            (await db.execute(select(SystemSetting.key))).scalars().all()
        )

        # 布尔开关与其默认值，统一并入配置定义表处理
        bool_defaults = {"enable_asr": "true", "registration_open": "true"}

        for d in SETTING_DEFS:
            key = d["key"]
            if key in existing_keys:
                continue

            # 环境变量有值的，首次写入库，方便管理员在后台看到并修改
            default = ""
            if d["env"]:
                val = getattr(settings, d["env"], "")
                default = "" if val is None else str(val)
            if not default and key in bool_defaults:
                default = bool_defaults[key]

            is_secret = bool(d["secret"])
            db.add(SystemSetting(
                key=key,
                value=encrypt_secret(default) if (is_secret and default) else default,
                is_encrypted=is_secret,
                is_secret=is_secret,
                description=d["desc"],
            ))
            existing_keys.add(key)

        # 兜底：SETTING_DEFS 未声明的开关也补上
        for key, val in bool_defaults.items():
            if key in existing_keys:
                continue
            db.add(SystemSetting(key=key, value=val, description="开关"))
            existing_keys.add(key)

        # 管理员账号
        admin = (
            await db.execute(select(User).where(User.username == settings.admin_username))
        ).scalar_one_or_none()

        if admin is None:
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                display_name="管理员",
                role="admin",
            )
            db.add(admin)
            await db.flush()

            # 首个管理员默认一个邀请码
            db.add(InviteCode(
                code="WELCOME2026",
                created_by=admin.id,
                max_uses=5,
                note="初始邀请码，请尽快修改",
            ))
            logger.warning(
                "已创建管理员账号：%s / %s —— 请立即修改密码！",
                settings.admin_username, settings.admin_password,
            )


def _warn_region_mismatch() -> None:
    """启动时提醒 COS 与 ASR 地域一致性（跨地域会产生外网流量费）"""
    if settings.cos_region and settings.asr_region:
        if settings.cos_region != settings.asr_region:
            logger.warning(
                "COS 地域（%s）与 ASR 地域（%s）不一致，"
                "ASR 拉取音频可能产生外网下行流量费用，建议调整为同一地域",
                settings.cos_region, settings.asr_region,
            )


# ═══════════════════════════════════════════
# 应用实例
# ═══════════════════════════════════════════

app = FastAPI(
    title="B站视频总结工具 · 云端版",
    description="B站/YouTube 视频与文章 AI 总结，含 RAG 知识库问答",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
)

# 生产环境前后端同源部署，无需 CORS；开发环境放开
if not settings.is_production:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ═══════════════════════════════════════════
# 异常处理
# ═══════════════════════════════════════════

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务器内部错误，请稍后重试",
            "detail": str(exc) if not settings.is_production else None,
        },
    )


# ═══════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════

from app.api.admin import data as admin_data  # noqa: E402
from app.api.admin import settings as admin_settings  # noqa: E402
from app.api.admin import users as admin_users  # noqa: E402
from app.api.user import auth, chat, content, settings as user_settings  # noqa: E402

app.include_router(auth.router)
app.include_router(content.router)
app.include_router(chat.router)
app.include_router(user_settings.router)

app.include_router(admin_settings.router)
app.include_router(admin_users.router)
app.include_router(admin_data.router)


@app.get("/api/health")
async def health():
    """健康检查（供容器编排与监控使用）"""
    from app.core.progress import health_check as redis_check

    redis_ok, redis_msg = await _async_wrap(redis_check)

    db_ok, db_msg = True, "数据库连接正常"
    try:
        from sqlalchemy import text

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok, db_msg = False, f"数据库连接失败：{exc}"

    return {
        "status": "ok" if (redis_ok and db_ok) else "degraded",
        "version": __version__,
        "env": settings.app_env,
        "checks": {
            "database": {"ok": db_ok, "message": db_msg},
            "redis": {"ok": redis_ok, "message": redis_msg},
        },
    }


async def _async_wrap(fn):
    import asyncio

    return await asyncio.to_thread(fn)


@app.get("/api/config/public")
async def public_config():
    """前端启动时需要的公开配置（不含任何密钥）"""
    return {
        "app_name": settings.app_name,
        "version": __version__,
        "max_tasks_per_user": settings.max_tasks_per_user,
    }


# ═══════════════════════════════════════════
# 静态页面
# ═══════════════════════════════════════════

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(settings.template_dir / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(settings.template_dir / "login.html")


@app.get("/knowledge", include_in_schema=False)
async def knowledge_page():
    return FileResponse(settings.template_dir / "knowledge.html")


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(settings.template_dir / "admin.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon = settings.static_dir / "favicon.svg"
    if icon.exists():
        return FileResponse(icon)
    return RedirectResponse(url="/static/app.png")


settings.static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
