"""
AI 总结服务 — DeepSeek 调用 + （必要时）Map-Reduce 分段总结。

路径选择不再看固定的字数阈值，而是看「一次放下需要多少输出预算」：
推理模型的思考量随输入线性增长，输入过长时预算会顶到接口上限，这时才分段。
    - 放得下 → 单次直连（质量更高：模型看得到全文）
    - 放不下 → 两/三级 Map-Reduce（分段提炼 → 汇总）

原来用的是固定阈值（<12000 直接 / 12000-20000 两级 / >20000 三级），
但实测那条 13803 字的视频（38 分钟）走分段后只产出 20% 的篇幅，
原因见 _guard_chunk_output 的说明。

业务参数沿用本地版调优结果，见 app/core/config.py：
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
# 输出预算
# ═══════════════════════════════════════════

# 推理模型（deepseek-v4-flash / deepseek-reasoner 等）会先消耗大量 token
# 思考，正文预算会被挤掉。本地版的 word_limit * 4 是按**非推理模型**定的：
# 实测同一份 2288 字转写，上限 4000 时 reasoning 就吃掉约 4000（finish=length），
# 正文只剩 0~500 字，加粗/小标题等排版要求完全失效；
# 预留 4096 余量后正文恢复到 1100+ 字，格式也正常。
#
# 对非推理模型不会增加开销：max_tokens 只是上限，它们会提前 finish=stop。
REASONING_TOKEN_ALLOWANCE = 4096

# 中文约 2.1 字符 / token —— 对线上模型实测（1,000,000 字符 = 476,220 tokens）
CHARS_PER_TOKEN = 2.1

# 推理模型的「思考」量随输入增长，不能只留固定余量。
# 实测：6500 字符的分段要 5,471 个思考 token，约 1.76 × 输入 token 数。
# 这里按 4 倍留余量 —— 交叉验证：6500 字符的输入在预算 8192 时正文为 0
# （全被思考吃掉、finish=length），提到 16384 才正常收尾；
# 本公式对该输入给出 1500 + 6500/2.1*4 ≈ 13881，再算上正文预算即超过 16384。
REASONING_TOKEN_RATIO = 4.0

# 实测可用的单次输出上限（max_tokens=65536 能正常收尾）。超过会被接口拒绝，
# 所以预算必须据此封顶，而不是无限加大。
MAX_TOKENS_CEILING = 65536

# 一段产出低于这个字数就视为「没有有效产出」。之所以不设为 0：
# 有些模型会返回一句「好的，以下是总结」之类的开场白但正文为空，
# strip_preamble 不一定剥得干净；正常情况下哪怕 1000 字的分段也能写出几百字。
MIN_CHUNK_OUTPUT = 80


def _reasoning_allowance(input_chars: int) -> int:
    """按输入长度估算推理模型需要的思考余量"""
    if input_chars <= 0:
        return REASONING_TOKEN_ALLOWANCE
    est = int(input_chars / CHARS_PER_TOKEN * REASONING_TOKEN_RATIO)
    return max(REASONING_TOKEN_ALLOWANCE, est)


def summary_max_tokens(base: int, input_chars: int = 0) -> int:
    """
    正文预算 base + 随输入增长的思考余量，并封顶在接口上限内。

    input_chars 不传时退化为旧行为（只加固定余量），便于渐进迁移。
    """
    return min(base + _reasoning_allowance(input_chars), MAX_TOKENS_CEILING)


# ═══════════════════════════════════════════
# 输出校正
# ═══════════════════════════════════════════

_HEADING_LINE_RE = re.compile(r"^(#{2,4})[ \t]+(.+)$")
_HEADING_MAX_LEN = 30      # 超过这个长度的「## 行」几乎肯定粘了正文
_HEADING_COLON_LIMIT = 20  # 在这之前找冒号作为标题终点


def normalize_markdown_headings(
    text: str, max_len: int = _HEADING_MAX_LEN, colon_limit: int = _HEADING_COLON_LIMIT
) -> str:
    """
    修正「## 标题」与正文粘在同一行的情况。

    实测流式调用下模型偶尔会把小标题和正文写在一行
    （非流式没遇到），marked 会把整行渲染成二级标题，
    正文变成一大坨加粗大字，非常难看。

    有冒号时按冒号切开（模型的小标题习惯带「：」）；
    没有可用分隔符时降级成加粗段落 —— 宁可少一个标题，
    也不要让一整段正文糊成标题。
    """
    if not text:
        return text

    lines = []
    for line in text.split("\n"):
        m = _HEADING_LINE_RE.match(line)
        if not m:
            lines.append(line)
            continue

        body = m.group(2).strip()
        if len(body) <= max_len:
            lines.append(line)
            continue

        head = body[:colon_limit]
        idx = max(head.rfind("："), head.rfind(":"))
        if idx > 0:
            lines.append(f"{m.group(1)} {body[: idx + 1]}")
            lines.append(body[idx + 1:].strip())
        else:
            lines.append(f"**{body}**")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# 提示词（沿用本地版，勿随意改动）
# ═══════════════════════════════════════════

SUMMARY_PROMPT = """{title_line}请根据以下语音转写，生成一份结构化总结，控制在 {word_limit} 字以内。

输出时从第一句概括性描述直接开始，严禁任何开场白。

内容取舍：
- 广告、赞助、恰饭、带货推广（口播广告、优惠券/口令、App 下载引导、商品链接引导等）不纳入总结
- 不要为广告单开章节，也不要提及广告主、商品名或促销信息
- 被广告打断的正文内容要照常保留，只剔除广告本身
- 只剔除明确的广告推广，不要把正常的观点、案例、举例误判成广告

结构要求：
- 开头：一两句话概括核心内容
- 主体：分点列出主要观点（数量不限，不遗漏重要信息）
- 结尾：摘录 1-3 句有价值的原话

排版要求（Markdown，前端按 Markdown 渲染，务必遵守）：
- 每个分点的标题必须独占一行：先写 "## 标题"，换行后再写正文
- 标题行内不要接正文内容，标题与正文之间必须换行
- 关键结论、关键数字、专有名词用 **双星号加粗**，每段控制在 2-3 处
- 不要输出代码块、表格和分隔线

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

内容取舍：
- 广告、赞助、恰饭、带货推广（口播广告、优惠券/口令、App 下载引导、商品链接引导等）不纳入总结
- 不要为广告单开章节，也不要提及广告主、商品名或促销信息
- 被广告打断的正文内容要照常保留，只剔除广告本身
- 只剔除明确的广告推广，不要把正常的观点、案例、举例误判成广告

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

内容取舍：
- 广告、赞助、恰饭、带货推广（口播广告、优惠券/口令、App 下载引导、商品链接引导等）不纳入总结
- 不要为广告单开章节，也不要提及广告主、商品名或促销信息
- 被广告打断的正文内容要照常保留，只剔除广告本身
- 只剔除明确的广告推广，不要把正常的观点、案例、举例误判成广告

结构：
- 核心内容：3-5 句话概括全片
- 主要内容：按主题汇总所有重点，合并重复，不丢细节
- 观点摘录：保留有价值的原话

排版要求（Markdown，前端按 Markdown 渲染，务必遵守）：
- 每个主题的标题单独成行，用 "## " 开头，例如 "## 一、事件始末"
- 关键结论、关键数字、专有名词用 **双星号加粗**，每段控制在 2-3 处
- 不要输出代码块、表格和分隔线

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
    # ⚠️ 这两条本地版写成了 [总结|结构化总结] / [总结|整理]，
    # 那是**字符集**不是分组，等价于「匹配 总/结/|/整/理 中的任意一个字」，
    # 于是「以下是对该视频的总结：」这类最常见的开场白永远剥不掉。
    # 这里改成 (?:…) 才符合原意；其余 6 条与本地版逐字一致。
    r'^以下是[^，,\n]*的(?:总结|结构化总结)[：:]\s*',
    r'^下面[是给为]您?[^，,\n]*(?:总结|整理)[的]*[：:]\s*',
    r'^根据[您你]提供的[^，,\n]*[，,]?\s*',
    r'^这是根据[^，,\n]*生成[的]*[：:]\s*',
    r'^我已[经]?[为您你]*[^，,\n]*[，,]\s*',
]


def strip_preamble_leading(text: str) -> str:
    """
    只去掉开头的开场白，**保留结尾原样**。

    流式输出时必须用这个而不是 strip_preamble()：缓冲区是在流的中间被
    刷出去的，rstrip 会把缓冲区末尾的换行一起吃掉，下一个片段接上来就被
    粘成一行。实测症状是「第一个小标题和正文挤在同一行」——
    因为只有第一次刷新会踩到这个边界，之后的片段都是原样透传。
    """
    for pattern in _PREAMBLE_PATTERNS:
        text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)
    return text.lstrip()


def strip_preamble(text: str) -> str:
    """完整文本用：去掉开场白并清理首尾空白"""
    return strip_preamble_leading(text).rstrip()


def _build_title_line(title: str) -> str:
    if title and title.strip() and title.strip() != "未知标题":
        return f"视频标题：{title.strip()}\n\n"
    return ""


def calc_word_limit(text_length: int) -> int:
    """按 35% 比例计算，下限 1000，上限 8000"""
    raw = int(text_length * settings.summary_ratio)
    return max(settings.summary_min_words, min(raw, settings.summary_max_words))


def single_shot_fits(text_len: int) -> bool:
    """
    这段文本能否一次喂完。

    单次调用质量更高 —— 模型能看到全文、不会像分段那样先各自压缩再汇总，
    所以只要能放下就不分段。判断依据是所需 max_tokens 是否落在接口上限内：
    推理模型的思考量随输入增长，输入太长时预算必然不够，这时才退到分段。

    以线上实测值估算：约 25000 字符（≈1 小时视频）以内都能单次处理。
    """
    base = calc_word_limit(text_len) * 4
    return base + _reasoning_allowance(text_len) <= MAX_TOKENS_CEILING


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
        usage_sink: dict | None = None,
    ):
        """
        逐块 yield 文本。

        usage_sink：传入可变 dict（如 {}），流结束时会被填入服务端返回的 usage，
        用于流式调用也能统计 token 用量（OpenAI 兼容的 include_usage）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if usage_sink is not None:
            payload["stream_options"] = {"include_usage": True}

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
            except json.JSONDecodeError:
                continue

            # include_usage 时最后一个 chunk 只有 usage、没有 choices
            usage = chunk.get("usage")
            if usage and usage_sink is not None:
                usage_sink.clear()
                usage_sink.update(usage)

            choices = chunk.get("choices") or []
            if not choices:
                continue

            # 推理模型把预算耗尽时正文会被截断甚至为空，这里留一条线索
            if choices[0].get("finish_reason") == "length":
                logger.warning(
                    "AI 输出被 max_tokens=%s 截断（推理模型会先吃掉大量预算）", max_tokens
                )

            content = (choices[0].get("delta") or {}).get("content", "")
            if content:
                yield content


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

    # 只有单次放不下才分段；放得下就走直连（模型看得到全文，质量更高）
    if not single_shot_fits(length):
        return _map_reduce(client, text, title, on_progress)

    if on_progress:
        on_progress("生成总结中…")

    word_limit = calc_word_limit(length)
    prompt = SUMMARY_PROMPT.format(
        text=text,
        word_limit=word_limit,
        title_line=_build_title_line(title),
    )

    raw, tokens = client.complete(
        prompt,
        temperature=0.4,
        max_tokens=summary_max_tokens(word_limit * 4, length),
    )
    return {
        "summary": normalize_markdown_headings(
            _require_output(strip_preamble(raw), "总结")
        ),
        "tokens": tokens,
        "mode": "direct",
    }


def summarize_stream(
    client: DeepSeekClient,
    text: str,
    title: str = "",
    on_progress=None,
    usage_sink: dict | None = None,
):
    """
    流式总结。短文本直接流式输出；长文本走 Map-Reduce，
    先通过 on_progress 汇报进度，最后 yield 正文。

    本地版就有的能力，云端版此前只移植了函数、没人调用（Worker 走同步版），
    结果用户只能看到「AI 总结中」干等到结束。现在由 Worker 消费它做实时显字。
    """
    if not text or not text.strip():
        raise AIError("转写内容为空，无法总结")

    length = len(text)

    if not single_shot_fits(length):
        result = _map_reduce(client, text, title, on_progress)
        if usage_sink is not None:
            usage_sink.clear()
            usage_sink["total_tokens"] = result.get("tokens", 0)
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
        client.stream(
            messages,
            temperature=0.4,
            max_tokens=summary_max_tokens(word_limit * 4, length),
            usage_sink=usage_sink,
        )
    )


def summarize_stream_collect(
    client: DeepSeekClient,
    text: str,
    title: str = "",
    on_progress=None,
    on_chunk=None,
) -> dict:
    """
    流式总结 + 收集结果，供 Worker 使用。

    on_chunk(chunk)：每产出一段就回调（用于推送到前端实时显字）。
    返回 {"summary", "tokens", "mode"} —— 与 summarize_sync 保持一致，
    这样调用方切换实现时无需改动其余逻辑。
    """
    usage: dict = {}
    parts: list[str] = []

    for chunk in summarize_stream(
        client, text, title, on_progress, usage_sink=usage
    ):
        if not chunk:
            continue
        parts.append(chunk)
        if on_chunk:
            on_chunk(chunk)

    summary = normalize_markdown_headings("".join(parts))
    if not summary.strip():
        # 最常见的原因是推理模型把全部预算花在思考上（无正文输出）
        raise AIError(
            "AI 没有返回正文内容。若是推理模型（如 deepseek-v4-flash），"
            "通常是思考占满了 max_tokens，请调大预算或更换模型后重试"
        )

    mode = "direct" if single_shot_fits(len(text)) else "map_reduce"
    # direct 模式的 token 来自 include_usage；map_reduce 由 _map_reduce 累加后写入 sink
    tokens = int(usage.get("total_tokens") or 0)

    return {"summary": summary, "tokens": tokens, "mode": mode}


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
            # 用 *leading* 版本：缓冲区结尾可能正好停在小标题后面，
            # 这里一旦 rstrip 就会把后面的正文粘到标题行上
            cleaned = strip_preamble_leading(buf)
            yield cleaned if (cleaned != buf and cleaned) else buf
            yielded = True

    if not yielded and buf:
        yield strip_preamble(buf)


def _require_output(piece: str, label: str) -> str:
    """
    空产出检查 —— 这是本文件里最容易被忽略、后果最严重的一处。

    推理模型在预算不足时会把 token 全部花在思考上，正文为 0、finish_reason=length。
    这只是「返回值很短」，**不会抛异常**。旧代码因此把 `## 片段 3\\n`
    （后面什么都没有）原样拼进最终汇总的输入，那一段内容就被静默丢弃了：
    实测 13803 字的视频 3 段里有 2 段产出为空，最终总结只剩 20% 的篇幅、
    约 94% 的内容没进总结，而用户和管理员都完全看不出来。

    这里宁可直接失败（用户能重试、能换模型），也不给一份悄悄少了内容的总结。
    """
    body = (piece or "").strip()
    if len(body) >= MIN_CHUNK_OUTPUT:
        return body
    raise AIError(
        f"{label}没有产出内容（只返回了 {len(body)} 字），为避免总结缺失内容已中止。"
        "通常是因为推理模型把输出预算全用在思考上，请更换非推理模型"
        "（如 deepseek-chat）或调大预算后重试"
    )


def _map_one(
    client: DeepSeekClient, chunk: str, title: str, label: str
) -> tuple[str, int]:
    """
    提炼单个分段，返回 (文本, 消耗 tokens)。

    预算必须跟着分段长度走：原来固定 summary_max_tokens(1500) = 5596，
    而 6500 字的分段光思考就要 5471 个 token（实测），正文一个都分不到。
    这里按输入长度给足；万一仍为空产出，就用接口上限再试一次。
    """
    prompt = CHUNK_SUMMARY_PROMPT.format(
        text=chunk, title_line=_build_title_line(title)
    )
    # 分段任务是「忠实提炼」，产出通常 2000~4000 字，正文预算给足
    budgets = (summary_max_tokens(6000, len(chunk)), MAX_TOKENS_CEILING)
    spent = 0
    piece = ""

    for attempt, budget in enumerate(budgets):
        last = attempt == len(budgets) - 1
        try:
            raw, tk = client.complete(
                prompt,
                system="你是一个专业的视频内容分析助手。",
                temperature=0.3,
                max_tokens=budget,
            )
        except AIError as exc:
            if last:
                raise
            logger.warning("%s调用失败，用最大预算重试: %s", label, exc)
            continue

        spent += tk
        piece = strip_preamble(raw)
        if len(piece.strip()) >= MIN_CHUNK_OUTPUT:
            return piece, spent

        logger.warning(
            "%s产出为空（%d 字，预算 %d），提高预算重试", label, len(piece), budget
        )

    return _require_output(piece, label), spent


def _map_reduce(
    client: DeepSeekClient,
    text: str,
    title: str,
    on_progress=None,
) -> dict:
    """Map-Reduce 总结：分段提炼 → 汇总（仅在单次放不下时才走这里）"""
    chunks = chunk_text(text)
    total = len(chunks)
    total_tokens = 0

    if on_progress:
        on_progress(f"文本 {len(text)} 字，超出单次处理能力，分 {total} 段处理…")

    # ─── Map ───
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(f"处理第 {i + 1}/{total} 段（{len(chunk)} 字）…")
        piece, tk = _map_one(client, chunk, title, f"第 {i + 1}/{total} 段")
        total_tokens += tk
        chunk_summaries.append(f"## 片段 {i + 1}\n{piece}")

    # ─── Reduce ───
    # 是否需要中间那一层「分组汇总」，同样按预算判断而不是按固定字数：
    # 分段总结合起来如果还放得下，就直接一次性汇总（少一层压缩、少一次信息损耗）。
    combined_raw = "\n\n".join(chunk_summaries)
    if total > 1 and not single_shot_fits(len(combined_raw)):
        # 三级：先分组汇总，再最终汇总
        if on_progress:
            on_progress("分段总结合并后仍超出单次能力，先分组汇总…")

        batch_size = 5
        batched: list[str] = []
        for b in range(0, total, batch_size):
            g = b // batch_size + 1
            group = "\n\n".join(chunk_summaries[b:b + batch_size])
            wl = max(500, min(int(len(group) * 0.3), 2000))
            if on_progress:
                on_progress(f"分组汇总 {g}…")
            piece, tk = client.complete(
                FINAL_SUMMARY_PROMPT.format(
                    text=group, word_limit=wl, title_line=_build_title_line(title)
                ),
                temperature=0.4,
                max_tokens=summary_max_tokens(wl * 4, len(group)),
            )
            total_tokens += tk
            batched.append(_require_output(strip_preamble(piece), f"第 {g} 组汇总"))
        combined = "\n\n".join(batched)
    else:
        combined = combined_raw

    if on_progress:
        on_progress("生成最终总结…")

    word_limit = calc_word_limit(len(text))
    final, tk = client.complete(
        FINAL_SUMMARY_PROMPT.format(
            text=combined, word_limit=word_limit, title_line=_build_title_line(title)
        ),
        temperature=0.4,
        max_tokens=summary_max_tokens(word_limit * 4, len(combined)),
    )
    total_tokens += tk

    return {
        "summary": normalize_markdown_headings(
            _require_output(strip_preamble(final), "最终汇总")
        ),
        "tokens": total_tokens,
        "mode": "map_reduce",
        "chunks": total,
    }


# ═══════════════════════════════════════════
# 对话（提示词逐字沿用本地版，勿随意改动）
# ═══════════════════════════════════════════

# 单内容问答：人设 + 资料注入结构（## AI 总结 / ## 相关原文段落 / 转写开头兜底）
CONTENT_CHAT_SYSTEM_PROMPT = (
    "你是一个视频内容讨论助手。请基于以下信息回答用户问题，尽量引用原文内容。"
    "如果信息不足以回答，诚实说明。\n\n"
)

# 跨内容知识库问答：输出格式要求（加粗标题分点 / 标注来源 / 留空行）
KB_CHAT_SYSTEM_PROMPT = (
    "你是一个知识库助手。回复要求：1) 用加粗标题分点，每点简明扼要 "
    "2) 关键结论用**加粗**突出 3) 每个观点标注来源 4) 段落间留空行 "
    "5) 不要客套话，直接给答案。"
)

KB_CHAT_USER_PROMPT = """以下是多个视频/文章的相关原文。请尽量综合所有来源回答，每个来源都标注。如有冲突观点也要说明。直接回答，标注来源。

## 相关原文
{context}

## 用户问题
{question}

请回答："""

# 多轮追问的查询改写（短追问直接检索命中率低，先改写成完整查询）
QUERY_REWRITE_SYSTEM_PROMPT = "你是查询改写器，只输出改写后的查询语句，不要解释。"

QUERY_REWRITE_PROMPT = """根据对话上下文，把用户的追问改写成一个完整、清晰的搜索查询语句（30字以内）。

{history}用户追问: {question}
改写后的查询:"""

# 触发查询改写的追问字数上限（本地版阈值）
REWRITE_MAX_QUESTION_LEN = 15
# 注入的对话历史条数（本地版取末尾 4 条，每条截断 300 字）
REWRITE_HISTORY_TURNS = 4
REWRITE_HISTORY_TRUNC = 300
# 知识库历史消息的截断长度（本地版）
KB_HISTORY_TRUNC = 2000
# 单内容无 RAG 命中时，用转写开头兜底的字符数（本地版）
TRANSCRIPT_FALLBACK_CHARS = 3000


def build_content_system_prompt(summary: str, context: str, transcript: str) -> str:
    """
    单内容问答的 system 提示（结构逐字沿用本地版）。

    有 RAG 命中 → 注入「## 相关原文段落」；无命中 → 用「## 转写开头」兜底，
    避免资料为空时模型只能凭空回答。
    """
    prompt = CONTENT_CHAT_SYSTEM_PROMPT
    if summary:
        prompt += f"## AI 总结\n{summary}\n\n"
    if context:
        prompt += f"## 相关原文段落\n{context}\n"
    elif transcript:
        prompt += f"## 转写开头\n{transcript[:TRANSCRIPT_FALLBACK_CHARS]}\n"
    return prompt


def build_kb_user_prompt(context: str, question: str) -> str:
    return KB_CHAT_USER_PROMPT.format(context=context, question=question)


def rewrite_query(client: DeepSeekClient, question: str, history: list[dict]) -> str:
    """
    把短追问改写成完整查询；不满足条件或失败时返回原问题。
    失败只降级、不阻断问答。
    """
    if not history or len(question) > REWRITE_MAX_QUESTION_LEN:
        return question

    lines = ""
    for msg in history[-REWRITE_HISTORY_TURNS:]:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if content:
            lines += f"{'用户' if msg['role'] == 'user' else 'AI'}: {content[:REWRITE_HISTORY_TRUNC]}\n"

    if not lines:
        return question

    prompt = QUERY_REWRITE_PROMPT.format(history=lines, question=question)
    try:
        rewritten, _ = client.complete(
            prompt, system=QUERY_REWRITE_SYSTEM_PROMPT, temperature=0.8, max_tokens=2000
        )
        rewritten = rewritten.strip()
        if rewritten and len(rewritten) > 2:
            logger.info("查询重写: '%s' → '%s'", question, rewritten)
            return rewritten
    except AIError as exc:
        logger.warning("查询重写失败，用原问题检索: %s", exc)
    return question


def chat_stream(client: DeepSeekClient, messages: list[dict], system: str = ""):
    """流式问答。system 由调用方按场景拼装（单内容 / 知识库）。"""
    full = ([{"role": "system", "content": system}] if system else []) + messages
    yield from client.stream(full, temperature=0.8, max_tokens=2000)


def chat_sync(client: DeepSeekClient, messages: list[dict], system: str = "") -> dict:
    full = ([{"role": "system", "content": system}] if system else []) + messages
    text, tokens = _complete_messages(client, full, temperature=0.8, max_tokens=2000)
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
