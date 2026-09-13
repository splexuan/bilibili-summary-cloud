# ═══════════════════════════════════════════
# 多阶段构建：Web 与 Worker 共用此镜像
# ═══════════════════════════════════════════

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    TZ=Asia/Shanghai

WORKDIR /app

# FFmpeg：音频转码必需
# tzdata：容器内时区；curl：健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*


# ─── 依赖层（单独一层，便于缓存）───
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ─── 运行层 ───
FROM base AS runtime

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY app/ ./app/
COPY alembic.ini ./
COPY migrations/ ./migrations/

# 非 root 运行
RUN useradd -m -u 1000 bsum \
    && mkdir -p /app/data /app/temp /app/logs \
    && chown -R bsum:bsum /app
USER bsum

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 默认启动 Web；Worker 通过 compose 覆盖 command。
# 先跑 bootstrap（等库就绪 + 建表 + 管理员 + 默认配置），再起服务；
# bootstrap 可重复执行，容器每次启动都跑是安全的。
CMD ["sh", "-c", "python -m app.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
