"""管理端 — 全局密钥与服务配置"""
import asyncio

import httpx
from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import Ok, SettingsUpdateReq
from app.core.logging import get_logger
from app.db.settings_repo import get_all_for_admin, get_settings_map, set_many

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin/settings", tags=["管理-配置"])


@router.get("")
async def list_settings(admin: CurrentUser, db: DbSession):
    """
    全部配置项列表。

    敏感值（密钥）返回脱敏串，只用于显示「是否已配置」，
    不接受前端回显值直接写回。
    """
    items = await get_all_for_admin(db)
    return {"items": items}


@router.put("", response_model=Ok)
async def update_settings(req: SettingsUpdateReq, admin: CurrentUser, db: DbSession):
    """批量更新配置。空字符串表示不修改该项。"""
    changed = await set_many(db, req.items, updated_by=admin.id)
    await db.flush()

    # 重置各服务的实例缓存，让新配置立即生效
    if any(k.startswith("cos_") for k in changed):
        from app.services.cos_service import reset_client_cache

        reset_client_cache()
    if any(k.startswith("asr_") for k in changed):
        from app.services.asr_service import reset_client_cache as reset_asr

        reset_asr()

    logger.info("管理员 %s 更新配置: %s", admin.username, changed)
    return Ok(message=f"已更新 {len(changed)} 项配置")


@router.post("/test")
async def test_connection(payload: dict, admin: CurrentUser, db: DbSession):
    """测试外部服务连通性：asr / cos / deepseek"""
    target = (payload.get("target") or "").lower()

    if target == "deepseek":
        return await _test_deepseek(db)
    if target == "asr":
        return await _test_asr(db)
    if target == "cos":
        return await _test_cos(db)

    return {"ok": False, "message": f"未知的测试目标：{target}"}


async def _test_deepseek(db) -> dict:
    cfg = await get_settings_map(db, ["deepseek_api_url", "deepseek_api_key", "deepseek_model"])
    url = cfg.get("deepseek_api_url", "")
    key = cfg.get("deepseek_api_key", "")
    model = cfg.get("deepseek_model", "")

    if not key:
        return {"ok": False, "message": "未配置 DeepSeek API Key"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复 ok"}],
        "max_tokens": 8,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {key}"}
            )
        if resp.status_code == 200:
            return {"ok": True, "message": f"连接正常，模型 {model}"}
        if resp.status_code == 401:
            return {"ok": False, "message": "API Key 无效"}
        if resp.status_code == 402:
            return {"ok": False, "message": "账户余额不足"}
        return {"ok": False, "message": f"接口返回 {resp.status_code}"}
    except Exception as exc:
        return {"ok": False, "message": f"连接失败：{exc}"}


async def _test_asr(db) -> dict:
    from app.db.settings_repo import get_asr_config
    from app.services.asr_service import verify_config

    cfg = await get_asr_config(db)
    ok, msg = await asyncio.to_thread(
        verify_config,
        cfg.get("asr_secret_id", ""),
        cfg.get("asr_secret_key", ""),
        cfg.get("asr_region", "ap-guangzhou"),
    )

    # 鉴权通过说明账号配置没问题 → 顺手清掉「腾讯云额度不可用」的熔断标志，
    # 让管理员补了资源包之后不用等 TTL 过期就能重试
    if ok:
        from app.core.progress import clear_asr_quota_blocked

        await asyncio.to_thread(clear_asr_quota_blocked)

    return {"ok": ok, "message": msg}


async def _test_cos(db) -> dict:
    """上传一个探针对象再删除，验证读写权限"""
    from app.services.cos_service import get_cos

    try:
        cos = await get_cos(db)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    probe_key = "_healthcheck/probe.txt"

    def _probe() -> tuple[bool, str]:
        try:
            cos.upload_bytes(probe_key, b"ok", "text/plain")
            url = cos.presigned_url(probe_key, expires=60)
            cos.delete(probe_key)
            return True, f"读写正常（地域 {cos.region}，桶 {cos.bucket}）"
        except Exception as exc:
            return False, f"操作失败：{exc}"

    ok, msg = await asyncio.to_thread(_probe)
    return {"ok": ok, "message": msg}


@router.get("/cos-check")
async def cos_region_check(admin: CurrentUser, db: DbSession):
    """
    检查 COS 地域是否与服务器一致。
    跨地域会导致 ASR 拉取音频产生外网下行流量费。
    """
    from app.core.config import settings
    from app.db.settings_repo import get_cos_config

    cfg = await get_cos_config(db)
    cos_region = cfg.get("cos_region", "")
    asr_region = (await get_settings_map(db, ["asr_region"])).get("asr_region", "")

    same = cos_region == asr_region

    return {
        "cos_region": cos_region,
        "asr_region": asr_region,
        "same_region": same,
        "message": (
            "地域一致，音频走内网，不产生外网下行流量费"
            if same
            else f"⚠ COS 地域（{cos_region}）与 ASR 地域（{asr_region}）不一致，"
                 "ASR 拉取音频可能产生外网下行流量费"
        ),
    }
