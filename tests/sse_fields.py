#!/usr/bin/env python
"""
SSE 字段名一致性测试。

背景：这类 bug 静态检查抓不到、单元测试也覆盖不到 ——
前端读 `p.progress`，服务端写 `percent`，两边谁都没报错，
只是功能静默失效（进度条不走 / 视频信息卡不显示）。
唯一的防御就是显式比对两边的字段名。

覆盖：
  1. app/core/progress.py 的 set_progress 写入哪些 key
  2. index.html / knowledge.html 的 onProgress 读哪些 key
  3. 两者必须对得上
  4. common.js 的 error 处理不能把真实原因吞成一句兜底文案
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
FAILED: list[str] = []


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  OK   {msg}")


def bad(msg: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    FAILED.append(msg)
    print(f"  FAIL {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"       {line}")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def strip_js_comments(src: str) -> str:
    """去掉 JS 的 // 与 /* */ 注释，避免注释里的字面量干扰判断"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(^|[^:'\"\\])//[^\n]*", r"\1", src)
    return src


# ═══════════════════════════════════════════
# 1. 服务端写出的字段
# ═══════════════════════════════════════════

print("\n=== 1. progress.py 写入的字段 ===")

progress_src = read(ROOT / "app" / "core" / "progress.py")

# 抓 set_progress 里 data[...] = ... 和 data = {...} 的键
set_progress_body = progress_src
m = re.search(r"def set_progress\(.*?\n(.*?)\ndef ", progress_src, re.S)
if m:
    set_progress_body = m.group(1)

written: set[str] = set()
for km in re.finditer(r'data\["([^"]+)"\]\s*=', set_progress_body):
    written.add(km.group(1))
for km in re.finditer(r'"([a-z_]+)"\s*:', set_progress_body):
    written.add(km.group(1))

expected_keys = {"stage", "message", "percent", "ts", "extra"}
missing = expected_keys - written
if missing:
    bad(f"set_progress 缺少预期字段: {sorted(missing)}", f"实际写出: {sorted(written)}")
else:
    ok(f"set_progress 写出字段完整: {sorted(expected_keys)}")

if "progress" in written:
    bad("set_progress 写了 progress 字段（前端字段名应为 percent，不要引入同义字段）")
else:
    ok("没有引入 progress 同义字段（统一用 percent）")

# 确认 percent 确实是进度值
if re.search(r'"percent"\s*:\s*percent', set_progress_body):
    ok("percent 取值正确（来自 percent 参数）")
else:
    bad("percent 的取值来源可疑，请人工确认")


# ═══════════════════════════════════════════
# 2. 前端读取的字段
# ═══════════════════════════════════════════

print("\n=== 2. 前端 onProgress 读取的字段 ===")

# 只检查真正订阅了任务流的模板。knowledge.html 用的是自己的聊天流
# （/api/kb/chat 的 SSE），不走 subscribeJob，所以没有 onProgress。
TPL_DIR = ROOT / "app" / "templates"
subscribers = []
for p in sorted(TPL_DIR.glob("*.html")):
    if "subscribeJob(" in read(p):
        subscribers.append(p)

if not subscribers:
    bad("没有任何模板调用 subscribeJob —— 前端进度功能是不是整个没了？")
else:
    print(f"       订阅任务流的模板: {[p.name for p in subscribers]}")

for p in subscribers:
    tpl = p.name
    src = strip_js_comments(read(p))

    m = re.search(r"onProgress:\s*\((\w+)\)\s*=>\s*\{(.*?)\n\s*\},", src, re.S)
    if not m:
        bad(f"{tpl} 调用了 subscribeJob 但没有 onProgress 回调")
        continue

    var, body = m.group(1), m.group(2)

    # 读 percent？
    if re.search(rf"{var}\.percent", body):
        ok(f"{tpl} 读 {var}.percent（与服务端一致）")
    elif re.search(rf"{var}\.progress", body):
        bad(
            f"{tpl} 读的是 {var}.progress，但服务端写的是 percent",
            "这会让进度条静默失效 —— 改成 p.percent",
        )
    else:
        bad(f"{tpl} 的 onProgress 里没读 percent 字段")

    # 内容信息：服务端放 extra
    if re.search(rf"{var}\.extra", body):
        ok(f"{tpl} 兼容 {var}.extra（服务端字段）")
    elif re.search(rf"{var}\.data", body):
        # 允许 "p.data || p.extra" 这种兜底写法
        if re.search(rf"{var}\.data\s*\|\|\s*{var}\.extra", body):
            ok(f"{tpl} 用 {var}.data || {var}.extra 兜底（可接受）")
        else:
            bad(
                f"{tpl} 只读 {var}.data，但服务端写在 extra 里",
                "视频信息卡不会显示 —— 加上 || p.extra",
            )


# ═══════════════════════════════════════════
# 3. SSE error 处理不能吞掉真实原因
# ═══════════════════════════════════════════

print("\n=== 3. common.js 的 error 处理 ===")

cj = read(ROOT / "app" / "static" / "common.js")

m = re.search(r"es\.addEventListener\('error'.*?\n\s*\}\);", cj, re.S)
if not m:
    bad("common.js 里找不到 error 事件监听")
else:
    handler = m.group(0)

    if "JSON.parse" in handler and "e.data" in handler:
        ok("error 处理会尝试解析服务端 data")
    else:
        bad("error 处理没有解析 e.data，真实错误会被盖掉")

    # 兜底文案里应该带上可操作信息（jobId 或 API 路径）
    if "/api/job/" in handler or "jobId" in handler:
        ok("兜底文案带了可操作的排查指引")
    else:
        bad(
            "兜底文案太笼统（只有「连接中断」）",
            "应提示任务仍在后台、可查 /api/job/{id}",
        )

    if re.search(r"msg\s*=\s*'连接中断'\s*;", handler):
        bad("兜底文案仍是无条件覆盖（先赋值再 try 覆盖）—— 会吃掉真实 message")
    else:
        ok("兜底文案不会无条件覆盖服务端 message")


# ═══════════════════════════════════════════
# 4. SSE 事件名两边一致
# ═══════════════════════════════════════════

print("\n=== 4. SSE 事件名一致性 ===")

server_events = set(re.findall(r'_sse\(\s*"([a-z]+)"', read(ROOT / "app" / "api" / "user" / "content.py")))
client_events = set(re.findall(r"addEventListener\('([a-z]+)'", cj))

print(f"       服务端推送: {sorted(server_events)}")
print(f"       前端监听:   {sorted(client_events)}")

for ev in server_events:
    if ev in client_events:
        ok(f"事件 '{ev}' 两边都有")
    else:
        bad(f"服务端推 '{ev}' 但前端没监听")

for ev in client_events:
    if ev == "error":
        continue  # error 是 EventSource 内置事件，前端必须监听
    if ev not in server_events:
        bad(f"前端监听 '{ev}' 但服务端从不推送")


# ═══════════════════════════════════════════

print("\n" + "=" * 56)
print(f"通过 {PASS} 项   失败 {FAIL} 项")
print("=" * 56)

if FAILED:
    print("\n失败明细：")
    for f in FAILED:
        print(f"  - {f}")

sys.exit(1 if FAIL else 0)
