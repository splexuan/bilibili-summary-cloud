"""
FastAPI 依赖 — 当前用户注入、权限校验、分页参数。
"""
from typing import Annotated

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthError, PermissionError_
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _extract_token(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None, description="SSE 场景通过 query 传 token"),
) -> str:
    """从 Authorization 头或 query 参数取 token（EventSource 无法自定义头）"""
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    if token:
        return token.strip()
    raise AuthError()


async def get_current_user(
    db: DbSession,
    raw_token: Annotated[str, Depends(_extract_token)],
) -> User:
    payload = decode_access_token(raw_token)
    user_id = payload.get("sub")
    if not user_id:
        raise AuthError()

    user = await db.get(User, int(user_id))
    if not user:
        raise AuthError("用户不存在")
    if not user.is_active:
        raise PermissionError_("账号已被禁用，请联系管理员")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise PermissionError_("需要管理员权限")
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


class Pagination:
    def __init__(
        self,
        page: int = Query(1, ge=1, description="页码，从 1 开始"),
        page_size: int = Query(20, ge=1, le=100, description="每页条数"),
        search: str = Query("", description="搜索关键词"),
    ):
        self.page = page
        self.page_size = page_size
        self.search = search.strip()

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


PageParams = Annotated[Pagination, Depends(Pagination)]
