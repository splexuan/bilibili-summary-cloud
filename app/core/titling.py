"""
文章标题提取 — 逐字沿用本地版的 _generate_title。

本地版的做法比「取首行前 40 字」讲究得多：
- 扫全部行，不只看开头几行（正文里常有大段版权/导航噪声）
- 去掉 Markdown 的 `#` 前缀
- 至少 4 个字才算标题，避免把 "一、" 这类序号行当成标题
- 超过 30 字时**在标点处截断**（在 10~30 字的范围内找第一个标点），
  而不是直接丢掉这一行 —— 否则遇到长标题会退化成「未命名文章」
"""
import re

# 视为「没提取到标题」的占位名
PLACEHOLDER_TITLES = frozenset({"", "未命名", "未命名文章", "untitled"})

# 标题上限（本地版为 30）
TITLE_MAX_LEN = 30
# 最短标题长度（本地版为 4）
TITLE_MIN_LEN = 4
# 截断时标点搜索的起始位置（本地版从第 10 个字开始找）
TITLE_TRUNCATE_FROM = 10
# 可用于截断的标点
_TITLE_SEPARATORS = "，,。！？：:；;、—"

_MD_HEADING_RE = re.compile(r"^#{1,6}\s*")


def is_placeholder_title(title: str | None) -> bool:
    """标题是否只是占位名（说明当初没提取到、用户也没填）"""
    return (title or "").strip().lower() in PLACEHOLDER_TITLES


def truncate_title(line: str, max_len: int = TITLE_MAX_LEN) -> str:
    """超长标题在标点处截断，找不到合适标点则硬截"""
    if len(line) <= max_len:
        return line

    for sep in _TITLE_SEPARATORS:
        idx = line.find(sep, TITLE_TRUNCATE_FROM)
        if TITLE_TRUNCATE_FROM <= idx <= max_len:
            return line[:idx].strip()
    return line[:max_len]


def generate_title(
    text: str,
    fallback: str = "未命名文章",
    min_len: int = TITLE_MIN_LEN,
    max_len: int = TITLE_MAX_LEN,
) -> str:
    """
    从正文提取标题。找不到合适行时返回 fallback。

    fallback 传 "" 可用于「只想知道有没有更好的标题」的场景。
    """
    for line in (text or "").split("\n"):
        clean = _MD_HEADING_RE.sub("", line.strip()).strip()
        if len(clean) >= min_len:
            return truncate_title(clean, max_len)
    return fallback
