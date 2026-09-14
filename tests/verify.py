"""端到端静态校验 —— 检查模板、静态资源、路由与 API 契约是否自洽

用法（在项目根目录）：python tests/verify.py
"""
import sys
from pathlib import Path

# 脚本在 tests/ 下，项目根是上一级
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TPL = ROOT / "app" / "templates"
ST = ROOT / "app" / "static"

fails = []
warns = []


def chk(cond, msg):
    (print if cond else (lambda m: (fails.append(m), print(m))))(("  OK  " if cond else "  FAIL ") + msg)


print("\n=== 1. 模板文件 ===")
for name in ("index.html", "login.html", "knowledge.html", "admin.html"):
    p = TPL / name
    chk(p.exists() and p.stat().st_size > 500, f"{name} ({p.stat().st_size if p.exists() else 0} bytes)")

print("\n=== 2. 静态资源 ===")
for name in ("common.css", "common.js", "favicon.svg"):
    p = ST / name
    chk(p.exists(), f"{name}")

print("\n=== 3. 导入应用 ===")
try:
    import app.main as m
    chk(True, f"app.main 导入成功（v{m.__version__}）")
except Exception as exc:
    chk(False, f"app.main 导入失败: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n=== 4. 路由注册 ===")


def walk(node, out):
    """递归收集路由 —— 新版本 FastAPI 会把 include_router 包成 _IncludedRouter"""
    for sub in getattr(node, "routes", []) or []:
        p = getattr(sub, "path", None)
        meths = getattr(sub, "methods", None)
        if p:
            out.setdefault(p, set()).update((meths or set()) - {"HEAD", "OPTIONS"})
        for attr in ("original_router", "router", "effective_candidates", "effective_route_contexts"):
            inner = getattr(sub, attr, None)
            if inner is None:
                continue
            if isinstance(inner, dict):
                for v in inner.values():
                    walk(v, out)
            elif hasattr(inner, "routes"):
                walk(inner, out)


routes = {}
walk(m.app, routes)

expect_pages = ["/", "/login", "/knowledge", "/admin", "/favicon.ico"]
for p in expect_pages:
    chk(p in routes, f"页面路由 {p}")

expect_api = [
    "/api/health", "/api/config/public",
    "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/password",
    "/api/video", "/api/article",
    "/api/job/{job_id}", "/api/job/{job_id}/stream", "/api/job/{job_id}/cancel",
    "/api/videos", "/api/videos/{video_id}", "/api/videos/{video_id}/transcript",
    "/api/articles", "/api/articles/{article_id}", "/api/usage",
    "/api/chat/video/{video_id}", "/api/chat/kb",
    "/api/chat/history", "/api/chat/history/{chat_id}",
    "/api/chat/export/video/{video_id}", "/api/chat/export/article/{article_id}",
    "/api/settings", "/api/settings/test/deepseek", "/api/settings/stats",
    "/api/admin/settings", "/api/admin/settings/test", "/api/admin/settings/cos-check",
    "/api/admin/users", "/api/admin/invites",
    "/api/admin/overview", "/api/admin/queue/requeue", "/api/admin/online",
]
missing = [p for p in expect_api if p not in routes]
if missing:
    for p in missing:
        chk(False, f"缺少 API 路由 {p}")
else:
    chk(True, f"前端依赖的 {len(expect_api)} 个 API 路由全部存在")

print("\n=== 5. 模板引用的端点在服务端是否存在 ===")
import re

# 收集前端 JS 里出现的 /api/... 字面量（去掉模板变量部分）
used = set()
for name in ("index.html", "knowledge.html", "admin.html", "login.html", "common.js"):
    txt = (TPL / name).read_text(encoding="utf-8") if name.endswith(".html") else (ST / name).read_text(encoding="utf-8")
    for mt in re.finditer(r"['\"`](/api/[A-Za-z0-9_/\-{}.\$]+)", txt):
        used.add(mt.group(1))


# 归一化：把 ${...} 与路由占位符合并为通配符
def norm(u):
    u = re.sub(r"\$\{[^}]*\}", "{x}", u)
    u = re.sub(r"\{[^}]+\}", "{x}", u)
    return u.rstrip("/")


declared = {norm(p) for p in routes if p.startswith("/api")}

unmatched = set()
for u in used:
    n = norm(u)
    if not n:
        continue
    if n in declared:
        continue
    # 前缀匹配，覆盖 /api/videos?page=1 这类查询串被截断的情况
    if any(d == n or n.startswith(d + "/") or d.startswith(n + "/") for d in declared):
        continue
    unmatched.add(u)

if unmatched:
    for u in sorted(unmatched):
        warns.append(f"前端调用了服务端未声明的路径：{u}")
        print("  WARN " + u)
else:
    chk(True, f"前端调用的 {len(used)} 个 API 路径均能在服务端找到")

print("\n=== 6. 关键响应字段核对（前端读取的字段 vs 接口实际返回）===")
import inspect

from app.api.user import content as um

src = inspect.getsource(um.list_videos)
for f in ("thumbnail", "duration_str", "has_summary", "transcript_source", "processed_at"):
    chk(f'"{f}"' in src, f"/api/videos 返回 {f}")

src2 = inspect.getsource(um.job_status)
for f in ("video_id", "article_id", "stage", "progress", "live"):
    chk(f'"{f}"' in src2, f"/api/job/{{id}} 返回 {f}")

from app.api.user import chat as cm
src3 = inspect.getsource(cm._stream_response)
chk('"sources"' in src3, "SSE 首帧携带 sources")
chk('"delta"' in src3, "SSE 增量帧为 delta")
chk('"end"' in src3, "SSE 结束帧为 end")

# 任务结束帧：统一由 _terminal_events 产出（job_stream 调用它），
# 必须回数据库取终态 —— Worker 是「先写 Redis、后写库」，
# 只看 Redis 拿不到 video_id，前端会定位不到结果
src4 = inspect.getsource(um.job_stream)
src4b = inspect.getsource(um._terminal_events)
chk("_terminal_events" in src4, "job_stream 经 _terminal_events 下发终态")
chk('"video_id"' in src4b and '"article_id"' in src4b,
    "任务结束帧携带 video_id/article_id（前端据此定位结果）")
chk('"error"' in src4b and '"end"' in src4b,
    "失败发 error 事件、成功发 end 事件")

print("\n=== 7. 页面模板中的 id 与 JS 引用一致性 ===")
idx = (TPL / "index.html").read_text(encoding="utf-8")
ids = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', idx))
refs = set(re.findall(r"getElementById\(['\"]([A-Za-z0-9_\-]+)['\"]\)", idx))
bad = refs - ids
if bad:
    for b in sorted(bad):
        warns.append(f"index.html 引用了不存在的元素 id: {b}")
        print("  WARN " + b)
else:
    chk(True, f"index.html 的 {len(refs)} 个元素引用全部存在")

kn = (TPL / "knowledge.html").read_text(encoding="utf-8")
ids2 = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', kn))
# knowledge.html 用 el('xxx') 简写取元素
refs2 = set(re.findall(r"getElementById\(['\"]([A-Za-z0-9_\-]+)['\"]\)", kn))
refs2 |= set(re.findall(r"\bel\(['\"]([A-Za-z0-9_\-]+)['\"]\)", kn))
bad2 = refs2 - ids2
if bad2:
    for b in sorted(bad2):
        warns.append(f"knowledge.html 引用了不存在的元素 id: {b}")
        print("  WARN " + b)
else:
    chk(True, f"knowledge.html 的 {len(refs2)} 个元素引用全部存在")

print("\n=== 8. CSS 异常字符检查 ===")
css = (ST / "common.css").read_text(encoding="utf-8")
fullwidth = re.findall(r"[０-９Ａ-Ｚａ-ｚ]", css)
if fullwidth:
    for c in set(fullwidth):
        fails.append(f"common.css 含全角字符 U+{ord(c):04X} ({c})")
        print(f"  FAIL 全角字符 U+{ord(c):04X} ({c})")
else:
    chk(True, "common.css 无全角字符污染")

print("\n" + "=" * 52)
print(f"通过项：见上   失败：{len(fails)}   警告：{len(warns)}")
if fails:
    print("\n失败清单：")
    for f in fails:
        print("  - " + f)
if warns:
    print("\n警告清单：")
    for w in warns:
        print("  - " + w)
print("=" * 52)
sys.exit(1 if fails else 0)
