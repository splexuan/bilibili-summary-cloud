"""
安全模块 — 密码哈希、JWT 会话、凭证加解密。

凭证加密说明：
用户的 DeepSeek Key 与 B站 Cookie 属于高敏感数据，明文入库风险极高。
统一用 Fernet（AES-128-CBC + HMAC）对称加密后存储，
密钥来自 SECRET_KEY 环境变量，不入库、不进代码仓库。

密码哈希说明：
直接使用 bcrypt 库，不用 passlib —— passlib 1.7.4 与 bcrypt 5.x 不兼容
（前者依赖 bcrypt.__about__.__version__ 且其内部自检会触发 72 字节报错）。
bcrypt 本身有 72 字节上限，这里先做 SHA-256 预哈希再 base64，
既绕开上限、又保留 bcrypt 的加盐与成本因子。
"""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.exceptions import AuthError, ConfigError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ALGO = "HS256"
# bcrypt 成本因子，12 在现代 CPU 上约 250ms，兼顾安全与登录体验
_BCRYPT_ROUNDS = 12


# ═══════════════════════════════════════════
# 密码
# ═══════════════════════════════════════════

def _prehash(password: str) -> bytes:
    """
    先 SHA-256 再 base64，得到固定 44 字节。

    这样做解决两个问题：
    1. bcrypt 只认前 72 字节，超长密码会被静默截断（安全隐患）
    2. bcrypt 5.x 对超 72 字节直接抛 ValueError
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("ascii"))
    except Exception:
        return False


def generate_password(length: int = 12) -> str:
    """生成随机密码（管理员建号时用）"""
    return secrets.token_urlsafe(length)[:length]


# ═══════════════════════════════════════════
# JWT 会话
# ═══════════════════════════════════════════

def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_expire_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise AuthError("登录凭证无效")


# ═══════════════════════════════════════════
# 凭证加解密（Fernet）
# ═══════════════════════════════════════════

def _get_fernet() -> Fernet:
    key = settings.secret_key
    if not key or key.startswith("CHANGE_ME"):
        raise ConfigError(
            "SECRET_KEY 未配置。请生成 Fernet 密钥并写入 .env："
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        raise ConfigError(f"SECRET_KEY 格式无效，必须是 Fernet 密钥：{exc}")


def encrypt_secret(plaintext: str) -> str:
    """加密敏感字符串，返回可直接入库的文本"""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """解密，失败返回空串（避免脏数据导致整个请求崩溃）"""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("凭证解密失败：SECRET_KEY 可能已更换")
        return ""


def mask_secret(plaintext: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    """脱敏展示，用于前端回显。绝不向前端下发完整密钥。"""
    if not plaintext:
        return ""
    if len(plaintext) <= keep_head + keep_tail:
        return "*" * len(plaintext)
    return f"{plaintext[:keep_head]}{'*' * 8}{plaintext[-keep_tail:]}"


# ═══════════════════════════════════════════
# 签名与随机
# ═══════════════════════════════════════════

def generate_invite_code(length: int = 16) -> str:
    return secrets.token_urlsafe(length)[:length].replace("-", "").replace("_", "")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
