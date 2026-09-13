"""
HTTP 冒烟测试 —— 用 TestClient 真实跑一遍关键页面与接口。

为了不依赖 Postgres / Redis：
- 数据库切到 SQLite（aiosqlite）
- Redis 层用内存假实现替换

验证目标：
1. 四个页面能正常返回 HTML
2. 健康检查、公开配置可访问
3. 登录流程可用（管理员账号）
4. 未登录访问受保护接口会返回 401/403
5. 带 token 能读到自己的列表接口
6. 管理端接口需要 admin 角色
"""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ─── 环境变量必须在导入 app 之前设置 ───
TMP = Path(tempfile.mkdtemp(prefix="bsum_smoke_"))
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP / 'test.db'}"
os.environ["SECRET_KEY"] = "uL9k2YqWm3nR8tVx5zA7bC1dE4fG6hJ0kL2mN4oP6qR="
os.environ["JWT_SECRET"] = "smoke_test_jwt_secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test123456"
os.environ["MAX_TASKS_PER_USER"] = "2"

logging.disable(logging.CRITICAL)

# ─── 假 Redis：用 fakeredis 代替真实 Redis ───
# 自己手写的 stub 顶不住 RQ —— RQ 入队要 pipeline/事务/lua 脚本，
# fakeredis 是完整的内存实现，能真实跑通入队链路。
import fakeredis

import app.core.progress as progress_mod


def _install_fake_redis():
    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    progress_mod._redis = fake
    if hasattr(progress_mod, "get_redis"):
        progress_mod.get_redis = lambda: fake
    return fake


# 在导入 main 之前装好
FakeR = _install_fake_redis()

RESULTS = []


def chk(cond, msg):
    RESULTS.append((cond, msg))
    print(("  OK   " if cond else "  FAIL ") + msg)


print("=== A. 建表 + bootstrap ===")


async def _prepare():
    from app.db.models import Base
    from app.db.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return True


asyncio.run(_prepare())
chk(True, f"SQLite 建表完成 → {TMP / 'test.db'}")

print("\n=== B. 导入应用并启动 lifespan ===")
from fastapi.testclient import TestClient

import app.main as m

# lifespan 里的 create_all 走的是 session.engine，已替换为 sqlite
with TestClient(m.app) as client:
    chk(True, "TestClient 启动成功（lifespan 执行完毕）")

    print("\n=== C. 页面路由 ===")
    for path, must in [
        ("/", "视频总结工具"),
        ("/login", "登录"),
        ("/knowledge", "知识库"),
        ("/admin", "管理后台"),
        ("/favicon.ico", ""),
    ]:
        r = client.get(path)
        body = r.text if r.status_code == 200 else ""
        cond = r.status_code == 200 and (must in body or not must)
        chk(cond, f"GET {path} → {r.status_code}"
                  + (f"（含「{must}」）" if must else ""))

    print("\n=== D. 公开接口 ===")
    r = client.get("/api/health")
    d = r.json() if r.status_code == 200 else {}
    chk(r.status_code == 200, f"GET /api/health → {r.status_code} status={d.get('status')}")
    chk("checks" in d, "健康检查返回 checks 明细")

    r = client.get("/api/config/public")
    chk(r.status_code == 200, f"GET /api/config/public → {r.status_code}")
    cfg = r.json() if r.status_code == 200 else {}
    chk("version" in cfg and "max_tasks_per_user" in cfg,
        f"公开配置字段完整：{list(cfg.keys())}")

    print("\n=== E. 未登录访问受保护接口 ===")
    for path in ["/api/auth/me", "/api/videos", "/api/articles", "/api/usage",
                 "/api/settings", "/api/chat/history",
                 "/api/admin/overview", "/api/admin/settings", "/api/admin/users"]:
        r = client.get(path)
        chk(r.status_code in (401, 403), f"GET {path} 未登录 → {r.status_code}")

    print("\n=== F. 登录 ===")
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test123456"})
    chk(r.status_code == 200, f"POST /api/auth/login → {r.status_code}")
    tok = r.json().get("token", "") if r.status_code == 200 else ""
    chk(bool(tok), "登录返回 token")

    H = {"Authorization": f"Bearer {tok}"}

    print("\n=== G. 带 token 访问 ===")
    r = client.get("/api/auth/me", headers=H)
    chk(r.status_code == 200, f"GET /api/auth/me → {r.status_code}")
    me = r.json().get("user", {}) if r.status_code == 200 else {}
    chk(me.get("role") == "admin", f"身份为 admin（role={me.get('role')}）")
    chk("notice" in (r.json() if r.status_code == 200 else {}), "返回公告字段 notice")

    for path in ["/api/videos", "/api/articles", "/api/usage",
                 "/api/settings", "/api/settings/stats", "/api/chat/history"]:
        r = client.get(path, headers=H)
        chk(r.status_code == 200, f"GET {path} → {r.status_code}")

    r = client.get("/api/videos?page=1&page_size=20&search=", headers=H)
    d = r.json() if r.status_code == 200 else {}
    chk(all(k in d for k in ("items", "page", "page_size", "total", "has_more")),
        f"列表接口分页字段完整：{sorted(d.keys())}")

    print("\n=== H. 管理端接口 ===")
    for path in ["/api/admin/overview", "/api/admin/settings", "/api/admin/users",
                 "/api/admin/invites", "/api/admin/videos", "/api/admin/articles",
                 "/api/admin/jobs", "/api/admin/usage", "/api/admin/online",
                 "/api/admin/asr-tasks"]:
        r = client.get(path, headers=H)
        chk(r.status_code == 200, f"GET {path} → {r.status_code}")

    r = client.get("/api/admin/settings", headers=H)
    d = r.json() if r.status_code == 200 else {}
    items = d.get("items", [])
    chk(len(items) > 0, f"管理端配置项数量：{len(items)}")
    has_secret_flag = all("is_secret" in i and "is_set" in i for i in items)
    chk(has_secret_flag, "配置项带 is_secret / is_set 标记（前端据此控件）")
    leaked = [i["key"] for i in items
              if i.get("is_secret") and i.get("value") and len(str(i["value"])) > 12
              and not str(i["value"]).startswith("*")]
    chk(not leaked, f"密钥类配置已脱敏（疑似泄露项：{leaked}）")

    print("\n=== I. SSE 进度流（任务不存在时应 404）===")
    r = client.get("/api/job/99999/stream?token=" + tok)
    chk(r.status_code == 404, f"GET /api/job/99999/stream → {r.status_code}")

    print("\n=== J. 提交任务（验证入队链路真正跑通）===")
    # 用无效 URL 提交：接口应当受理并成功入队（真正的下载失败发生在 worker 里）
    r = client.post("/api/video", json={"url": "https://www.bilibili.com/video/BV1xx411c7mD", "force": False}, headers=H)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    chk(r.status_code == 200 and bool(body.get("job_id")),
        f"POST /api/video → {r.status_code}，job_id={body.get('job_id')}")

    rq_id = body.get("rq_id")
    chk(bool(rq_id), f"入队返回 rq_id={rq_id}")

    # 确认任务真的进了 RQ 队列
    from app.core.queue import get_queue
    qlen = len(get_queue())
    chk(qlen >= 1, f"RQ 队列中有 {qlen} 个待执行任务")

    r = client.post("/api/article",
                    json={"text": "这是一段测试正文。" * 50, "title": "冒烟测试", "url": ""},
                    headers=H)
    body2 = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    chk(r.status_code == 200 and bool(body2.get("job_id")),
        f"POST /api/article → {r.status_code}，job_id={body2.get('job_id')}")

    chk(len(get_queue()) >= 2, f"两次提交后队列共 {len(get_queue())} 个任务")

print("\n" + "=" * 56)
failed = [m for okk, m in RESULTS if not okk]
print(f"合计 {len(RESULTS)} 项，失败 {len(failed)} 项")
if failed:
    print("\n失败清单：")
    for f in failed:
        print("  - " + f)
print("=" * 56)
sys.exit(1 if failed else 0)
