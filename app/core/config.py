"""
配置管理 — 统一从环境变量 / .env 读取。

设计要点：
- 所有密钥类配置都有环境变量兜底，但运行时优先读数据库 settings 表
  （管理员可在后台随时修改，无需重启容器）
- 业务参数沿用本地版调优结果，不要随意改动
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── 基础 ───
    app_env: str = "development"
    app_name: str = "bilibili-summary-cloud"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_base_url: str = "http://127.0.0.1:8000"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    # ─── 安全 ───
    secret_key: str = "CHANGE_ME_fernet_key"
    jwt_secret: str = "CHANGE_ME_jwt_secret"
    jwt_expire_hours: int = 168

    # ─── 数据库 ───
    # 支持三种后端（改 DATABASE_URL 即可切换，无需改代码）：
    #   MySQL（宝塔推荐）  mysql+aiomysql://user:pass@127.0.0.1:3306/bsum?charset=utf8mb4
    #   Postgres          postgresql+asyncpg://user:pass@127.0.0.1:5432/bsum
    #   SQLite（仅开发）    sqlite+aiosqlite:///./data/dev.db
    database_url: str = "postgresql+asyncpg://bsum:bsum_pass@localhost:5432/bilibili_summary"
    database_url_sync: str = "postgresql+psycopg://bsum:bsum_pass@localhost:5432/bilibili_summary"

    @property
    def db_backend(self) -> str:
        """识别当前使用的数据库后端：mysql / postgresql / sqlite"""
        u = self.database_url.lower()
        if u.startswith("mysql") or u.startswith("mariadb"):
            return "mysql"
        if u.startswith("sqlite"):
            return "sqlite"
        return "postgresql"

    @property
    def is_mysql(self) -> bool:
        return self.db_backend == "mysql"

    @property
    def is_sqlite(self) -> bool:
        return self.db_backend == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.db_backend == "postgresql"

    # ─── 队列 ───
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "bsum_tasks"
    max_tasks_per_user: int = 1
    worker_concurrency: int = 2

    # ─── 限流（每分钟每 IP，按写操作计；0 表示不限制）───
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    # 登录/注册/TTS 等敏感接口的独立配额
    strict_rate_limit_per_minute: int = 10

    # ─── COS ───
    cos_region: str = "ap-guangzhou"
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = ""
    cos_domain: str = ""

    # ─── ASR ───
    asr_secret_id: str = ""
    asr_secret_key: str = ""
    asr_region: str = "ap-guangzhou"
    asr_engine: str = "16k_zh_en_2.0"
    asr_res_text_format: int = 3
    asr_monthly_quota_sec: int = 36000
    asr_poll_interval: int = 4
    asr_poll_timeout: int = 1800

    # ─── AI ───
    deepseek_api_url: str = "https://api.deepseek.com/v1/chat/completions"
    deepseek_model: str = "deepseek-v4-flash"
    allow_user_own_key: bool = True

    # ─── 跨用户复用 ───
    # 别人已总结过同一个视频时，直接复制其「转写 + 总结」快照，
    # 省掉一次音频下载与 ASR（额度、时间、COS 流量）。后台可关闭。
    share_summary_across_users: bool = True

    # ─── 下载与代理 ───
    http_proxy: str = ""
    global_cookie_file: str = ""
    ffmpeg_path: str = "ffmpeg"

    # ─── 业务参数（沿用本地版，勿随意改动）───
    # 分段阈值已废弃：是否分段由「单次输出预算是否放得下」决定
    # （见 summarizer.single_shot_fits），不再依赖固定字数。
    # chunk_size / chunk_overlap 仍用于超出单次能力时的分段。
    chunk_size: int = 6500
    chunk_overlap: int = 600
    summary_ratio: float = 0.35
    summary_min_words: int = 1000
    summary_max_words: int = 8000

    # ─── 管理员初始化 ───
    # 注意：这里只是「.env 没写」时的兜底，仅用于本地开发。
    # 生产部署必须在 .env 里显式设置 ADMIN_PASSWORD。
    admin_username: str = "admin"
    admin_password: str = "admin123456"

    # ─── 目录 ───
    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def data_dir(self) -> Path:
        d = BASE_DIR / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def temp_dir(self) -> Path:
        d = BASE_DIR / "temp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def cookie_dir(self) -> Path:
        d = self.data_dir / "cookies"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "app" / "static"

    @property
    def template_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
