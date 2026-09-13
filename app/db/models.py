"""
SQLAlchemy 模型定义。

设计要点：
1. 所有业务表都带 user_id，查询必须带用户过滤
2. videos 的唯一键是 (user_id, vid)，同一视频不同用户各自独立
3. 凭证字段一律存密文（_enc 后缀）
4. asr_tasks 独立成表，因为 ASR 是异步任务 + 轮询模型，
   且腾讯云识别结果仅保留 24 小时，必须及时落库
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════
# 用户
# ═══════════════════════════════════════════

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin / member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # 用户自带凭证（密文）
    deepseek_key_enc: Mapped[str] = mapped_column(Text, default="")
    bili_cookie_enc: Mapped[str] = mapped_column(Text, default="")

    # 配额（秒），0 表示不限
    asr_quota_sec: Mapped[int] = mapped_column(Integer, default=0)
    # 允许用户自带 Key（管理员可单独关闭某用户）
    can_use_own_key: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    videos: Mapped[list["Video"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    articles: Mapped[list["Article"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ═══════════════════════════════════════════
# 全局配置（管理员在后台配置的密钥等）
# ═══════════════════════════════════════════

class SystemSetting(Base):
    """
    键值配置表。管理员在后台修改后立即生效，无需重启容器。
    敏感值（如密钥）存密文，is_encrypted 标记。
    """
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)  # 前端回显时脱敏
    description: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


# ═══════════════════════════════════════════
# 视频
# ═══════════════════════════════════════════

class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("user_id", "vid", name="uq_videos_user_vid"),
        Index("ix_videos_user_processed", "user_id", "processed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    vid: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    uploader: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[str] = mapped_column(String(32), default="")
    duration_str: Mapped[str] = mapped_column(String(32), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")

    # 封面存 COS 对象键，不再是本地路径
    thumbnail_key: Mapped[str] = mapped_column(Text, default="")

    # 转写来源：subtitle（字幕）/ asr（语音识别）/ manual
    transcript_source: Mapped[str] = mapped_column(String(16), default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="videos")


# ═══════════════════════════════════════════
# 文章
# ═══════════════════════════════════════════

class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("ix_articles_user_processed", "user_id", "processed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="articles")


# ═══════════════════════════════════════════
# 对话
# ═══════════════════════════════════════════

class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(16), default="kb")  # kb（知识库）/ video（视频内）
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, default="")
    messages: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ═══════════════════════════════════════════
# 任务
# ═══════════════════════════════════════════

class Job(Base):
    """
    任务表。进度写回 Redis（高频），终态落库（低频）。

    阶段常量见 app.core.constants.JobStage
    """
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # 任务类型：summarize_video / summarize_article / re_summarize
    type: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/done/error
    stage: Mapped[str] = mapped_column(String(32), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)

    # 关联对象（视频或文章）
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=True
    )
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=True
    )

    # 入参（JSON）
    params: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")

    # RQ 任务 ID，便于取消
    rq_job_id: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════
# ASR 任务
# ═══════════════════════════════════════════

class ASRTask(Base):
    """
    腾讯云录音文件识别任务。

    注意：
    - 腾讯云 TaskId 仅 24 小时有效，且跨日可能重复，不可作业务唯一键
    - 识别结果服务端仅保留 24 小时，轮询成功后必须立刻写入 videos.transcript
    - audio_duration_sec 用于额度统计
    """
    __tablename__ = "asr_tasks"
    __table_args__ = (
        Index("ix_asr_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=True
    )

    # 腾讯云任务 ID（仅用于轮询，不可作业务唯一键）
    tencent_task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/waiting/doing/success/failed

    # 音频时长（秒），用于免费额度统计
    audio_duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    # COS 中转的音频对象键，识别成功后应删除
    cos_audio_key: Mapped[str] = mapped_column(Text, default="")

    engine: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════
# 额度用量统计（按月聚合，便于超限预警）
# ═══════════════════════════════════════════

class UsageMonthly(Base):
    __tablename__ = "usage_monthly"
    __table_args__ = (
        UniqueConstraint("user_id", "year_month", name="uq_usage_user_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    year_month: Mapped[str] = mapped_column(String(7), index=True)  # 2026-09

    # 计数器一律 server_default，保证「新建对象后立刻 += n」不会撞上 None。
    # 只写 default=0 时，值由 Python 在 flush 阶段才填充，
    # 而 _record_ai_usage 是先 add 再 +=，会拿到 None 抛
    # "unsupported operand type(s) for +=: 'NoneType' and 'int'"。
    asr_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    asr_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    summary_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    ai_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"), default=0)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
