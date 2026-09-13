"""管理后台「密钥配置」页布局校验

针对一个真实踩过的样式 bug：
    左侧标签文字被挤成 1 个字符宽，中文逐字换行，看起来像竖排。

根因是两个规则打架：
    .setting-input { width: 250px; flex-shrink: 0 }   ← admin.html
    input[type="text"] { width: 100% }                ← common.css（优先级更高）
    叠加 .setting-info { min-width: 0 }，左侧被压到最小内容宽度。

这个脚本做静态断言，确保：
  1. .setting-input 不再用 flex-shrink:0 + width 的组合
  2. .setting-info 有可读的最小宽度
  3. 存在窄屏下的堆叠降级
  4. 标签文字被包在独立 span 里（配合 flex 对齐）

用法（项目根目录）：python tests/css_admin.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "app" / "templates" / "admin.html"
COMMON = ROOT / "app" / "static" / "common.css"

fails = []
warns = []
passes = []


def chk(cond, msg):
    if cond:
        passes.append(msg)
        print(f"  OK   {msg}")
    else:
        fails.append(msg)
        print(f"  FAIL {msg}")


def warn(cond, msg):
    """条件不满足时给警告而非失败"""
    if not cond:
        warns.append(msg)
        print(f"  WARN {msg}")


admin_raw = ADMIN.read_text(encoding="utf-8")
common_src = COMMON.read_text(encoding="utf-8")


def strip_comments(css: str) -> str:
    """删掉 CSS 注释。

    必须在扫描前做 —— 否则注释文本会黏在选择器字符串前面，
    导致 `.setting-info` 这类选择器匹配不上（实测踩过）。
    用等长空白替换而非直接删除，避免把相邻 token 拼在一起。
    """
    def _repl(m):
        # 保留换行，便于定位
        return "\n" * m.group(0).count("\n")

    return re.sub(r"/\*.*?\*/", _repl, css, flags=re.S)


def extract_style(html: str) -> str:
    """从 HTML 里只取出 <style> ... </style> 的内容（并去掉注释）。

    必须先做这一步再扫 CSS —— 否则 JS 里的 { } 会被当成 CSS 规则，
    body/html 也会混进选择器列表，导致选择器提取全部错位。
    """
    out = []
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", html, re.S | re.I):
        out.append(m.group(1))
    return strip_comments("\n".join(out))


admin_src = extract_style(admin_raw)
common_src = strip_comments(COMMON.read_text(encoding="utf-8"))
if not admin_src.strip():
    print("FAIL 未能从 admin.html 提取 <style> 内容")
    sys.exit(1)


def _rules(src: str):
    """产出 (选择器字符串, 规则体, 嵌套深度) —— 简易 CSS 扫描器。

    需要正确处理：
      - 逗号分组选择器（一个规则多个选择器）
      - @media 嵌套（深度 > 0 的规则只在媒体查询里生效）
      - 选择器里可能含字符串（如 [type="text"]）
    """
    i = 0
    n = len(src)
    sel_start = 0
    depth = 0
    body_start = 0
    while i < n:
        ch = src[i]
        # 跳过注释
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        # 跳过字符串
        if ch in "\"'":
            j = src.find(ch, i + 1)
            i = (j + 1) if j != -1 else n
            continue
        if ch == "{":
            sel = src[sel_start:i]
            depth += 1
            body_start = i + 1
            # 找配对的 }
            d = 1
            j = i + 1
            while j < n and d:
                c2 = src[j]
                if c2 in "\"'":
                    k = src.find(c2, j + 1)
                    j = (k + 1) if k != -1 else n
                    continue
                if c2 == "/" and j + 1 < n and src[j + 1] == "*":
                    k = src.find("*/", j + 2)
                    j = (k + 2) if k != -1 else n
                    continue
                if c2 == "{":
                    d += 1
                elif c2 == "}":
                    d -= 1
                j += 1
            yield (sel, src[body_start:j - 1], depth)
            depth -= 1
            i = j
            sel_start = j
            continue
        if ch == "}":
            sel_start = i + 1
        i += 1


def block(src: str, selector: str, include_media: bool = False) -> str:
    """抽出某选择器的规则体。

    include_media=False 时只取最外层规则（顶层的 .setting-input），
    避免把 @media 里的覆盖版本混进来。
    """
    target = selector.strip()
    found = []
    for sel, body, depth in _rules(src):
        if not include_media and depth != 1:
            continue
        parts = [p.strip() for p in sel.split(",")]
        if target in parts:
            found.append(body)
    return "\n".join(found)


def media_block(src: str) -> str:
    """取出 @media (max-width: ...) 的完整块内容（含嵌套），做花括号配对。

    手写 CSS 解析器容易在嵌套上出错，这里用最笨但最可靠的办法：
    定位 '@media' 后的第一个 '{'，向后数到配对的 '}'。
    """
    m = re.search(r"@media[^{]*\{", src)
    if not m:
        return ""
    start = m.end()  # '{' 之后
    depth = 1
    i = start
    n = len(src)
    while i < n and depth:
        c = src[i]
        if c in "\"'":
            j = src.find(c, i + 1)
            i = (j + 1) if j != -1 else n
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return src[start:i - 1]


print("\n=== 1. admin.html 里 .setting-input 的写法 ===")

si = block(admin_src, ".setting-input")
chk(bool(si), "找到 .setting-input 规则")

# 核心断言：不能再用 flex-shrink: 0（那会让它拒绝收缩，挤扁左侧）
has_shrink0 = re.search(r"flex-shrink\s*:\s*0", si) is not None
chk(not has_shrink0, ".setting-input 未使用 flex-shrink:0（避免挤扁左侧标签）")

# 不能用裸 width: 250px 这类固定宽度（会被 common.css 的 width:100% 覆盖，语义混乱）
# 允许 width: auto
has_bare_width = re.search(r"(?<!min-)(?<!max-)\bwidth\s*:\s*\d+px", si) is not None
chk(not has_bare_width, ".setting-input 未用固定 px 宽度（改用 flex-basis 更稳）")

has_flex = re.search(r"\bflex\s*:", si) is not None
chk(has_flex, ".setting-input 使用 flex 简写控制伸缩")

print("\n=== 2. .setting-info 的最小宽度 ===")

info = block(admin_src, ".setting-info")
chk(bool(info), "找到 .setting-info 规则")

mw = re.search(r"min-width\s*:\s*(\d+)px", info)
chk(mw is not None, ".setting-info 显式设置了 px 最小宽度")
if mw:
    px = int(mw.group(1))
    chk(px >= 160, f".setting-info min-width={px}px 足够容纳中文标签（>=160）")
else:
    # 若用 min-width: 0 就是原来的 bug
    chk(False, ".setting-info 不能是 min-width:0（这正是竖排 bug 的成因之一）")

chk("flex" in info, ".setting-info 有 flex 属性参与空间分配")

print("\n=== 3. common.css 的全局 input 宽度会不会再打架 ===")

inp = block(common_src, 'input[type="text"]')
chk(bool(inp), '找到 common.css 的 input[type="text"] 规则（含逗号分组写法）')
has_100 = re.search(r"width\s*:\s*100%", inp) is not None
# width:100% 本身没问题（它对普通表单是对的），关键是 admin 侧必须覆盖它
chk(has_100, 'common.css 仍是 width:100%（全局表单默认，合理）')
chk(
    re.search(r"width\s*:\s*auto", si) is not None or has_flex,
    "admin 侧对 .setting-input 做了覆盖，不会被全局 100% 主导",
)

print("\n=== 4. 窄屏降级 ===")

chk("@media" in admin_src, "admin.html 内有媒体查询")

# 断点提取：只看 @media 前面的 at-rule 文本
bps = [int(x) for x in re.findall(r"@media[^{]*?max-width\s*:\s*(\d+)px", admin_src)]
chk(bool(bps), f"存在 max-width 断点（找到 {bps}）")
if bps:
    chk(min(bps) <= 900, f"最小断点 {min(bps)}px 覆盖常见窄屏")

# 窄屏下 .setting-item 是否改为纵向
mbody = media_block(admin_src)
chk(bool(mbody), "能取出 @media 块内容")
if mbody:
    chk(
        ".setting-item" in mbody,
        "媒体查询内有 .setting-item 覆盖规则",
    )
    chk(
        re.search(r"flex-direction\s*:\s*column", mbody) is not None,
        "窄屏下 .setting-item 改为纵向堆叠（避免左右挤压）",
    )
    chk(
        ".setting-input" in mbody,
        "媒体查询内同时调整了 .setting-input（铺满宽度）",
    )
else:
    chk(False, "缺少窄屏下的 .setting-item 降级规则")

print("\n=== 5. 标签 DOM 结构（检查完整 HTML 源码）===")

chk(
    'class="name"' in admin_raw,
    "存在 .name 容器",
)
# 新写法：状态点与文字各占一个 span，便于 flex 对齐
has_dot_span = 'class="status-dot' in admin_raw
chk(has_dot_span, "状态点使用 class 形式（配合 CSS 而非内联样式）")

has_txt_span = 'class="txt"' in admin_raw
chk(has_txt_span, "标签文字包在 .txt span 里（避免与状态点混排被拆行）")

# 确认旧写法已移除：`<div class="name">${dot} ...`
chk(
    re.search(r'class="name">\s*\$\{dot\}', admin_raw) is None,
    "旧的 ${dot} 插值写法已移除",
)

print("\n=== 6. 文字换行策略 ===")

chk(
    "overflow-wrap" in info or "word-break" in info,
    ".setting-info 设置了换行策略（长 key 不溢出）",
)

desc = block(admin_src, ".setting-info .desc")
if desc:
    chk(
        "line-height" in desc,
        ".setting-info .desc 有 line-height（多行更易读）",
    )
else:
    warn(False, "未找到 .setting-info .desc 规则，跳过")

print("\n=== 7. CSS 语法粗检 ===")

for name, src in (("admin.html <style>", admin_src), ("common.css", common_src)):
    ob, cb = src.count("{"), src.count("}")
    chk(ob == cb, f"{name} 花括号配平（{{={ob} }}={cb}）")

print("\n" + "=" * 56)
print(f"通过 {len(passes)} 项   失败 {len(fails)} 项   警告 {len(warns)} 项")
if warns:
    print("\n警告明细：")
    for w in warns:
        print("  -", w)
print("=" * 56)

sys.exit(1 if fails else 0)
