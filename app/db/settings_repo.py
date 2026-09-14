"""
全局配置读写（system_settings 表）

设计：
- 管理员在后台修改配置 → 写库 → 立即生效，无需重启
- 密钥类值加密存储（is_encrypted=True）
- 读取时「库优先，环境变量兜底」，方便首次部署用 .env 初始化
- 带进程内短缓存（TTL），避免每个请求都查库
"""
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import SystemSetting

logger = get_logger(__name__)

# ─── 配置项定义：(key, 描述, 是否敏感, 环境变量兜底值) ───

SETTING_DEFS: list[dict[str, Any]] = [
    # AI
    {"key": "deepseek_api_url", "desc": "DeepSeek 接口地址", "secret": False, "env": "deepseek_api_url"},
    {"key": "deepseek_model", "desc": "DeepSeek 模型名", "secret": False, "env": "deepseek_model"},
    {"key": "deepseek_api_key", "desc": "DeepSeek API Key（全局）", "secret": True, "env": None},
    {"key": "allow_user_own_key", "desc": "允许用户自带 Key", "secret": False, "env": "allow_user_own_key"},

    # 腾讯云 ASR
    {"key": "asr_secret_id", "desc": "腾讯云 SecretId", "secret": True, "env": "asr_secret_id"},
    {"key": "asr_secret_key", "desc": "腾讯云 SecretKey", "secret": True, "env": "asr_secret_key"},
    {"key": "asr_region", "desc": "ASR 地域", "secret": False, "env": "asr_region"},
    {"key": "asr_engine", "desc": "ASR 引擎模型", "secret": False, "env": "asr_engine"},
    {"key": "asr_monthly_quota_sec", "desc": "每月免费额度（秒）", "secret": False, "env": "asr_monthly_quota_sec"},

    # COS
    {"key": "cos_secret_id", "desc": "COS SecretId", "secret": True, "env": "cos_secret_id"},
    {"key": "cos_secret_key", "desc": "COS SecretKey", "secret": True, "env": "cos_secret_key"},
    {"key": "cos_region", "desc": "COS 地域（需与服务器一致）", "secret": False, "env": "cos_region"},
    {"key": "cos_bucket", "desc": "COS 存储桶", "secret": False, "env": "cos_bucket"},
    {"key": "cos_domain", "desc": "COS 自定义域名", "secret": False, "env": "cos_domain"},

    # 下载
    {"key": "http_proxy", "desc": "HTTP 代理（YouTube 用）", "secret": False, "env": "http_proxy"},
    {"key": "global_bili_cookie", "desc": "全局 B站 Cookie", "secret": True, "env": None},
    {"key": "ffmpeg_path", "desc": "FFmpeg 路径", "secret": False, "env": "ffmpeg_path"},

    # 业务开关
    {"key": "enable_asr", "desc": "启用语音识别兜底", "secret": False, "env": None},
    {"key": "share_summary_across_users", "desc": "跨用户复用已总结的视频（true / false）", "secret": False, "env": "share_summary_across_users"},
    {"key": "site_notice", "desc": "前台公告", "secret": False, "env": None},
    {"key": "registration_open", "desc": "开放邀请码注册", "secret": False, "env": None},
]

_DEF_MAP = {d["key"]: d for d in SETTING_DEFS}

# 进程内缓存：{key: (value, expire_ts)}
_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 30


def _cache_get(key: str) -> str | None:
    item = _cache.get(key)
    if item and item[1] > time.time():
        return item[0]
    return None


def _cache_set(key: str, value: str) -> None:
    _cache[key] = (value, time.time() + _CACHE_TTL)


def clear_cache(key: str | None = None) -> None:
    if key:
        _cache.pop(key, None)
    else:
        _cache.clear()


def _env_fallback(key: str) -> str:
    """环境变量兜底"""
    d = _DEF_MAP.get(key)
    if not d or not d.get("env"):
        return ""
    val = getattr(settings, d["env"], "")
    return "" if val is None else str(val)


# ═══════════════════════════════════════════
# 读
# ═══════════════════════════════════════════

async def get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    """读取单个配置（自动解密）"""
    cached = _cache_get(key)
    if cached is not None:
        return cached

    row = await db.get(SystemSetting, key)
    if row is None or not row.value:
        val = _env_fallback(key) or default
    else:
        val = decrypt_secret(row.value) if row.is_encrypted else row.value

    _cache_set(key, val)
    return val


async def get_settings_map(db: AsyncSession, keys: list[str]) -> dict[str, str]:
    """批量读取"""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key.in_(keys)))
    rows = {r.key: r for r in result.scalars().all()}

    out: dict[str, str] = {}
    for k in keys:
        row = rows.get(k)
        if row is None or not row.value:
            out[k] = _env_fallback(k)
        else:
            out[k] = decrypt_secret(row.value) if row.is_encrypted else row.value
    return out


async def get_all_for_admin(db: AsyncSession) -> list[dict]:
    """管理端列表：敏感值脱敏展示"""
    result = await db.execute(select(SystemSetting))
    rows = {r.key: r for r in result.scalars().all()}

    out = []
    for d in SETTING_DEFS:
        key = d["key"]
        row = rows.get(key)
        if row and row.value:
            raw = decrypt_secret(row.value) if row.is_encrypted else row.value
            source = "db"
        else:
            raw = _env_fallback(key)
            source = "env" if raw else "unset"

        if d["secret"]:
            display = mask_secret(raw)
        else:
            display = raw

        out.append({
            "key": key,
            "value": display,
            "is_secret": d["secret"],
            "is_set": bool(raw),
            "source": source,
            "description": d["desc"],
        })
    return out


# ═══════════════════════════════════════════
# 写
# ═══════════════════════════════════════════

async def set_setting(db: AsyncSession, key: str, value: str, updated_by: int | None = None) -> None:
    """写入配置。敏感项自动加密。"""
    if key not in _DEF_MAP:
        logger.warning("尝试写入未定义的配置项: %s", key)
        return

    d = _DEF_MAP[key]
    is_secret = bool(d["secret"])
    stored = encrypt_secret(value) if is_secret else value

    row = await db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(
            key=key,
            value=stored,
            is_encrypted=is_secret,
            is_secret=is_secret,
            description=d["desc"],
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.value = stored
        row.is_encrypted = is_secret
        row.is_secret = is_secret
        row.description = d["desc"]
        row.updated_by = updated_by

    clear_cache(key)
    logger.info("配置已更新: %s", key)


async def set_many(db: AsyncSession, items: dict[str, str], updated_by: int | None = None) -> list[str]:
    """批量写入，返回实际更新的键。空字符串表示「不修改」（便于前端只提交改动项）"""
    changed = []
    for k, v in items.items():
        if v is None or v == "":
            continue
        if k not in _DEF_MAP:
            continue
        await set_setting(db, k, v, updated_by)
        changed.append(k)
    return changed


# ═══════════════════════════════════════════
# 便捷取用
# ═══════════════════════════════════════════

async def get_deepseek_config(db: AsyncSession) -> tuple[str, str, str]:
    """返回 (api_url, api_key, model)"""
    m = await get_settings_map(db, ["deepseek_api_url", "deepseek_api_key", "deepseek_model"])
    return (
        m.get("deepseek_api_url") or settings.deepseek_api_url,
        m.get("deepseek_api_key", ""),
        m.get("deepseek_model") or settings.deepseek_model,
    )


async def get_asr_config(db: AsyncSession) -> dict:
    return await get_settings_map(db, [
        "asr_secret_id", "asr_secret_key", "asr_region", "asr_engine",
        "asr_monthly_quota_sec",
    ])


async def get_cos_config(db: AsyncSession) -> dict:
    return await get_settings_map(db, [
        "cos_secret_id", "cos_secret_key", "cos_region", "cos_bucket", "cos_domain",
    ])


async def is_enabled(db: AsyncSession, key: str, default: bool = True) -> bool:
    val = await get_setting(db, key, "true" if default else "false")
    return str(val).lower() in ("1", "true", "yes", "on")
