"""管理端 — 用户与邀请码管理"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import CurrentAdmin, DbSession, PageParams
from app.api.schemas import InviteCreateReq, Ok, UserCreateReq, UserUpdateReq
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.progress import get_asr_usage
from app.core.security import generate_invite_code, generate_password, hash_password
from app.db.models import Article, InviteCode, User, Video
from app.db.settings_repo import get_setting

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["管理-用户"])


# ═══════════════════════════════════════════
# 用户列表
# ═══════════════════════════════════════════

@router.get("/users")
async def list_users(admin: CurrentAdmin, db: DbSession, page: PageParams):
    stmt = select(User)
    count_stmt = select(func.count(User.id))

    if page.search:
        like = f"%{page.search}%"
        cond = User.username.ilike(like) | User.display_name.ilike(like)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc())
            .limit(page.page_size + 1)
            .offset(page.offset)
        )
    ).scalars().all()

    default_quota = int(await get_setting(db, "asr_monthly_quota_sec", "36000") or 36000)

    items = []
    for u in rows[: page.page_size]:
        video_count = (
            await db.execute(select(func.count(Video.id)).where(Video.user_id == u.id))
        ).scalar_one()
        article_count = (
            await db.execute(select(func.count(Article.id)).where(Article.user_id == u.id))
        ).scalar_one()

        items.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or u.username,
            "role": u.role,
            "is_active": u.is_active,
            "has_deepseek_key": bool(u.deepseek_key_enc),
            "has_bili_cookie": bool(u.bili_cookie_enc),
            "can_use_own_key": u.can_use_own_key,
            "asr_quota_sec": u.asr_quota_sec or default_quota,
            "asr_used_sec": get_asr_usage(u.id),
            "video_count": video_count,
            "article_count": article_count,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
            "last_login_at": (
                u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else ""
            ),
        })

    return {
        "items": items,
        "page": page.page,
        "page_size": page.page_size,
        "total": total,
        "has_more": len(rows) > page.page_size,
    }


@router.post("/users")
async def create_user(req: UserCreateReq, admin: CurrentAdmin, db: DbSession):
    exists = (
        await db.execute(select(User).where(User.username == req.username))
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("用户名已存在")

    password = req.password or generate_password(12)

    user = User(
        username=req.username,
        password_hash=hash_password(password),
        display_name=req.display_name or req.username,
        role=req.role if req.role in ("admin", "member") else "member",
        asr_quota_sec=req.asr_quota_sec,
    )
    db.add(user)
    await db.flush()

    logger.info("管理员 %s 创建用户 %s", admin.username, req.username)

    return {
        "id": user.id,
        "username": user.username,
        "password": password,
        "message": "用户已创建，请妥善保存初始密码",
    }


@router.put("/users/{user_id}", response_model=Ok)
async def update_user(user_id: int, req: UserUpdateReq, admin: CurrentAdmin, db: DbSession):
    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")

    if req.display_name is not None:
        user.display_name = req.display_name
    if req.is_active is not None:
        if user.id == admin.id and not req.is_active:
            raise ConflictError("不能禁用自己的账号")
        user.is_active = req.is_active
    if req.role is not None and req.role in ("admin", "member"):
        user.role = req.role
    if req.asr_quota_sec is not None:
        user.asr_quota_sec = req.asr_quota_sec
    if req.can_use_own_key is not None:
        user.can_use_own_key = req.can_use_own_key
    if req.reset_password:
        new_pwd = generate_password(12)
        user.password_hash = hash_password(new_pwd)
        await db.flush()
        logger.info("管理员重置用户 %s 密码", user.username)
        return Ok(message=f"密码已重置为：{new_pwd}")

    await db.flush()
    return Ok(message="已更新")


@router.delete("/users/{user_id}", response_model=Ok)
async def delete_user(user_id: int, admin: CurrentAdmin, db: DbSession):
    if user_id == admin.id:
        raise ConflictError("不能删除自己的账号")

    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")

    # 清理该用户的 COS 文件
    from app.db.models import Video as V

    rows = (await db.execute(select(V).where(V.user_id == user_id))).scalars().all()
    thumb_keys = [v.thumbnail_key for v in rows if v.thumbnail_key]

    if thumb_keys:
        try:
            from app.services.cos_service import get_cos

            cos = await get_cos(db)
            cos.delete_many(thumb_keys)
        except Exception as exc:
            logger.warning("清理用户 COS 文件失败: %s", exc)

    username = user.username
    await db.delete(user)  # 级联删除 videos / articles / chats / jobs

    logger.info("管理员 %s 删除用户 %s", admin.username, username)
    return Ok(message=f"用户 {username} 及其数据已删除")


@router.post("/users/{user_id}/reset-usage", response_model=Ok)
async def reset_user_usage(user_id: int, admin: CurrentAdmin, db: DbSession):
    """重置某用户本月的 ASR 用量统计（例如管理员手动代付后归零）"""
    from app.core.progress import reset_usage

    user = await db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")

    reset_usage(user_id)
    logger.info("管理员 %s 重置用户 %s 用量", admin.username, user.username)
    return Ok(message="用量已重置")


# ═══════════════════════════════════════════
# 邀请码
# ═══════════════════════════════════════════

@router.get("/invites")
async def list_invites(admin: CurrentAdmin, db: DbSession):
    rows = (
        await db.execute(select(InviteCode).order_by(InviteCode.created_at.desc()).limit(200))
    ).scalars().all()

    now = datetime.now(timezone.utc)
    return {
        "items": [
            {
                "id": c.id,
                "code": c.code,
                "max_uses": c.max_uses,
                "used_count": c.used_count,
                "note": c.note,
                "expires_at": c.expires_at.strftime("%Y-%m-%d %H:%M") if c.expires_at else "",
                "expired": bool(c.expires_at and c.expires_at.replace(tzinfo=timezone.utc) < now),
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "",
            }
            for c in rows
        ]
    }


@router.post("/invites")
async def create_invite(req: InviteCreateReq, admin: CurrentAdmin, db: DbSession):
    code = generate_invite_code(16)
    row = InviteCode(
        code=code,
        created_by=admin.id,
        max_uses=req.max_uses,
        note=req.note,
        expires_at=datetime.now(timezone.utc) + timedelta(days=req.expires_days),
    )
    db.add(row)
    await db.flush()

    logger.info("管理员 %s 生成邀请码 %s", admin.username, code)
    return {"id": row.id, "code": code}


@router.delete("/invites/{invite_id}", response_model=Ok)
async def delete_invite(invite_id: int, admin: CurrentAdmin, db: DbSession):
    row = await db.get(InviteCode, invite_id)
    if not row:
        raise NotFoundError("邀请码不存在")
    await db.delete(row)
    return Ok(message="已删除")
