"""
腾讯云录音文件识别（ASR）服务。

调用链路：
    音频上传 COS → 生成预签名 URL → CreateRecTask 提交
    → DescribeTaskStatus 轮询 → 成功后立刻落库 → 删除 COS 音频

【必须遵守的约束】
1. 音频来源只能用 URL（≤1GB / ≤5小时）。本地 base64 直传限 5MB，不适用。
2. COS 必须与服务器同地域，否则 ASR 拉取音频会产生外网下行流量费。
3. 腾讯云 TaskId 与识别结果均只保留 24 小时，且 TaskId 跨日可能重复，
   不可作业务唯一键。轮询成功后必须立刻写入数据库。
4. ResTextFormat=3（按标点分段，适用字幕场景）不额外收费；
   =4（语义分段）与 =5（口语转书面语）为增值付费项，不要开。
5. 额度耗尽返回 FailedOperation.UserHasNoFreeAmount，需专门提示。
"""
import json
import time
from pathlib import Path

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.asr.v20190614 import asr_client, models

from app.core.constants import ASRStatus
from app.core.exceptions import ASRError, ConfigError, QuotaExceededError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 腾讯云错误码 → 用户可读提示
#
# 文案里必须写明「腾讯云账号」：这两个码说的是**腾讯云账号侧**的资源包/免费额度
# 用完了，和本站给用户算的月度额度完全是两回事。写成「本月额度已用完」会让
# 没用过 ASR 的用户莫名其妙（站点用量明明是 0）。
_ERROR_HINTS = {
    "FailedOperation.UserHasNoFreeAmount": (
        "腾讯云账号的语音识别免费额度已用完（不是本站给你的月度额度）。"
        "请管理员到腾讯云控制台开通后付费或购买资源包"
    ),
    "FailedOperation.UserHasNoAmount": (
        "腾讯云账号的语音识别资源包/额度已耗尽（不是本站给你的月度额度）。"
        "请管理员到腾讯云控制台购买资源包或开通后付费"
    ),
    "FailedOperation.UserNotRegistered": "腾讯云语音识别服务尚未开通，请先前往腾讯云控制台开通",
    "FailedOperation.ServiceIsolate": "腾讯云账号因欠费已停止服务",
    "FailedOperation.ErrorDownFile": "腾讯云无法下载音频文件，请检查 COS 是否与服务器同地域",
    "InternalError.ErrorDownFile": "腾讯云无法下载音频文件，请检查 COS 配置",
    "FailedOperation.NoSuchTask": "识别任务不存在或已超过 24 小时有效期",
    "RequestLimitExceeded.UinLimitExceeded": "请求过于频繁，请稍后再试",
}

_QUOTA_CODES = {
    "FailedOperation.UserHasNoFreeAmount",
    "FailedOperation.UserHasNoAmount",
}

# 最大可提交时长（秒）：腾讯云限制 5 小时
MAX_AUDIO_SECONDS = 5 * 3600

# 支持的音频格式
SUPPORTED_FORMATS = (
    "wav", "mp3", "m4a", "flv", "mp4", "wma", "3gp", "amr", "aac", "ogg-opus", "flac",
)


class ASRService:
    """录音文件识别客户端。按配置实例化。"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str = "ap-guangzhou",
        engine: str = "16k_zh_en_2.0",
        res_text_format: int = 3,
        poll_interval: int = 4,
        poll_timeout: int = 1800,
    ):
        if not (secret_id and secret_key):
            raise ConfigError("腾讯云 ASR 配置不完整，请在后台管理端填写 SecretId / SecretKey")

        self.engine = engine
        self.res_text_format = res_text_format
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

        cred = credential.Credential(secret_id, secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "asr.tencentcloudapi.com"
        http_profile.reqTimeout = 60

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        self.client = asr_client.AsrClient(cred, region, client_profile)

    # ═══════════════════════════════════════
    # 提交任务
    # ═══════════════════════════════════════

    def create_task(self, audio_url: str, duration_sec: int = 0) -> str:
        """
        提交识别任务，返回腾讯云 TaskId。

        audio_url: COS 预签名 URL（需公网可访问，同地域走内网）
        duration_sec: 音频时长，用于提交前校验
        """
        if duration_sec and duration_sec > MAX_AUDIO_SECONDS:
            raise ASRError(
                f"音频时长 {duration_sec // 60} 分钟超过腾讯云单次上限 5 小时，"
                "需先切分再提交"
            )

        req = models.CreateRecTaskRequest()
        req.EngineModelType = self.engine
        req.ChannelNum = 1                      # 16k 音频仅支持单声道
        req.ResTextFormat = self.res_text_format  # 3 = 按标点分段，适用字幕场景
        req.SourceType = 0                      # 0 = 音频 URL
        req.Url = audio_url
        req.SpeakerDiarization = 0              # 单人讲述场景无需分离
        req.ConvertNumMode = 1                  # 数字转换为阿拉伯数字

        try:
            resp = self.client.CreateRecTask(req)
            task_id = str(resp.Data.TaskId)
            logger.info("ASR 任务已提交: TaskId=%s", task_id)
            return task_id
        except TencentCloudSDKException as exc:
            self._raise_friendly(exc)

    # ═══════════════════════════════════════
    # 查询任务
    # ═══════════════════════════════════════

    def describe_task(self, task_id: str) -> dict:
        """查询一次任务状态，返回归一化结果"""
        req = models.DescribeTaskStatusRequest()
        req.TaskId = int(task_id)

        try:
            resp = self.client.DescribeTaskStatus(req)
        except TencentCloudSDKException as exc:
            self._raise_friendly(exc)

        data = resp.Data
        status = ASRStatus.TENCENT_MAP.get(data.Status, ASRStatus.PENDING)

        return {
            "status": status,
            "audio_duration": data.AudioDuration or 0,
            "result": data.Result or "",
            "result_detail": self._parse_detail(data),
            "error": data.ErrorMsg or "",
        }

    def _parse_detail(self, data) -> list[dict]:
        """解析 ResultDetail，提取分句与时间戳"""
        out = []
        for item in (getattr(data, "ResultDetail", None) or []):
            out.append({
                "text": item.FinalSentence or "",
                "start_ms": item.StartMs or 0,
                "end_ms": item.EndMs or 0,
                "speaker_id": getattr(item, "SpeakerId", 0) or 0,
            })
        return out

    # ═══════════════════════════════════════
    # 轮询直到完成
    # ═══════════════════════════════════════

    def wait_for_result(self, task_id: str, on_progress=None) -> dict:
        """
        轮询直到任务完成或超时。

        on_progress(elapsed_sec, status) — 进度回调
        返回 {"status", "text", "audio_duration", "detail", "error"}
        """
        started = time.time()
        last_status = ASRStatus.PENDING

        while True:
            elapsed = time.time() - started
            if elapsed > self.poll_timeout:
                raise ASRError(f"语音识别超时（已等待 {int(elapsed)} 秒），请稍后重试")

            info = self.describe_task(task_id)
            last_status = info["status"]

            if on_progress:
                on_progress(int(elapsed), last_status)

            if info["status"] == ASRStatus.SUCCESS:
                text = self._extract_text(info)
                if not text.strip():
                    raise ASRError("语音识别完成但未识别到有效内容")
                logger.info(
                    "ASR 完成: TaskId=%s, 音频 %.1f 秒, 文本 %d 字",
                    task_id, info["audio_duration"], len(text),
                )
                return {
                    "status": ASRStatus.SUCCESS,
                    "text": text,
                    "audio_duration": int(info["audio_duration"] or 0),
                    "detail": info["result_detail"],
                    "error": "",
                }

            if info["status"] == ASRStatus.FAILED:
                msg = info["error"] or "识别失败"
                hint = _ERROR_HINTS.get(msg, "")
                if hint:
                    raise ASRError(hint)
                raise ASRError(f"语音识别失败：{msg}")

            time.sleep(self.poll_interval)

    def _extract_text(self, info: dict) -> str:
        """
        提取纯文本。

        优先用 ResultDetail 的分句（ResTextFormat=3 时已按标点分段，最干净）；
        否则从 Result 字段剥离时间戳。
        """
        detail = info.get("result_detail") or []
        if detail:
            return "\n".join(d["text"] for d in detail if d["text"]).strip()

        raw = info.get("result", "")
        if not raw:
            return ""

        # Result 格式：[0:0.020,0:2.380]  文本内容
        import re
        lines = []
        for line in raw.split("\n"):
            line = re.sub(r"^\[\d+:\d+\.\d+,\d+:\d+\.\d+\]\s*", "", line.strip())
            if line:
                lines.append(line)
        return "\n".join(lines).strip()

    # ═══════════════════════════════════════
    # 错误处理
    # ═══════════════════════════════════════

    def _raise_friendly(self, exc: TencentCloudSDKException):
        code = getattr(exc, "code", "") or ""
        msg = getattr(exc, "message", "") or str(exc)

        if code in _QUOTA_CODES:
            logger.warning("ASR 额度耗尽: %s", code)
            raise QuotaExceededError(_ERROR_HINTS.get(code))

        hint = _ERROR_HINTS.get(code)
        logger.error("ASR 调用失败 [%s]: %s", code, msg)
        raise ASRError(hint or f"语音识别调用失败：{msg}")


# ═══════════════════════════════════════════
# 实例管理
# ═══════════════════════════════════════════

_cached: ASRService | None = None
_cached_fp = ""


async def get_asr(db) -> ASRService:
    """获取 ASR 实例（配置变更自动重建）"""
    global _cached, _cached_fp

    from app.core.config import settings
    from app.db.settings_repo import get_asr_config

    cfg = await get_asr_config(db)
    fp = "|".join([
        cfg.get("asr_secret_id", ""),
        cfg.get("asr_region", ""),
        cfg.get("asr_engine", ""),
    ])

    if _cached is not None and fp == _cached_fp:
        return _cached

    _cached = ASRService(
        secret_id=cfg.get("asr_secret_id", ""),
        secret_key=cfg.get("asr_secret_key", ""),
        region=cfg.get("asr_region") or settings.asr_region,
        engine=cfg.get("asr_engine") or settings.asr_engine,
        res_text_format=settings.asr_res_text_format,
        poll_interval=settings.asr_poll_interval,
        poll_timeout=settings.asr_poll_timeout,
    )
    _cached_fp = fp
    return _cached


def reset_client_cache() -> None:
    global _cached, _cached_fp
    _cached = None
    _cached_fp = ""


def verify_config(secret_id: str, secret_key: str, region: str) -> tuple[bool, str]:
    """
    校验 ASR 配置是否可用（管理端「测试连接」用）。

    用一个不存在的 TaskId 查询，若返回「任务不存在」说明鉴权通过。
    """
    if not (secret_id and secret_key):
        return False, "SecretId 与 SecretKey 不能为空"

    try:
        svc = ASRService(secret_id=secret_id, secret_key=secret_key, region=region)
        try:
            svc.describe_task("1")
        except (ASRError, TencentCloudSDKException) as exc:
            msg = str(exc)
            # 鉴权通过但任务不存在 → 说明配置正确
            if "任务不存在" in msg or "NoSuchTask" in msg:
                return True, "配置有效"
            if "未开通" in msg or "UserNotRegistered" in msg:
                return False, "语音识别服务尚未开通，请先前往腾讯云控制台开通"
            if "SecretId" in msg or "鉴权" in msg or "AuthFailure" in msg:
                return False, "鉴权失败，请检查 SecretId / SecretKey"
            return True, "配置可用"
        return True, "配置有效"
    except ConfigError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"校验失败：{exc}"
