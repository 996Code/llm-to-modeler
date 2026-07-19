"""
Config API Router

POST /api/config/chat     -> SSE stream (统一入口,ToolDispatcher 选工具)
POST /api/config/generate -> [DEPRECATED] 转发到 /chat
POST /api/config/modify   -> [DEPRECATED] 转发到 /chat
POST /api/config/validate -> sync (validate via upstream)
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])


class GenerateRequest(BaseModel):
    description: str = Field(..., description="Natural language form description")
    conversation_id: Optional[str] = None


class ModifyRequest(BaseModel):
    current_config: Dict[str, Any] = Field(..., description="Current FormConfig")
    instruction: str = Field(..., description="Modification instruction")
    conversation_id: Optional[str] = None


class ChatRequest(BaseModel):
    """Unified chat entry — intent is classified by the backend."""
    message: str = Field(..., description="User message")
    conversation_id: Optional[str] = None


class ValidateRequest(BaseModel):
    config: Dict[str, Any] = Field(..., description="FormConfig to validate")
    mode: Optional[str] = Field(default="CREATE")


def _load_current_config(request: Request, conv_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Load current config from conversation store (for unified chat entry)."""
    if not conv_id:
        return None
    user_id = request.headers.get("X-User-Id") or "anonymous"
    store = request.app.state.conversation_store
    try:
        conv = store.get_conversation(conv_id, user_id)
        if conv and conv.get("currentConfig"):
            return conv["currentConfig"]
    except Exception:
        pass
    return None


# Headers to forward to upstream njmind-modeler (everything except hop-by-hop
# and our own internal headers).
_FORWARD_PREFIXES = ("x-", "authorization", "cookie", "tenant", "accept-language")


def _extract_forward_headers(request: Request) -> Dict[str, str]:
    """Extract headers from the incoming request to forward to upstream modeler.

    Forwards: Authorization, cookies, and all X-* / tenant headers.
    """
    forwarded = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in ("host", "content-length", "content-type", "connection",
                      "x-user-id", "x-accel-buffering"):
            continue
        if any(lower.startswith(p) for p in _FORWARD_PREFIXES):
            forwarded[key] = value
    return forwarded


def _load_history(request: Request, conv_id: Optional[str]) -> List[Dict[str, str]]:
    """Load conversation history as [{role, content}] for LLM context."""
    if not conv_id:
        return []
    user_id = request.headers.get("X-User-Id") or "anonymous"
    store = request.app.state.conversation_store
    try:
        conv = store.get_conversation(conv_id, user_id)
        if not conv or not conv.get("messages"):
            return []
        # Only pass role + content (LLM doesn't need config snapshots in history)
        return [
            {"role": m["role"], "content": m["content"]}
            for m in conv["messages"]
        ]
    except Exception as e:
        logger.warning(f"Failed to load history for conv {conv_id}: {e}")
        return []


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """Unified chat entry - ToolDispatcher 选工具 + 执行。

    阶段 4:旧 LangGraph 已删除,统一走 ToolDispatcher。
    """
    dispatcher = request.app.state.dispatcher
    current_config = _load_current_config(request, req.conversation_id)
    history = _load_history(request, req.conversation_id)
    fwd = _extract_forward_headers(request)

    from engine.stream import stream_dispatcher

    async def stream():
        async for event in stream_dispatcher(
            dispatcher=dispatcher,
            user_input=req.message,
            conversation_id=req.conversation_id,
            user_id=request.headers.get("X-User-Id", ""),
            conversation_store=request.app.state.conversation_store,
            conversation_history=history,
            current_config=current_config,
            forward_headers=fwd,
        ):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/generate")
async def generate(req: GenerateRequest, request: Request):
    """[DEPRECATED] 使用 /api/chat 替代。保留向后兼容,内部转发到 chat。"""
    # 转发到 chat 逻辑
    dispatcher = request.app.state.dispatcher
    history = _load_history(request, req.conversation_id)
    fwd = _extract_forward_headers(request)

    from engine.stream import stream_dispatcher

    async def stream():
        async for event in stream_dispatcher(
            dispatcher=dispatcher,
            user_input=req.description,
            conversation_id=req.conversation_id,
            user_id=request.headers.get("X-User-Id", ""),
            conversation_store=request.app.state.conversation_store,
            conversation_history=history,
            current_config=None,
            forward_headers=fwd,
        ):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/modify")
async def modify(req: ModifyRequest, request: Request):
    """[DEPRECATED] 使用 /api/chat 替代。保留向后兼容,内部转发到 chat。"""
    dispatcher = request.app.state.dispatcher
    history = _load_history(request, req.conversation_id)
    fwd = _extract_forward_headers(request)

    from engine.stream import stream_dispatcher

    async def stream():
        async for event in stream_dispatcher(
            dispatcher=dispatcher,
            user_input=req.instruction,
            conversation_id=req.conversation_id,
            user_id=request.headers.get("X-User-Id", ""),
            conversation_store=request.app.state.conversation_store,
            conversation_history=history,
            current_config=req.current_config,
            forward_headers=fwd,
        ):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/validate")
async def validate(req: ValidateRequest, request: Request):
    """Validate FormConfig via upstream API (sync)."""
    upstream = request.app.state.upstream
    result = upstream.validate_form(req.config, mode=req.mode or "CREATE")
    return result
