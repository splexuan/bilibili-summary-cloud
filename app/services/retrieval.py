"""
检索服务 — 两级检索。

第一级：跨内容知识库粗筛（BM25 + TF-IDF 混合），从所有总结中选出最相关的 N 个内容
第二级：单内容 RAG 精查（TF-IDF 字符级 ngram），从选中内容的原文中找出最相关段落

参数沿用本地版：
- 混合权重 BM25 0.5 / TF-IDF 0.5
- RAG 用 char_wb ngram (2,4)，max_features=5000
- 粗筛 Top5 → 精查
"""
import hashlib
import re
from collections import OrderedDict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.core.logging import get_logger

logger = get_logger(__name__)

# ─── 调优参数（沿用本地版）───
BM25_WEIGHT = 0.5
RAG_TOP_K = 5
RAG_MAX_FEATURES = 5000
RAG_CACHE_MAX = 32


# ═══════════════════════════════════════════
# 段落切分
# ═══════════════════════════════════════════

def split_paragraphs(text: str, min_chars: int = 10) -> list[str]:
    """将全文切分为段落：优先按空行，段落太少时按句号切"""
    parts = re.split(r"\n\s*\n", text)
    paragraphs: list[str] = []

    for p in parts:
        p = p.strip()
        if len(p) >= min_chars:
            paragraphs.append(p)
        elif paragraphs:
            paragraphs[-1] += "\n" + p
        elif p:
            paragraphs.append(p)

    # 段落太少 → 用句号强行切
    if len(paragraphs) < 3:
        raw = "\n".join(paragraphs)
        paragraphs = [
            s.strip() + "。"
            for s in raw.split("。")
            if len(s.strip()) >= min_chars
        ]

    return [p for p in paragraphs if len(p) >= min_chars]


# ═══════════════════════════════════════════
# 单内容 RAG
# ═══════════════════════════════════════════

class RAGEngine:
    """单内容语义检索：TF-IDF 字符级 ngram + 余弦相似度"""

    def __init__(self):
        self.paragraphs: list[str] = []
        self.matrix = None
        self.vectorizer: TfidfVectorizer | None = None

    def build(self, text: str) -> "RAGEngine":
        self.paragraphs = split_paragraphs(text)
        if not self.paragraphs:
            return self

        self.vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            max_features=RAG_MAX_FEATURES,
        )
        self.matrix = self.vectorizer.fit_transform(self.paragraphs)
        return self

    def search(self, query: str, top_k: int = RAG_TOP_K) -> list[str]:
        if self.matrix is None or not self.paragraphs:
            return []

        try:
            q_vec = self.vectorizer.transform([query])
            scores = cosine_similarity(q_vec, self.matrix)[0]
        except Exception as exc:
            logger.warning("RAG 检索失败: %s", exc)
            return []

        top_idx = np.argsort(scores)[-top_k:][::-1]
        return [self.paragraphs[i] for i in top_idx if scores[i] > 0]

    def search_scored(self, query: str, top_k: int = RAG_TOP_K) -> list[tuple[str, float]]:
        """返回带分数的结果，用于过滤低相关段落"""
        if self.matrix is None or not self.paragraphs:
            return []

        try:
            q_vec = self.vectorizer.transform([query])
            scores = cosine_similarity(q_vec, self.matrix)[0]
        except Exception:
            return []

        top_idx = np.argsort(scores)[-top_k:][::-1]
        hits = [(self.paragraphs[i], float(scores[i])) for i in top_idx if scores[i] > 0]

        # 剔除低于最高分 10% 的段落（沿用本地版策略）
        if hits:
            threshold = hits[0][1] * 0.1
            hits = [h for h in hits if h[1] >= threshold]
        return hits


# 进程内缓存（LRU，防止内存无上限增长）
_rag_cache: OrderedDict[str, tuple[str, RAGEngine]] = OrderedDict()


def _fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def rag_search(vid: str, transcript: str, query: str, top_k: int = RAG_TOP_K) -> list[str]:
    """在单个视频/文章的原文中检索相关段落"""
    if not transcript:
        return []

    fp = _fingerprint(transcript)
    cached = _rag_cache.get(vid)

    if cached and cached[0] == fp:
        _rag_cache.move_to_end(vid)
        engine = cached[1]
    else:
        engine = RAGEngine().build(transcript)
        _rag_cache[vid] = (fp, engine)
        _rag_cache.move_to_end(vid)
        while len(_rag_cache) > RAG_CACHE_MAX:
            _rag_cache.popitem(last=False)

    return engine.search(query, top_k)


def clear_rag_cache(vid: str | None = None) -> None:
    if vid:
        _rag_cache.pop(vid, None)
    else:
        _rag_cache.clear()


# ═══════════════════════════════════════════
# 跨内容知识库索引
# ═══════════════════════════════════════════

class KBIndex:
    """
    知识库混合索引：BM25（关键词）+ TF-IDF（模糊）。

    索引对象是「每一条内容的总结」，用于粗筛出最相关的内容，
    再由调用方对这些内容做原文精查。
    """

    def __init__(self):
        self.keys: list[str] = []          # 对象标识，如 "v:12" / "a:34"
        self.meta: list[dict] = []         # 附加信息（标题等）
        self.vectorizer: TfidfVectorizer | None = None
        self.matrix = None
        self.tokenized: list[list[str]] = []

    def build(self, items: list[dict]) -> "KBIndex":
        """
        items: [{"key": "v:12", "text": "总结内容", "title": "标题", "kind": "video"}, ...]
        """
        items = [it for it in items if (it.get("text") or "").strip()]
        if not items:
            return self

        self.keys = [it["key"] for it in items]
        self.meta = items
        texts = [it["text"].strip() for it in items]

        # TF-IDF：字符级 ngram，对中文友好
        self.vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
        )
        self.matrix = self.vectorizer.fit_transform(texts)

        # BM25 分词：中文按字切分（无分词器依赖），英文保留单词
        self.tokenized = [_tokenize(t) for t in texts]

        return self

    def rank(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """混合排序，返回 [(key, score), ...] 降序"""
        if not self.keys:
            return []

        bm25_scores = self._bm25(query)
        tfidf_scores = self._tfidf(query)

        merged = []
        for key in self.keys:
            s = BM25_WEIGHT * bm25_scores.get(key, 0.0) + (1 - BM25_WEIGHT) * tfidf_scores.get(key, 0.0)
            merged.append((key, s))

        merged.sort(key=lambda x: x[1], reverse=True)
        return merged[:top_k]

    def _bm25(self, query: str) -> dict[str, float]:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return {}

        try:
            bm25 = BM25Okapi(self.tokenized)
            raw = bm25.get_scores(_tokenize(query))
            mx = float(raw.max()) if len(raw) else 0.0
            if mx > 0:
                raw = raw / mx
            return {self.keys[i]: float(raw[i]) for i in range(len(raw))}
        except Exception as exc:
            logger.warning("BM25 计算失败: %s", exc)
            return {}

    def _tfidf(self, query: str) -> dict[str, float]:
        if self.matrix is None:
            return {}
        try:
            q_vec = self.vectorizer.transform([query])
            scores = cosine_similarity(q_vec, self.matrix)[0]
            return {self.keys[i]: float(scores[i]) for i in range(len(scores))}
        except Exception as exc:
            logger.warning("TF-IDF 计算失败: %s", exc)
            return {}


def _tokenize(text: str) -> list[str]:
    """中文按字 + 英文按词，够用且无外部依赖"""
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    return chars + words


# 知识库索引缓存（按用户 + 内容指纹）
_kb_cache: dict[str, tuple[str, KBIndex]] = {}


def build_kb_index(user_id: int, items: list[dict]) -> KBIndex:
    """构建知识库索引（带指纹缓存，内容未变则复用）"""
    fp = hashlib.sha1(
        "|".join(f"{it['key']}:{len(it.get('text') or '')}" for it in items).encode()
    ).hexdigest()

    cache_key = f"u{user_id}"
    cached = _kb_cache.get(cache_key)
    if cached and cached[0] == fp:
        return cached[1]

    index = KBIndex().build(items)
    _kb_cache[cache_key] = (fp, index)
    return index


def clear_kb_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _kb_cache.clear()
    else:
        _kb_cache.pop(f"u{user_id}", None)
