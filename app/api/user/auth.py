"""用户端认证 — 登录、注册（邀请制）、修改密码、会话信息"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import ChangePasswordReq, LoginReq, LoginResp, RegisterReq, Ok
from app.core.exceptions import AuthError, ConflictError, PermissionError_
from app.core.logging import get_logger
from app.core.progress import get_asr_usage
from app.core.ratelimit import client_ip
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
    verify_password_lenient,
)
from app.db.models import InviteCode, User
from app.db.settings_repo import get_setting, is_enabled

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["认证"])


def _user_dict(user: User, asr_used: int = 0, asr_quota: int = 0) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "is_active": user.is_active,
        "has_deepseek_key": bool(user.deepseek_key_enc),
        "has_bili_cookie": bool(user.bili_cookie_enc),
        "can_use_own_key": user.can_use_own_key,
        "asr_used_sec": asr_used,
        "asr_quota_sec": asr_quota or user.asr_quota_sec,
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


@router.post("/login", response_model=LoginResp)
async def login(req: LoginReq, request: Request, db: DbSession):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password_lenient(req.password, user.password_hash):
        # 失败必须留痕：此前登录失败没有任何日志，只能靠猜
        # （不记录密码内容，只记长度与首尾空白，足以定位「多了空格」这类问题）
        pwd = req.password or ""
        logger.warning(
            "登录失败: 用户名=%r 来源IP=%s 用户存在=%s 密码长度=%d 首尾含空白=%s",
            req.username, client_ip(request), bool(user), len(pwd), pwd != pwd.strip(),
        )
        raise AuthError("用户名或密码错误（请确认密码首尾没有多余空格）")
    if not user.is_active:
        raise PermissionError_("账号已被禁用，请联系管理员")

    user.last_login_at = datetime.now(timezone.utc)

    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)
    quota = user.asr_quota_sec or default_quota
    used = get_asr_usage(user.id)

    token = create_access_token(user.id, user.role)
    logger.info("用户登录: %s", user.username)

    return LoginResp(token=token, user=_user_dict(user, used, quota))


@router.post("/register", response_model=LoginResp)
async def register(req: RegisterReq, db: DbSession):
    """邀请码注册。管理员可关闭注册开关。"""
    if not await is_enabled(db, "registration_open", True):
        raise PermissionError_("当前未开放注册，请联系管理员")

    # 用户名查重
    exists = (await db.execute(select(User).where(User.username == req.username))).scalar_one_or_none()
    if exists:
        raise ConflictError("用户名已被占用")

    # 邀请码校验
    code_row = (
        await db.execute(select(InviteCode).where(InviteCode.code == req.invite_code))
    ).scalar_one_or_none()

    if not code_row:
        raise AuthError("邀请码无效")

    now = datetime.now(timezone.utc)
    if code_row.expires_at and code_row.expires_at.replace(tzinfo=timezone.utc) < now:
        raise AuthError("邀请码已过期")
    if code_row.used_count >= code_row.max_uses:
        raise AuthError("邀请码已用尽")

    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name or req.username,
        role="member",
    )
    db.add(user)
    await db.flush()

    code_row.used_count += 1
    code_row.used_by = user.id

    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)
    token = create_access_token(user.id, user.role)
    logger.info("新用户注册: %s", user.username)

    return LoginResp(token=token, user=_user_dict(user, 0, default_quota))


@router.get("/me")
async def me(user: CurrentUser, db: DbSession):
    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)
    quota = user.asr_quota_sec or default_quota
    used = get_asr_usage(user.id)

    notice = await get_setting(db, "site_notice", "")
    return {
        "user": _user_dict(user, used, quota),
        "notice": notice,
    }


@router.post("/password", response_model=Ok)
async def change_password(req: ChangePasswordReq, user: CurrentUser, db: DbSession):
    # 同样走宽松校验：若登录时靠去空白才通过，改密码时也必须能通过，
    # 否则用户会被卡在「能登录但不能改密码」的死角
    if not verify_password_lenient(req.old_password, user.password_hash):
        raise AuthError("原密码错误")

    user.password_hash = hash_password(req.new_password)
    logger.info("用户修改密码: %s", user.username)
    return Ok(message="密码已更新")
