"""
用户端问答接口 — 单内容 RAG 问答 + 跨内容知识库问答。

检索架构（两级）：
    知识库粗筛：BM25 + TF-IDF 混合，从所有总结中选出 Top N 相关内容
    原文精查：对选中的内容做字符级 ngram RAG，找出最相关段落
    → 拼装上下文 → DeepSeek 回答
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import ChatReq, ChatSaveReq, KBChatReq, Ok
from app.core.constants import ChatKind
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models import Article, Chat, Video
from app.db.settings_repo import get_deepseek_config
from app.services.retrieval import (
    build_kb_index,
    rag_search,
)
from app.services.summarizer import DeepSeekClient, chat_stream

logger = get_logger(__name__)
router = APIRouter(prefix="/api/chat", tags=["问答"])

# 知识库粗筛数量
KB_TOP_N = 5
# 单次注入的上下文最大字符数（控制 token 消耗）
MAX_CONTEXT_CHARS = 12000


async def _get_client(db, user) -> DeepSeekClient:
    """构建 AI 客户端：用户自带 Key 优先"""
    api_url, global_key, model = await get_deepseek_config(db)

    if user.deepseek_key_enc and user.can_use_own_key:
        from app.core.security import decrypt_secret

        own = decrypt_secret(user.deepseek_key_enc)
        if own:
            return DeepSeekClient(own, api_url, model)

    if not global_key:
        from app.core.exceptions import ConfigError

        raise ConfigError("未配置 DeepSeek API Key，请在设置页填写或联系管理员")
    return DeepSeekClient(global_key, api_url, model)


# ═══════════════════════════════════════════
# 单内容 RAG 问答
# ═══════════════════════════════════════════

@router.post("/video/{video_id}")
async def chat_video(video_id: int, req: ChatReq, user: CurrentUser, db: DbSession):
    """针对单个视频/文章内容的问答，流式返回"""
    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")

    question = _last_user_message(req.messages)
    transcript = video.transcript or ""
    summary = video.summary or ""

    # 精查：从原文中检索相关段落
    context_parts = []
    if transcript and question:
        hits = await asyncio.to_thread(rag_search, video.vid, transcript, question, 5)
        context_parts.extend(hits)
    if summary:
        context_parts.append(f"【内容总结】\n{summary}")

    context = "\n\n---\n\n".join(context_parts)[:MAX_CONTEXT_CHARS]
    client = await _get_client(db, user)

    return _stream_response(client, req.messages, context, source="video")


# ═══════════════════════════════════════════
# 知识库问答
# ═══════════════════════════════════════════

@router.post("/kb")
async def chat_kb(req: KBChatReq, user: CurrentUser, db: DbSession):
    """
    跨内容知识库问答。

    流程：粗筛相关内容 → 对命中内容做原文精查 → 拼装上下文 → 回答
    """
    question = _last_user_message(req.messages)

    # ─── 收集该用户的全部内容 ───
    videos = (
        await db.execute(
            select(Video).where(Video.user_id == user.id, Video.summary != "")
        )
    ).scalars().all()

    articles = (
        await db.execute(
            select(Article).where(Article.user_id == user.id, Article.summary != "")
        )
    ).scalars().all()

    if not videos and not articles:
        async def empty_gen():
            yield "知识库中还没有任何内容，请先总结一些视频或文章。"
        return StreamingResponse(empty_gen(), media_type="text/plain; charset=utf-8")

    # ─── 第一级：粗筛 ───
    items = []
    for v in videos:
        items.append({
            "key": f"v:{v.id}",
            "text": v.summary,
            "title": v.title,
            "kind": "video",
        })
    for a in articles:
        items.append({
            "key": f"a:{a.id}",
            "text": a.summary,
            "title": a.title,
            "kind": "article",
        })

    index = build_kb_index(user.id, items)
    ranked = index.rank(question, top_k=KB_TOP_N) if question else []

    # ─── 第二级：原文精查 ───
    context_parts = []
    sources = []

    vmap = {f"v:{v.id}": v for v in videos}
    amap = {f"a:{a.id}": a for a in articles}

    for key, score in ranked:
        if key.startswith("v:"):
            obj = vmap.get(key)
            if not obj:
                continue
            hits = await asyncio.to_thread(
                rag_search, obj.vid, obj.transcript or "", question, 3
            )
            body = "\n\n".join(hits) if hits else obj.summary
            context_parts.append(f"【视频：{obj.title}】\n{body}")
            sources.append({"type": "video", "id": obj.id, "title": obj.title})
        else:
            obj = amap.get(key)
            if not obj:
                continue
            hits = await asyncio.to_thread(
                rag_search, f"a{obj.id}", obj.text or "", question, 3
            )
            body = "\n\n".join(hits) if hits else obj.summary
            context_parts.append(f"【文章：{obj.title}】\n{body}")
            sources.append({"type": "article", "id": obj.id, "title": obj.title})

    context = "\n\n---\n\n".join(context_parts)[:MAX_CONTEXT_CHARS]
    client = await _get_client(db, user)

    return _stream_response(client, req.messages, context, source="kb", sources=sources)


# ═══════════════════════════════════════════
# 流式响应封装
# ═══════════════════════════════════════════

def _stream_response(client, messages: list[dict], context: str, source: str, sources: list | None = None):
    """SSE 流式返回，首帧携带来源信息"""
    clean_messages = [
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in messages
        if m.get("content")
    ]

    async def gen():
        if sources is not None:
            yield _sse("sources", {"sources": sources})
        try:
            for chunk in chat_stream(client, clean_messages, context):
                yield _sse("delta", {"text": chunk})
        except Exception as exc:
            logger.exception("问答失败")
            yield _sse("error", {"message": str(exc)})
        yield _sse("end", {})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _last_user_message(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            return str(m["content"])
    return ""


# ═══════════════════════════════════════════
# 对话记录
# ═══════════════════════════════════════════

@router.get("/history")
async def list_chats(user: CurrentUser, db: DbSession, kind: str = ""):
    stmt = select(Chat).where(Chat.user_id == user.id)
    if kind:
        stmt = stmt.where(Chat.kind == kind)

    rows = (
        await db.execute(stmt.order_by(Chat.created_at.desc()).limit(200))
    ).scalars().all()

    return {
        "items": [
            {
                "id": c.id,
                "kind": c.kind,
                "video_id": c.video_id,
                "title": c.title,
                "message_count": len(json.loads(c.messages or "[]")),
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "",
            }
            for c in rows
        ]
    }


@router.get("/history/{chat_id}")
async def get_chat(chat_id: int, user: CurrentUser, db: DbSession):
    chat = await db.get(Chat, chat_id)
    if not chat or chat.user_id != user.id:
        raise NotFoundError("对话不存在")

    return {
        "id": chat.id,
        "kind": chat.kind,
        "video_id": chat.video_id,
        "title": chat.title,
        "messages": json.loads(chat.messages or "[]"),
        "created_at": chat.created_at.strftime("%Y-%m-%d %H:%M") if chat.created_at else "",
    }


@router.post("/history")
async def save_chat(req: ChatSaveReq, user: CurrentUser, db: DbSession):
    """保存或更新对话记录"""
    title = req.title or _last_user_message(req.messages)[:30] or "新对话"

    if req.chat_id:
        chat = await db.get(Chat, req.chat_id)
        if not chat or chat.user_id != user.id:
            raise NotFoundError("对话不存在")
        chat.title = title
        chat.messages = json.dumps(req.messages, ensure_ascii=False)
        await db.flush()
        return {"id": chat.id}

    chat = Chat(
        user_id=user.id,
        kind=req.kind or ChatKind.KB,
        video_id=req.video_id,
        title=title,
        messages=json.dumps(req.messages, ensure_ascii=False),
    )
    db.add(chat)
    await db.flush()
    return {"id": chat.id}


@router.delete("/history/{chat_id}", response_model=Ok)
async def delete_chat(chat_id: int, user: CurrentUser, db: DbSession):
    await db.execute(
        delete(Chat).where(Chat.id == chat_id, Chat.user_id == user.id)
    )
    return Ok(message="已删除")


# ═══════════════════════════════════════════
# 导出
# ═══════════════════════════════════════════

@router.get("/export/video/{video_id}")
async def export_video(video_id: int, user: CurrentUser, db: DbSession):
    """导出总结为 Markdown"""
    from fastapi.responses import Response

    video = await db.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise NotFoundError("视频不存在")

    content = f"# {video.title}\n\n"
    if video.uploader:
        content += f"- UP主：{video.uploader}\n"
    if video.url:
        content += f"- 链接：{video.url}\n"
    if video.duration_str:
        content += f"- 时长：{video.duration_str}\n"
    if video.processed_at:
        content += f"- 处理时间：{video.processed_at.strftime('%Y-%m-%d %H:%M')}\n"
    content += f"\n---\n\n{video.summary or '（暂无总结）'}\n"

    filename = f"{_safe_filename(video.title)}.md"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        },
    )


@router.get("/export/article/{article_id}")
async def export_article(article_id: int, user: CurrentUser, db: DbSession):
    from fastapi.responses import Response

    article = await db.get(Article, article_id)
    if not article or article.user_id != user.id:
        raise NotFoundError("文章不存在")

    content = f"# {article.title}\n\n"
    if article.url:
        content += f"- 原文链接：{article.url}\n"
    content += f"\n---\n\n{article.summary or '（暂无总结）'}\n"

    filename = f"{_safe_filename(article.title)}.md"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _safe_filename(name: str, max_len: int = 60) -> str:
    import re

    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "untitled").strip()
    return cleaned[:max_len] or "untitled"
