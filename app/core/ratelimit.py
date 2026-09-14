"""
接口限流 — Redis 固定窗口计数。

本地版有「每 IP 60 秒 10 次」的限流，云端版之前只有并发槽位和月度额度，
没有任何频率控制：登录接口可以无限试密码，写接口可以被脚本刷。

固定窗口 vs 滑动窗口：这里选固定窗口，一次 INCR 一次 EXPIRE 就够，
目标是「防滥用」而不是精确计量，没必要为边界效应引入更多复杂度。
"""
import time

from fastapi import Request

from app.core.config import settings
from app.core.logging import get_logger
from app.core.progress import get_redis

logger = get_logger(__name__)

# 只统计会改变状态的方法；GET（含 SSE 长连接）不限制
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 需要更严格配额的路径前缀：登录/注册防暴力破解、TTS 防被当免费网关
STRICT_PATHS = ("/api/auth/login", "/api/auth/register", "/api/tts")

WINDOW_SECONDS = 60


def client_ip(request: Request) -> str:
    """
    取真实客户端 IP。

    经过 Nginx 反代后 request.client.host 是 127.0.0.1，所有用户会共用
    一个桶，限流形同虚设 —— 所以优先读 X-Forwarded-For 的第一段。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else "unknown"


def hit(bucket: str, limit: int, window: int = WINDOW_SECONDS) -> tuple[bool, int]:
    """
    记一次访问。返回 (是否放行, 需等待秒数)。

    Redis 不可用时一律放行：限流组件故障不应该让整个服务不可用。
    """
    if limit <= 0:
        return True, 0

    try:
        r = get_redis()
        key = f"bsum:rl:{bucket}:{int(time.time() // window)}"
        count = r.incr(key)
        if count == 1:
            r.expire(key, window)

        if count > limit:
            ttl = r.ttl(key)
            return False, max(1, ttl)
        return True, 0
    except Exception as exc:
        logger.warning("限流检查失败，放行: %s", exc)
        return True, 0


def check_request(request: Request) -> tuple[bool, int]:
    """按请求特征判定是否放行"""
    if not settings.rate_limit_enabled:
        return True, 0
    if request.method not in _WRITE_METHODS:
        return True, 0

    path = request.url.path
    if not path.startswith("/api/"):
        return True, 0

    ip = client_ip(request)
    if path.startswith(STRICT_PATHS):
        limit = settings.strict_rate_limit_per_minute
        bucket = f"strict:{ip}"
    else:
        limit = settings.rate_limit_per_minute
        bucket = f"write:{ip}"

    return hit(bucket, limit)
