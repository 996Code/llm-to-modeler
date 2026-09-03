"""
会话（Conversation）CRUD API 模块。

负责会话的增删改查，会话消息的持久化由 ConversationStore（SQLite）负责。

接口清单：
  POST   /api/conversations              → 新建会话
  GET    /api/conversations              → 列出当前用户的会话（admin 看全部）
  GET    /api/conversations/{id}         → 获取会话详情（含全部消息）
  DELETE /api/conversations/{id}         → 删除会话

核心设计（Java 视角）：
  - 无登录态：本服务不维护用户登录，user_id 由上游系统通过 X-User-Id 请求头透传。
    类比 Spring 里前置网关已经鉴权，业务层直接信任 header。
  - admin 越权：user_id == "admin" 且请求满足管理端授权（api/admin.py 的
    is_admin_authorized——口令模式须带合法 X-Admin-Token，开放模式直接放行）
    时可查看/访问所有用户的会话。未满足时 "admin" 退化为普通用户名。
  - app.state.conversation_store：ConversationStore 单例，类比 @Autowired Repository。
    在 main.py lifespan 中初始化。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from api.admin import is_admin_authorized

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    """新建会话请求体（Pydantic 模型）。

    类比 Java 的 DTO + Bean Validation，Pydantic 自动校验 JSON 结构。
    title 可空，默认空字符串。context_key 用于嵌入模式会话绑定（如 formCode）。
    """
    title: Optional[str] = ""
    context_key: Optional[str] = ""


def _get_user_id(request: Request, x_user_id: Optional[str] = Header(None)) -> str:
    """从请求中提取 user_id，取不到则降级为 'anonymous'。

    取值优先级（三级降级）：
      1. X-User-Id 请求头（上游网关注入，生产环境主路径）
      2. userId 查询参数（方便联调/测试）
      3. 'anonymous'（兜底，未认证用户的匿名标识）

    Args:
        request: 当前请求对象。
        x_user_id: FastAPI 自动从 X-User-Id 头注入（None 表示头不存在）。

    Returns:
        user_id 字符串。
    """
    uid = x_user_id or request.query_params.get("userId") or "anonymous"
    return uid


@router.post("")
async def create_conversation(
    req: CreateConversationRequest,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """新建会话。

    Args:
        req: 请求体，含可选 title。
        request: 当前请求对象。
        x_user_id: X-User-Id 请求头。

    Returns:
        新建的会话对象（含分配的 conv_id、创建时间等）。
    """
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    return store.create_conversation(user_id, req.title or "", req.context_key or "")


@router.get("")
async def list_conversations(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """列出当前用户的会话。admin 可查看所有用户的对话。

    Args:
        request: 当前请求对象。
        x_user_id: X-User-Id 请求头。

    Returns:
        会话列表（不含消息正文，只含元信息如标题、最后更新时间）。
    """
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    # 嵌入模式会话恢复：按 (user_id, contextKey) 查该绑定下最新会话
    # 前端 GET /api/conversations?contextKey=xxx&latest=true 触发。
    # 「还没有历史会话」是预期状态（首次打开该表单），返回 200 + null 而非 404：
    # 404 会在浏览器控制台刷红，容易被误判为链路故障（真正的错误仍走异常）。
    context_key = request.query_params.get("contextKey")
    latest = request.query_params.get("latest") == "true"
    if context_key and latest:
        return store.find_latest_by_context(user_id, context_key)
    # admin + 合法管理口令 可查看所有用户的对话（跨用户审计走管理端鉴权）
    if user_id == "admin" and is_admin_authorized(request):
        return store.list_all_conversations()
    return store.list_conversations(user_id)


@router.get("/{conv_id}")
async def get_conversation(
    conv_id: str,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """获取会话详情（含全部消息）。admin 可访问任何用户的会话。

    Args:
        conv_id: 会话 ID。
        request: 当前请求对象。
        x_user_id: X-User-Id 请求头。

    Returns:
        会话对象（含 messages 列表）。

    Raises:
        HTTPException(404): 会话不存在或无权访问。
    """
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    # admin + 合法管理口令 可访问任何对话（同 list 的越权收紧）
    if user_id == "admin" and is_admin_authorized(request):
        conv = store.get_conversation_any_user(conv_id)
    else:
        conv = store.get_conversation(conv_id, user_id)
    # Fail-Closed：找不到或无权访问都返回 404（不区分，避免泄露会话是否存在）
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.delete("/{conv_id}")
async def delete_conversation(
    conv_id: str,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """删除会话。

    Args:
        conv_id: 会话 ID。
        request: 当前请求对象。
        x_user_id: X-User-Id 请求头。

    Returns:
        {"success": True}。

    Raises:
        HTTPException(404): 会话不存在或无权删除。
    """
    store = request.app.state.conversation_store
    user_id = _get_user_id(request, x_user_id)
    # delete_conversation 内部会校验 user_id 权限（非 owner 删不掉）
    if not store.delete_conversation(conv_id, user_id):
        raise HTTPException(404, "Conversation not found")
    return {"success": True}
