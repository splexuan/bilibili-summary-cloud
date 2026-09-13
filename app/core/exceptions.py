"""
统一异常体系 — 所有业务异常继承 AppError，由全局处理器转成统一 JSON 响应。
"""
from typing import Any


class AppError(Exception):
    """业务异常基类"""

    status_code: int = 400
    code: str = "APP_ERROR"
    message: str = "请求处理失败"

    def __init__(self, message: str | None = None, *, detail: Any = None):
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


# ─── 认证与授权 ───

class AuthError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "未登录或登录已失效"


class PermissionError_(AppError):
    status_code = 403
    code = "FORBIDDEN"
    message = "没有权限执行此操作"


class CredentialError(AppError):
    status_code = 400
    code = "CREDENTIAL_MISSING"
    message = "缺少必要的凭证配置"


# ─── 资源 ───

class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    message = "资源不存在"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    message = "资源冲突"


class QuotaExceededError(AppError):
    status_code = 429
    code = "QUOTA_EXCEEDED"
    message = "本月语音识别额度已用完"


class RateLimitError(AppError):
    status_code = 429
    code = "RATE_LIMITED"
    message = "操作过于频繁，请稍后再试"


# ─── 外部服务 ───

class ExternalServiceError(AppError):
    status_code = 502
    code = "EXTERNAL_ERROR"
    message = "外部服务调用失败"


class DownloadError(ExternalServiceError):
    code = "DOWNLOAD_FAILED"
    message = "视频下载失败"


class SubtitleUnavailable(AppError):
    """无字幕，需走 ASR — 属于正常流程分支，不是错误"""
    status_code = 200
    code = "NO_SUBTITLE"
    message = "该视频没有可用字幕"


class ASRError(ExternalServiceError):
    code = "ASR_FAILED"
    message = "语音识别失败"


class COSError(ExternalServiceError):
    code = "COS_FAILED"
    message = "对象存储操作失败"


class AIError(ExternalServiceError):
    code = "AI_FAILED"
    message = "AI 服务调用失败"


class ConfigError(AppError):
    status_code = 500
    code = "CONFIG_ERROR"
    message = "服务配置不完整"
