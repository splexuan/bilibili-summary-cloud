"""
文本朗读 — 用 Edge TTS 把总结合成 MP3。

本地版就有这个功能（/api/tts），云端版此前没移植。与本地版的差异：
- 全程在内存里合成，不落临时文件（本地版写 tempfile 后 send_file）
- 校验音色白名单，并限制单次合成字数，避免被当成免费 TTS 网关滥用
- 生成前把 Markdown 标记去掉，否则会把 `#`、`**` 一起念出来
"""
import asyncio
import re

import edge_tts
from fastapi import APIRouter
from fastapi.responses import Response

from app.api.deps import CurrentUser
from app.api.schemas import TTSReq
from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["朗读"])

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 允许的音色（普通话常用几个，避免任意传参）
ALLOWED_VOICES = frozenset({
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural",
})

# 单次合成上限：5 分钟左右的音频，足够读完一份总结
MAX_TTS_CHARS = 5000
SYNTH_TIMEOUT = 120

_MD_PATTERNS = (
    (re.compile(r"```.*?```", re.S), ""),          # 代码块
    (re.compile(r"`([^`]*)`"), r"\1"),             # 行内代码
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),     # 图片
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # 链接保留文字
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # 标题符号
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),       # 引用符号
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),       # 列表符号
    (re.compile(r"(\*\*|__|\*|_|~~)"), ""),        # 强调标记
    (re.compile(r"^\s*[-|: ]{3,}\s*$", re.M), ""),  # 表格分隔行
)


def to_plain_text(markdown: str) -> str:
    """去掉 Markdown 标记，只留可朗读的文字"""
    text = (markdown or "").replace("|", " ")
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    return re.sub(r"\n{2,}", "\n", text).strip()


async def _collect_audio(text: str, voice: str, rate: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


@router.post("/tts")
async def synthesize(req: TTSReq, user: CurrentUser):
    """合成语音，直接返回 MP3 字节流"""
    text = to_plain_text(req.text)
    if not text:
        raise AppError("没有可朗读的文本")

    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS]

    voice = req.voice if req.voice in ALLOWED_VOICES else DEFAULT_VOICE
    rate = req.rate if req.rate in ("-50%", "-25%", "+0%", "+25%", "+50%") else "+0%"

    try:
        audio = await asyncio.wait_for(
            _collect_audio(text, voice, rate), timeout=SYNTH_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise AppError("语音合成超时，请缩短文本后重试") from None
    except Exception as exc:
        logger.warning("语音合成失败 user=%s voice=%s: %s", user.id, voice, exc)
        raise AppError("语音合成失败，请稍后重试") from exc

    if not audio:
        raise AppError("语音合成失败：未返回音频数据")

    logger.info("语音合成完成 user=%s 字数=%d 字节=%d", user.id, len(text), len(audio))
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
