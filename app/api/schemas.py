"""请求 / 响应模型"""
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════
# 通用
# ═══════════════════════════════════════════

class Ok(BaseModel):
    ok: bool = True
    message: str = ""


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    has_more: bool


# ═══════════════════════════════════════════
# 认证
# ═══════════════════════════════════════════

class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RegisterReq(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    invite_code: str = Field(..., min_length=1, max_length=32)
    display_name: str = Field("", max_length=64)


class LoginResp(BaseModel):
    token: str
    user: dict


# ═══════════════════════════════════════════
# 用户设置
# ═══════════════════════════════════════════

class UserSettingsReq(BaseModel):
    deepseek_key: str | None = Field(None, description="留空表示不修改")
    bili_cookie: str | None = Field(None, description="留空表示不修改")
    clear_deepseek_key: bool = False
    clear_bili_cookie: bool = False


class ChangePasswordReq(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


# ═══════════════════════════════════════════
# 内容
# ═══════════════════════════════════════════

class VideoSubmitReq(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    force: bool = Field(False, description="已存在时强制重新处理")


class ArticleSubmitReq(BaseModel):
    text: str = Field(..., min_length=1)
    title: str = Field("", max_length=512)
    url: str = Field("", max_length=2048)


class ChatReq(BaseModel):
    messages: list[dict] = Field(..., min_length=1)
    video_id: int | None = None


class KBChatReq(BaseModel):
    messages: list[dict] = Field(..., min_length=1)


class ChatSaveReq(BaseModel):
    chat_id: int | None = None
    kind: str = "kb"
    video_id: int | None = None
    title: str = ""
    messages: list[dict] = []


# ═══════════════════════════════════════════
# 朗读
# ═══════════════════════════════════════════

class TTSReq(BaseModel):
    text: str = Field("", description="待朗读文本（Markdown 会被自动清理）")
    voice: str = Field("", description="音色，留空用默认")
    rate: str = Field("+0%", description="语速，如 +25%")


# ═══════════════════════════════════════════
# 管理端
# ═══════════════════════════════════════════

class SettingsUpdateReq(BaseModel):
    items: dict[str, str] = Field(default_factory=dict, description="键值对，空值表示不修改")


class UserCreateReq(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field("", description="留空则自动生成")
    display_name: str = Field("", max_length=64)
    role: str = Field("member")
    asr_quota_sec: int = Field(0, ge=0, description="月度额度（秒），0 表示用全局默认")


class UserUpdateReq(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    role: str | None = None
    asr_quota_sec: int | None = Field(None, ge=0)
    can_use_own_key: bool | None = None
    reset_password: bool = False


class InviteCreateReq(BaseModel):
    max_uses: int = Field(1, ge=1, le=100)
    expires_days: int = Field(7, ge=1, le=365)
    note: str = Field("", max_length=128)


class TestConnectionReq(BaseModel):
    target: str = Field(..., description="asr / cos / deepseek")
