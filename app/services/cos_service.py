"""
腾讯云 COS 对象存储封装。

实际只存两类对象（对象键由服务端统一生成，杜绝目录遍历）：
    thumbnails/{user_id}/{vid}.jpg     封面（长期保留，随内容删除）
    audio/{user_id}/{vid}_{ts}.m4a     音频中转（识别完立即删除）

注意：RAG / 知识库索引**不落 COS**，只存在进程内内存缓存
（见 services/retrieval.py 的 _rag_cache / _kb_cache），重启即失效并按需重建。
cos_service 里保留的 index_key() 目前没有任何调用点，不要误以为索引在对象存储里。

【重要】COS 地域必须与服务器地域一致。
官方说明：使用 COS 存储音频并生成 URL 提交 ASR 任务，
同地域走内网，不产生外网下行流量费用；跨地域则会产生费用。

【未配置时】没有本地磁盘兜底，也不会降级到本地目录：
    - 封面：上传失败只记 warning，thumbnail_key 留空，前端显示占位图
    - ASR：上传音频/生成预签名 URL 直接抛 COSError，任务失败
"""
import time
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosServiceError

from app.core.exceptions import COSError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 预签名 URL 有效期（秒）。ASR 下载完即可，给足余量。
PRESIGN_EXPIRES = 3600

# 展示用缩略图规格（16:9）。
# 站内最大的封面显示宽度是知识库预览弹窗的 180px，取 2 倍屏即 360px，
# 480 有富余。9:16 用 270 保持比例，服务端按比例缩放不会变形。
THUMB_SIZE = (480, 270)

# 展示用缩略图有效期：比预签名默认长一些，页面停留久了也不至于裂图
THUMB_EXPIRES = 7200


def _with_image_process(
    url: str,
    width: int,
    height: int,
    fmt: str = "webp",
    quality: int = 80,
) -> str:
    """
    在图片 URL 上追加 COS 基础图片处理（imageMogr2）参数。

    为什么要这么做：封面原图是 B 站的 1146~1920px 大图，单张 130~210KB，
    而站内最大只显示到 180px。由 COS 按需压缩，实测可降到 24~41KB
    （省 74%~84%），且不占额外存储 —— 每次访问实时生成，无需预生成多套图。

    参数失效时的行为（线上桶实测）：thumbnail/format 这类参数写错会被忽略、
    直接返回原图（200），所以最坏情况只是退回原图，不会裂图。
    前端仍保留了 onerror 退回原图的兜底 —— 因为 ci-process 这类会改变路由
    的参数写错时 COS 是返回 400 的（实测），留着兜底不吃亏。
    """
    if not url:
        return ""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}imageMogr2/thumbnail/{width}x{height}/format/{fmt}/quality/{quality}"


class COSClient:
    """
    按配置实例化。配置变更后由 get_cos 重新创建。

    构造时不校验配置完整性 —— 未配置时对象仍可创建，
    只有真正调用上传/下载/签名时才报错。
    这样「列出视频」这类只读接口不会因为没有 COS 而整体 502。
    """

    def __init__(self, secret_id: str, secret_key: str, region: str, bucket: str, domain: str = ""):
        self.secret_id = secret_id or ""
        self.secret_key = secret_key or ""
        self.bucket = bucket or ""
        self.region = region or ""
        self.domain = (domain or "").strip()

        self._client: CosS3Client | None = None

    @property
    def configured(self) -> bool:
        return bool(self.secret_id and self.secret_key and self.region and self.bucket)

    def _ensure(self) -> CosS3Client:
        """惰性建连；未配置时抛出可读错误"""
        if not self.configured:
            raise COSError(
                "COS 未配置完整（需要 SecretId / SecretKey / 地域 / 存储桶），"
                "请在后台管理端「密钥配置」中填写"
            )
        if self._client is None:
            config = CosConfig(
                Region=self.region,
                SecretId=self.secret_id,
                SecretKey=self.secret_key,
                Token=None,
                Scheme="https",
            )
            self._client = CosS3Client(config)
        return self._client

    @property
    def client(self) -> CosS3Client:
        """兼容旧写法：self.client.xxx(...)"""
        return self._ensure()

    # ─── 对象键 ───

    @staticmethod
    def thumb_key(user_id: int, vid: str) -> str:
        return f"thumbnails/{user_id}/{vid}.jpg"

    @staticmethod
    def audio_key(user_id: int, vid: str, ext: str = "m4a") -> str:
        return f"audio/{user_id}/{vid}_{int(time.time())}.{ext}"

    @staticmethod
    def index_key(user_id: int, vid: str) -> str:
        return f"index/{user_id}/{vid}.joblib"

    # ─── 上传 ───

    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Body=data,
                Key=key,
                ContentType=content_type,
            )
            logger.info("COS 上传成功: %s (%d bytes)", key, len(data))
            return key
        except CosServiceError as exc:
            logger.error("COS 上传失败 %s: %s", key, exc.get_error_msg())
            raise COSError(f"上传失败：{exc.get_error_msg()}")

    def upload_file(self, key: str, path: str | Path, content_type: str = "application/octet-stream") -> str:
        """上传本地文件（大文件用，走流式）"""
        path = Path(path)
        if not path.exists():
            raise COSError(f"待上传文件不存在：{path}")
        try:
            with path.open("rb") as fp:
                self.client.put_object(
                    Bucket=self.bucket,
                    Body=fp,
                    Key=key,
                    ContentType=content_type,
                )
            logger.info("COS 上传成功: %s (%d bytes)", key, path.stat().st_size)
            return key
        except CosServiceError as exc:
            logger.error("COS 上传失败 %s: %s", key, exc.get_error_msg())
            raise COSError(f"上传失败：{exc.get_error_msg()}")

    # ─── 下载 ───

    def download_bytes(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].get_raw_stream().read()
        except CosServiceError as exc:
            logger.error("COS 下载失败 %s: %s", key, exc.get_error_msg())
            raise COSError(f"下载失败：{exc.get_error_msg()}")

    def download_to_file(self, key: str, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            resp["Body"].get_stream_to_file(str(path))
            return path
        except CosServiceError as exc:
            logger.error("COS 下载失败 %s: %s", key, exc.get_error_msg())
            raise COSError(f"下载失败：{exc.get_error_msg()}")

    # ─── 预签名 URL（交给 ASR 拉取音频）───

    def presigned_url(self, key: str, expires: int = PRESIGN_EXPIRES) -> str:
        """
        生成预签名 URL。ASR 通过此 URL 拉取音频文件。
        同一地域时走内网，不计外网下行流量。
        """
        client = self._ensure()  # 未配置时在此抛 COSError（调用方负责兜底）
        try:
            url = client.get_presigned_url(
                Method="GET",
                Bucket=self.bucket,
                Key=key,
                Expired=expires,
            )
            # 若配置了自定义域名，替换默认域名
            if self.domain:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                url = url.replace(f"{parsed.scheme}://{parsed.netloc}", self.domain.rstrip("/"))
            return url
        except CosServiceError as exc:
            logger.error("生成预签名 URL 失败 %s: %s", key, exc.get_error_msg())
            raise COSError(f"生成下载链接失败：{exc.get_error_msg()}")

    # ─── 访问 URL（封面展示）───

    def public_url(self, key: str, expires: int = 7200) -> str:
        """
        生成带签名的访问 URL，用于前端展示封面。
        未配置或 key 为空时返回空串 —— 前端据此显示占位图，
        不让「列表里有个视频没有封面」升级成整个接口 502。
        """
        if not key or not self.configured:
            return ""
        try:
            return self.presigned_url(key, expires)
        except COSError:
            logger.warning("生成封面链接失败，已降级为空: %s", key)
            return ""

    def thumb_url(self, key: str, size: tuple[int, int] = THUMB_SIZE,
                  expires: int = THUMB_EXPIRES) -> str:
        """
        展示用小图地址：由 COS 实时压缩后再回源，比原图省 7~8 成流量。

        这里不做任何可用性探测 —— 处理参数不受支持时 COS 会返回原图，
        真出错也是前端的 onerror 兜底。探测一次要多发一次请求，
        而失败场景本就少见，交前端兜底更划算。
        """
        return _with_image_process(self.public_url(key, expires), *size)

    def display_urls(self, key: str, expires: int = 3600) -> tuple[str, str]:
        """
        展示用的一对地址：(原图 URL, 缩略图 URL)。

        原图作为缩略图加载失败时的兜底，所以两个都给前端。
        key 为空时返回两个空串，前端显示占位图。
        """
        full = self.public_url(key, expires)
        if not full:
            return "", ""
        return full, _with_image_process(full, *THUMB_SIZE)

    # ─── 删除 ───

    def delete(self, key: str) -> bool:
        if not key or not self.configured:
            return True  # 没配 COS = 没有远端对象，视为删除成功
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info("COS 删除成功: %s", key)
            return True
        except (CosServiceError, COSError) as exc:
            msg = exc.get_error_msg() if isinstance(exc, CosServiceError) else str(exc)
            logger.warning("COS 删除失败 %s: %s", key, msg)
            return False

    def delete_many(self, keys: list[str]) -> int:
        """批量删除，返回成功数"""
        keys = [k for k in keys if k]
        if not keys:
            return 0
        if not self.configured:
            return len(keys)
        try:
            # COS 批量删除单次上限 1000
            deleted = 0
            for i in range(0, len(keys), 1000):
                batch = keys[i:i + 1000]
                resp = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Object": [{"Key": k} for k in batch], "Quiet": "true"},
                )
                deleted += len(batch)
            logger.info("COS 批量删除 %d 个对象", deleted)
            return deleted
        except CosServiceError as exc:
            logger.warning("COS 批量删除失败: %s", exc.get_error_msg())
            return 0

    def exists(self, key: str) -> bool:
        try:
            return self.client.object_exists(Bucket=self.bucket, Key=key)
        except Exception:
            return False


# ═══════════════════════════════════════════
# 实例管理（配置变更后重建）
# ═══════════════════════════════════════════

_cached_client: COSClient | None = None
_cached_fingerprint: str = ""


def build_cos_client(cfg: dict) -> COSClient:
    return COSClient(
        secret_id=cfg.get("cos_secret_id", ""),
        secret_key=cfg.get("cos_secret_key", ""),
        region=cfg.get("cos_region", ""),
        bucket=cfg.get("cos_bucket", ""),
        domain=cfg.get("cos_domain", ""),
    )


async def get_cos(db) -> COSClient:
    """
    获取 COS 客户端（带配置指纹缓存）。
    配置变更时自动重建，无需重启。
    """
    global _cached_client, _cached_fingerprint

    from app.db.settings_repo import get_cos_config

    cfg = await get_cos_config(db)
    fingerprint = "|".join([
        cfg.get("cos_secret_id", ""),
        cfg.get("cos_region", ""),
        cfg.get("cos_bucket", ""),
        cfg.get("cos_domain", ""),
    ])

    if _cached_client is not None and fingerprint == _cached_fingerprint:
        return _cached_client

    _cached_client = build_cos_client(cfg)
    _cached_fingerprint = fingerprint
    return _cached_client


def reset_client_cache() -> None:
    global _cached_client, _cached_fingerprint
    _cached_client = None
    _cached_fingerprint = ""
