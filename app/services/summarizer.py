"""
AI 总结服务 — DeepSeek 调用 + Map-Reduce 分段总结。

业务参数沿用本地版调优结果，见 app/core/config.py：
    <12000 字      直接总结
    12000-20000   两级 Map-Reduce
    >20000        三级（分组汇总后再汇总）
    chunk_size=6500, overlap=600
    温度：分段 0.3 / 汇总 0.4
    输出比例 35%，下限 1000 字，上限 8000 字
"""
import json
import re
from collections import OrderedDict

import requests

from app.core.config import settings
from app.core.exceptions import AIError
from app.core.logging import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════
# 提示词（沿用本地版，勿随意改动）
# ═══════════════════════════════════════════

SUMMARY_PROMPT = """{title_line}请根据以下语音转写，生成一份结构化总结，控制在 {word_limit} 字以内。

输出时从第一句概括性描述直接开始，严禁任何开场白。

结构要求：
- 开头：一两句话概括核心内容
- 主体：分点列出主要观点（数量不限，不遗漏重要信息）
- 结尾：摘录 1-3 句有价值的原话

禁止事项：
- 禁止输出"好的"、"以下是"、"根据您提供的"、"下面我来"等开场白
- 禁止输出你的身份说明或任务确认语句
- 只基于原文，不添加原文没有的观点

转写：
---
{text}
---"""

CHUNK_SUMMARY_PROMPT = """{title_line}下面是一段长视频转写的片段，请提取其中所有有价值的信息，不要遗漏。

输出时从"本段概要"直接开始，严禁任何开场白。

输出格式：
- 本段概要：2-3 句说明本段讲了什么
- 关键信息：逐条列出观点、论据、案例、步骤、数据、结论
- 原话摘录：1-2 句原汁原味的原话

规则：
1. 信息召回率优先于压缩率——遗漏重要信息的代价高于轻微冗长
2. 保留案例、步骤、数据、结论，不遗漏
3. 不新增原文没有的内容
4. 去掉重复与口头禅
5. 禁止输出"好的"、"以下是本段总结"、"这段内容"等开场白

片段：
---
{text}
---"""

FINAL_SUMMARY_PROMPT = """{title_line}请将以下多段总结整合为一份完整的视频总结，控制在 {word_limit} 字以内。

输出时从核心内容概括直接开始，严禁任何开场白。

结构：
- 核心内容：3-5 句话概括全片
- 主要内容：按主题汇总所有重点，合并重复，不丢细节
- 观点摘录：保留有价值的原话

禁止事项：
- 禁止输出"好的"、"以下是"、"根据以上分段总结"、"整合如下"等开场白
- 禁止输出任务说明或身份确认语句
- 覆盖完整性优先于极度简短
- 不新增原文没有的内容

分段总结：
---
{text}
---"""

# 常见 AI 开场白，自动剔除
_PREAMBLE_PATTERNS = [
    r'^好的[，,]\s*这是根据[您你]提供的[^，,\n]*[，,]\s*',
    r'^好的[，,]\s*以下是[^，,\n]*[：:]\s*',
    r'^好的[，,]\s*下面我来[^，,\n]*[：:]\s*',
    r'^以下是[^，,\n]*的[总结|结构化总结][：:]\s*',
    r'^下面[是给为]您?[^，,\n]*[总结|整理][的]*[：:]\s*',
    r'^根据[您你]提供的[^，,\n]*[，,]?\s*',
    r'^这是根据[^，,\n]*生成[的]*[：:]\s*',
    r'^我已[经]?[为您你]*[^，,\n]*[，,]\s*',
]


def strip_preamble(text: str) -> str:
    for pattern in _PREAMBLE_PATTERNS:
        text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)
    return text.strip()


def _build_title_line(title: str) -> str:
    if title and title.strip() and title.strip() != "未知标题":
        return f"视频标题：{title.strip()}\n\n"
    return ""


def calc_word_limit(text_length: int) -> int:
    """按 35% 比例计算，下限 1000，上限 8000"""
    raw = int(text_length * settings.summary_ratio)
    return max(settings.summary_min_words, min(raw, settings.summary_max_words))


def chunk_text(text: str, chunk_size: int | None = None) -> list[str]:
    """按语义断句切分，段间重叠"""
    chunk_size = chunk_size or settings.chunk_size
    overlap = settings.chunk_overlap

    if len(text) <= chunk_size:
        return [text]

    sentences = re.split(r"(?<=[。！？\n])\s*", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(current) + len(s) <= chunk_size:
            current += s
        else:
            if current:
                chunks.append(current)
                tail = current[-overlap:] if overlap > 0 and len(current) > overlap else ""
                current = tail + s
            else:
                current = s
    if current:
        chunks.append(current)

    return chunks


# ═══════════════════════════════════════════
# DeepSeek 客户端
# ═══════════════════════════════════════════

class DeepSeekClient:
    """无状态调用封装，api_key 由调用方传入（支持用户自带 Key）"""

    def __init__(self, api_key: str, api_url: str = "", model: str = ""):
        if not api_key or not api_key.strip():
            raise AIError("缺少 DeepSeek API Key，请前往设置页填写")
        self.api_key = api_key.strip()
        self.api_url = api_url or settings.deepseek_api_url
        self.model = model or settings.deepseek_model

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _handle_error(self, resp: requests.Response) -> None:
        msg = "API 请求失败"
        try:
            msg = resp.json().get("error", {}).get("message", msg)
        except Exception:
            pass

        if resp.status_code == 401:
            raise AIError("DeepSeek API Key 无效，请检查设置")
        if resp.status_code == 402:
            raise AIError("DeepSeek 账户余额不足")
        if resp.status_code == 429:
            raise AIError("请求过于频繁，请稍后重试")
        raise AIError(f"AI 调用失败：{msg}")

    # ─── 非流式 ───

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.4,
        max_tokens: int = 4000,
        timeout: int = 180,
    ) -> tuple[str, int]:
        """返回 (文本, 消耗 token 数)"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            resp = requests.post(
                self.api_url,
                headers=self._headers(),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            raise AIError("AI 请求超时，请重试")
        except requests.exceptions.RequestException as exc:
            raise AIError(f"AI 请求失败：{exc}")

        if resp.status_code != 200:
            self._handle_error(resp)

        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        return text, tokens

    # ─── 流式 ───

    def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4000,
        timeout: int = 300,
    ):
        """逐块 yield 文本"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            resp = requests.post(
                self.api_url,
                headers=self._headers(),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=timeout,
                stream=True,
            )
        except requests.exceptions.Timeout:
            raise AIError("AI 请求超时")
        except requests.exceptions.RequestException as exc:
            raise AIError(f"AI 请求失败：{exc}")

        if resp.status_code != 200:
            self._handle_error(resp)

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk["choices"][0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


# ═══════════════════════════════════════════
# 总结流程
# ═══════════════════════════════════════════

def summarize_sync(
    client: DeepSeekClient,
    text: str,
    title: str = "",
    on_progress=None,
) -> dict:
    """
    同步总结（Worker 中使用）。

    on_progress(stage_text) — 进度文本回调
    返回 {"summary": str, "tokens": int, "mode": "direct"|"map_reduce"}
    """
    if not text or not text.strip():
        raise AIError("转写内容为空，无法总结")

    length = len(text)

    # 长文本 → Map-Reduce
    if length > settings.map_reduce_threshold:
        return _map_reduce(client, text, title, on_progress)

    # 短文本 → 直接总结
    if on_progress:
        on_progress("生成总结中…")

    word_limit = calc_word_limit(length)
    prompt = SUMMARY_PROMPT.format(
        text=text,
        word_limit=word_limit,
        title_line=_build_title_line(title),
    )

    raw, tokens = client.complete(
        prompt, temperature=0.4, max_tokens=word_limit * 4
    )
    return {
        "summary": strip_preamble(raw),
        "tokens": tokens,
        "mode": "direct",
    }


def summarize_stream(
    client: DeepSeekClient,
    text: str,
    title: str = "",
    on_progress=None,
):
    """
    流式总结。短文本直接流式输出；长文本走 Map-Reduce，
    先 yield 进度提示（以 \x00 开头），最后 yield 正文。
    """
    if not text or not text.strip():
        raise AIError("转写内容为空，无法总结")

    length = len(text)

    if length > settings.map_reduce_threshold:
        result = _map_reduce(client, text, title, on_progress)
        yield result["summary"]
        return

    word_limit = calc_word_limit(length)
    prompt = SUMMARY_PROMPT.format(
        text=text,
        word_limit=word_limit,
        title_line=_build_title_line(title),
    )
    messages = [{"role": "user", "content": prompt}]

    yield from _strip_preamble_stream(
        client.stream(messages, temperature=0.4, max_tokens=word_limit * 4)
    )


def _strip_preamble_stream(chunks):
    """流式输出时先缓冲开头，去掉开场白再逐字输出"""
    buf = ""
    yielded = False

    for chunk in chunks:
        if yielded:
            yield chunk
            continue

        buf += chunk
        if len(buf) > 120 or (chunk and chunk in ("\n", "。", "！", "？", "：", ":")):
            cleaned = strip_preamble(buf)
            yield cleaned if (cleaned != buf and cleaned) else buf
            yielded = True

    if not yielded and buf:
        yield strip_preamble(buf)


def _map_reduce(
    client: DeepSeekClient,
    text: str,
    title: str,
    on_progress=None,
) -> dict:
    """Map-Reduce 总结：分段提炼 → 汇总"""
    chunks = chunk_text(text)
    total = len(chunks)
    total_tokens = 0

    if on_progress:
        on_progress(f"文本 {len(text)} 字，分 {total} 段处理…")

    # ─── Map ───
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(f"处理第 {i + 1}/{total} 段（{len(chunk)} 字）…")
        try:
            piece, tk = client.complete(
                CHUNK_SUMMARY_PROMPT.format(
                    text=chunk, title_line=_build_title_line(title)
                ),
                system="你是一个专业的视频内容分析助手。",
                temperature=0.3,
                max_tokens=1500,
            )
            total_tokens += tk
            chunk_summaries.append(f"## 片段 {i + 1}\n{strip_preamble(piece)}")
        except AIError as exc:
            logger.warning("第 %d 段总结失败: %s", i + 1, exc)
            chunk_summaries.append(f"## 片段 {i + 1}\n[本段总结失败]")

    # ─── Reduce ───
    if len(text) > settings.hierarchical_threshold:
        # 三级：先分组汇总，再最终汇总
        if on_progress:
            on_progress("段数较多，先分组汇总…")

        batch_size = 5
        batched: list[str] = []
        for b in range(0, total, batch_size):
            group = "\n\n".join(chunk_summaries[b:b + batch_size])
            wl = max(500, min(int(len(group) * 0.3), 2000))
            if on_progress:
                on_progress(f"分组汇总 {b // batch_size + 1}…")
            try:
                piece, tk = client.complete(
                    FINAL_SUMMARY_PROMPT.format(
                        text=group, word_limit=wl, title_line=_build_title_line(title)
                    ),
                    temperature=0.4,
                    max_tokens=wl * 4,
                )
                total_tokens += tk
                batched.append(strip_preamble(piece))
            except AIError as exc:
                logger.warning("分组汇总失败: %s", exc)
                batched.append("[分组汇总失败]")
        combined = "\n\n".join(batched)
    else:
        combined = "\n\n".join(chunk_summaries)

    if on_progress:
        on_progress("生成最终总结…")

    word_limit = calc_word_limit(len(text))
    final, tk = client.complete(
        FINAL_SUMMARY_PROMPT.format(
            text=combined, word_limit=word_limit, title_line=_build_title_line(title)
        ),
        temperature=0.4,
        max_tokens=word_limit * 4,
    )
    total_tokens += tk

    return {
        "summary": strip_preamble(final),
        "tokens": total_tokens,
        "mode": "map_reduce",
        "chunks": total,
    }


# ═══════════════════════════════════════════
# 对话
# ═══════════════════════════════════════════

CHAT_SYSTEM_PROMPT = """你是一个内容问答助手。基于提供的资料回答用户问题。

规则：
1. 只依据资料内容回答，资料中没有的信息不要编造
2. 如果资料不足以回答，明确说明"资料中没有相关内容"
3. 回答简洁准确，可用分点
4. 不要输出"根据您提供的资料"这类开场白，直接回答"""


def chat_stream(client: DeepSeekClient, messages: list[dict], context: str = ""):
    """流式问答。context 为检索到的资料，注入到 system 中。"""
    system = CHAT_SYSTEM_PROMPT
    if context:
        system += f"\n\n【参考资料】\n{context}\n【资料结束】"

    full = [{"role": "system", "content": system}] + messages
    yield from client.stream(full, temperature=0.8, max_tokens=2000)


def chat_sync(client: DeepSeekClient, messages: list[dict], context: str = "") -> dict:
    system = CHAT_SYSTEM_PROMPT
    if context:
        system += f"\n\n【参考资料】\n{context}\n【资料结束】"

    full = [{"role": "system", "content": system}] + messages
    payload_messages = full

    # 复用 complete 的请求逻辑
    text, tokens = _complete_messages(client, payload_messages, temperature=0.8, max_tokens=2000)
    return {"reply": text, "tokens": tokens}


def _complete_messages(
    client: DeepSeekClient,
    messages: list[dict],
    *,
    temperature: float = 0.8,
    max_tokens: int = 2000,
) -> tuple[str, int]:
    payload = {
        "model": client.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            client.api_url,
            headers=client._headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=120,
        )
    except requests.exceptions.Timeout:
        raise AIError("AI 请求超时")
    except requests.exceptions.RequestException as exc:
        raise AIError(f"AI 请求失败：{exc}")

    if resp.status_code != 200:
        client._handle_error(resp)

    data = resp.json()
    return data["choices"][0]["message"]["content"], data.get("usage", {}).get("total_tokens", 0)
