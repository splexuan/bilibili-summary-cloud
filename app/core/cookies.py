"""
B站 Cookie 解析 — 用户粘贴的是浏览器里的 `k=v; k=v` 字符串，
而 yt-dlp 的 --cookies 只认 Netscape cookie 文件（7 列、Tab 分隔）。

直接落盘原始字符串会被 yt-dlp 判定 `skipping cookie file entry due to
invalid length 1` 并整行跳过，等于没带 Cookie，表现为「解析失败」。

放在 core 而非 workers：API 层保存 Cookie 时也要用它回报有效条数。
"""

# Netscape 文件头（yt-dlp 认得，纯注释行）
_HEADER = (
    "# Netscape HTTP Cookie File",
    "# 由 bilibili-summary-cloud 自动生成，请勿手工编辑",
    "",
)


def parse_cookie_pairs(content: str) -> list[tuple[str, str]]:
    """
    解析 `k=v; k=v` 形式，返回 (name, value) 列表。

    跳过空值项：本地版同样会丢弃 `if not name or not value`。
    空值 Cookie 对登录态没有任何作用，写进文件只会徒增噪声。
    """
    pairs: list[tuple[str, str]] = []
    for part in (content or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        pairs.append((name, value))
    return pairs


def is_netscape_file(content: str) -> bool:
    """判断是否已是 yt-dlp / 浏览器插件导出的 Netscape 文件"""
    text = (content or "").strip()
    return "\t" in text and ("# Netscape" in text or "HTTP Cookie File" in text)


def count_entries(content: str) -> int:
    """有效 Cookie 条数（已是 Netscape 文件时数数据行）"""
    text = (content or "").strip()
    if not text:
        return 0
    if is_netscape_file(text):
        return sum(
            1
            for line in text.split("\n")
            if line.strip() and not line.startswith("#") and "\t" in line
        )
    return len(parse_cookie_pairs(text))


def to_netscape(content: str, domain: str = ".bilibili.com") -> str:
    """
    转成 Netscape 格式；无有效项时返回空串（调用方据此判断"没 Cookie"）。
    已经是 Netscape 文件的原样返回。
    """
    text = (content or "").strip()
    if not text:
        return ""

    if is_netscape_file(text):
        return text if text.endswith("\n") else text + "\n"

    lines = list(_HEADER)
    for name, value in parse_cookie_pairs(text):
        # domain  include_subdomains  path  secure  expiry  name  value
        # expiry=0 表示会话 Cookie，yt-dlp 接受
        lines.append("\t".join([domain, "TRUE", "/", "TRUE", "0", name, value]))

    if len(lines) <= len(_HEADER):
        return ""
    return "\n".join(lines) + "\n"
