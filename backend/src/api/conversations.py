"""
Conversations API Router

POST   /api/conversations              → create
GET    /api/conversations              → list (by user_id from header)
GET    /api/conversations/{id}         → detail with messages
DELETE /api/conversations/{id}         → delete

No login — user_id from X-User-Id header (passed by upstream system).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    title: Optional[str] = ""


def _get_user_id(request: Request, x_user_id: Optional[str] = Header(None)) -> str:
    """Extract user_id from header. Falls back to 'anonymous'."""
    uid = x_user_id or request.query_params.get("userId") or "anonymous"
    return uid


@router.post("")
async def create_conversation(
    req: CreateConversationRequest,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Create a new conversation."""
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    return store.create_conversation(user_id, req.title or "")


@router.get("")
async def list_conversations(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """List conversations for the current user."""
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    return store.list_conversations(user_id)


@router.get("/{conv_id}")
async def get_conversation(
    conv_id: str,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Get conversation detail with all messages."""
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    conv = store.get_conversation(conv_id, user_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.delete("/{conv_id}")
async def delete_conversation(
    conv_id: str,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Delete a conversation."""
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    if not store.delete_conversation(conv_id, user_id):
        raise HTTPException(404, "Conversation not found")
    return {"success": True}
