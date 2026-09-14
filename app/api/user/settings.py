"""用户端设置 — 个人凭证配置"""
import asyncio

import httpx
from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import Ok, UserSettingsReq
from app.core.cookies import count_entries
from app.core.logging import get_logger
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import Video
from app.db.settings_repo import get_deepseek_config, get_setting

logger = get_logger(__name__)
router = APIRouter(prefix="/api/settings", tags=["设置"])


@router.get("")
async def get_user_settings(user: CurrentUser, db: DbSession):
    """返回个人配置状态（密钥一律脱敏，绝不下发完整值）"""
    allow_own = await get_setting(db, "allow_user_own_key", "true")
    global_key_configured = bool((await get_deepseek_config(db))[1])

    ds_key = decrypt_secret(user.deepseek_key_enc)
    cookie = decrypt_secret(user.bili_cookie_enc)

    return {
        "deepseek": {
            "configured": bool(ds_key),
            "masked": mask_secret(ds_key),
            "source": "self" if ds_key else ("global" if global_key_configured else "none"),
        },
        "bili_cookie": {
            "configured": bool(cookie),
            "masked": mask_secret(cookie, 8, 6),
            "length": len(cookie),
            # 有效条目数：让用户一眼看出 Cookie 是不是解析出了东西
            # （空值项会被丢弃，比如 `buvid_fp=` 这种）
            "entries": count_entries(cookie),
        },
        "can_use_own_key": user.can_use_own_key,
        "allow_user_own_key": str(allow_own).lower() in ("1", "true", "yes", "on"),
    }


@router.put("", response_model=Ok)
async def update_user_settings(req: UserSettingsReq, user: CurrentUser, db: DbSession):
    """
    更新个人凭证。
    字段留空 = 不修改；置 clear_xxx 为 true = 清除。
    """
    changed = []

    if req.clear_deepseek_key:
        user.deepseek_key_enc = ""
        changed.append("DeepSeek Key 已清除")
    elif req.deepseek_key and req.deepseek_key.strip():
        user.deepseek_key_enc = encrypt_secret(req.deepseek_key.strip())
        changed.append("DeepSeek Key 已更新")

    if req.clear_bili_cookie:
        user.bili_cookie_enc = ""
        changed.append("B站 Cookie 已清除")
    elif req.bili_cookie and req.bili_cookie.strip():
        raw = req.bili_cookie.strip()
        user.bili_cookie_enc = encrypt_secret(raw)

        # 本地版同样会回报有效条数：Cookie 少了一半（比如漏复制 SESSDATA）
        # 时用户当场就能发现，而不是等任务失败才来排查
        entries = count_entries(raw)
        if entries:
            changed.append(f"B站 Cookie 已更新（{entries} 条有效）")
        else:
            changed.append("B站 Cookie 已保存，但未解析到有效条目，请确认复制完整")

    await db.flush()
    return Ok(message="、".join(changed) if changed else "未做任何修改")


@router.post("/test/deepseek")
async def test_deepseek(user: CurrentUser, db: DbSession):
    """测试 DeepSeek Key 是否可用（优先测用户自带的）"""
    api_url, global_key, model = await get_deepseek_config(db)

    key = decrypt_secret(user.deepseek_key_enc) or global_key
    if not key:
        return {"ok": False, "message": "未配置 API Key"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复 ok"}],
        "max_tokens": 8,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(api_url, json=payload, headers=headers)

        if resp.status_code == 200:
            return {"ok": True, "message": "连接正常"}
        if resp.status_code == 401:
            return {"ok": False, "message": "API Key 无效"}
        if resp.status_code == 402:
            return {"ok": False, "message": "账户余额不足"}
        return {"ok": False, "message": f"接口返回 {resp.status_code}"}
    except Exception as exc:
        logger.warning("DeepSeek 测试失败: %s", exc)
        return {"ok": False, "message": f"连接失败：{exc}"}


@router.get("/stats")
async def user_stats(user: CurrentUser, db: DbSession):
    """个人统计概览"""
    video_count = (
        await db.execute(select(func.count(Video.id)).where(Video.user_id == user.id))
    ).scalar_one()
    summary_count = (
        await db.execute(
            select(func.count(Video.id)).where(Video.user_id == user.id, Video.summary != "")
        )
    ).scalar_one()
    asr_count = (
        await db.execute(
            select(func.count(Video.id)).where(
                Video.user_id == user.id, Video.transcript_source == "asr"
            )
        )
    ).scalar_one()

    return {
        "video_count": video_count,
        "summary_count": summary_count,
        "asr_count": asr_count,
        "subtitle_count": summary_count - asr_count,
    }
