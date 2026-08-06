"""
配置 API 路由模块。

提供统一对话入口和表单配置相关端点。

端点清单：
  POST /api/config/chat     → SSE 流式（LangGraph StateGraph 统一入口，支持追问恢复）
  POST /api/config/generate → [已废弃] 转发到 /chat（向后兼容）
  POST /api/config/modify   → [已废弃] 转发到 /chat（向后兼容）
  POST /api/config/validate → 同步校验（调上游 API）

核心设计（Java 视角）：
  - 统一入口：/chat 是唯一的对话端点，意图由后端 LangGraph 分类。
    类比 Spring MVC 的 DispatcherServlet 统一分发。
  - 请求头透传：嵌入模式下，宿主系统的认证头透传到上游 njmind-modeler API。
    类比 Spring 的 SecurityContext 跨系统传递。
  - SSE 流式：StreamingResponse + text/event-stream，实时推送管线进度。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])


class GenerateRequest(BaseModel):
    """[已废弃] 生成表单请求（旧接口，保留向后兼容）。"""
    description: str = Field(..., description="Natural language form description")
    conversation_id: Optional[str] = None


class ModifyRequest(BaseModel):
    """[已废弃] 修改表单请求（旧接口，保留向后兼容）。"""
    current_config: Dict[str, Any] = Field(..., description="Current FormConfig")
    instruction: str = Field(..., description="Modification instruction")
    conversation_id: Optional[str] = None


class ChatRequest(BaseModel):
    """统一对话请求——意图由后端 LangGraph 自动分类。

    追问恢复机制：
        answers 非空时，走 LangGraph Command(resume=answers) 路径，
        从上次 interrupt 的断点继续执行（而非当作新消息重新分类意图）。
        类比 Java 的 wait()/notify()——挂起时 HTTP 响应结束，
        恢复时是新的 HTTP 请求带 answers。

    图片识别：
        image_base64 非空时，传给 ImageFormTool 做图片识别（多模态）。

    Attributes:
        message: 用户消息文本
        conversation_id: 会话 ID（首次对话可不传，后端自动创建）
        answers: 追问回答（非空表示追问恢复）
        image_base64: 图片 base64 编码（用于图片识别表单）
    """
    message: str = Field(..., description="User message")
    conversation_id: Optional[str] = None
    answers: Optional[Dict[str, Any]] = None
    image_base64: Optional[str] = None


class ValidateRequest(BaseModel):
    """表单校验请求（同步调上游 API）。"""
    config: Dict[str, Any] = Field(..., description="FormConfig to validate")
    mode: Optional[str] = Field(default="CREATE")


def _load_current_config(request: Request, conv_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """从会话存储加载当前表单配置。

    用于统一对话入口——判断是否有已有配置（影响意图识别，
    如"修改表单"需要已有配置才能执行）。

    类比 Java 的 Session.getAttribute()，但是从 SQLite 读。
    """
    if not conv_id:
        return None  # 无会话 ID：首次对话，肯定没有已有配置
    # X-User-Id 头由网关注入，缺省 "anonymous" 做多租户隔离
    user_id = request.headers.get("X-User-Id") or "anonymous"
    # conversation_store 挂在 app.state 上（类比 ServletContext 的全局属性）
    store = request.app.state.conversation_store
    try:
        conv = store.get_conversation(conv_id, user_id)  # 从 SQLite 读会话
        if conv and conv.get("currentConfig"):
            return conv["currentConfig"]  # 返回已有的表单配置
    except Exception:
        # 读取异常静默吞掉：不影响主流程，最多当作无已有配置处理（Fail-Closed）
        pass
    return None


# 需要透传到上游 njmind-modeler 的请求头前缀。
# 排除 hop-by-hop 头（host/content-length 等）和内部头（X-User-Id 是本项目自己用的）。
# 类比 Java 的 HeaderPropagationFilter——选择性地把请求头传给下游服务。
_FORWARD_PREFIXES = ("x-", "authorization", "cookie", "tenant", "accept-language")


def _extract_forward_headers(request: Request) -> Dict[str, str]:
    """提取需要透传到上游的请求头。

    嵌入模式下，宿主系统的认证信息（Authorization/Cookie/X-*-Tenant）
    需要透传到上游 njmind-modeler API，实现单点登录。

    排除：
      - hop-by-hop 头（host/content-length/content-type/connection）
      - 内部头（X-User-Id 是本项目自己解析的，不透传）
      - X-Accel-Buffering（Nginx 专用，不透传）

    Returns:
        需要透传的请求头字典
    """
    forwarded = {}
    # 遍历请求头，按白名单前缀选择性透传
    # request.headers.items() 返回所有头，类比 Java HttpServletRequest.getHeaderNames()
    for key, value in request.headers.items():
        lower = key.lower()  # HTTP 头不区分大小写，统一转小写比较
        # 排除 hop-by-hop 和内部头
        # hop-by-hop 头是单跳头（host/connection 等），不该跨服务透传
        # x-user-id 是本项目内部头（已解析），透传会给上游造成困惑
        if lower in ("host", "content-length", "content-type", "connection",
                      "x-user-id", "x-accel-buffering"):
            continue
        # 只转发匹配前缀的头
        # any(...) 短路：只要匹配任一前缀就透传，类比 Java stream().anyMatch()
        if any(lower.startswith(p) for p in _FORWARD_PREFIXES):
            forwarded[key] = value  # 保留原始大小写的 key
    return forwarded


def _load_history(request: Request, conv_id: Optional[str]) -> List[Dict[str, str]]:
    """加载对话历史作为 LLM 上下文。

    只返回 role + content（不含配置快照等额外数据，LLM 不需要）。
    类比 Java 的 Conversation.history() 但精简为 LLM 需要的格式。

    Returns:
        [{role: "user"/"assistant", content: "..."}] 列表
    """
    if not conv_id:
        return []  # 无会话：空历史
    user_id = request.headers.get("X-User-Id") or "anonymous"  # 多租户隔离
    store = request.app.state.conversation_store
    try:
        conv = store.get_conversation(conv_id, user_id)
        if not conv or not conv.get("messages"):
            return []  # 会话不存在或无消息：空历史
        # Only pass role + content (LLM doesn't need config snapshots in history)
        # 中文说明：只取 role + content，丢弃配置快照等元数据（LLM 只需要对话文本）
        # 列表推导：类比 Java stream().map(m -> Map.of("role",...)).collect(toList())
        return [
            {"role": m["role"], "content": m["content"]}
            for m in conv["messages"]
        ]
    except Exception as e:
        # 历史加载失败：记 warning 但返回空历史（不阻断主流程）
        logger.warning(f"Failed to load history for conv {conv_id}: {e}")
        return []


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """统一对话入口——LangGraph StateGraph 编排，SSE 流式返回。

    这是整个系统的唯一对话端点。两种调用模式：
    1. 正常消息（answers 为空）：input = {user_input, ...}，走完整流程（意图识别→工具执行→结果处理）
    2. 追问恢复（answers 非空）：input = Command(resume=answers)，从上次 interrupt 的断点继续

    流程：
      1. 加载当前配置（判断是否有已有制品）
      2. 加载对话历史（作为 LLM 上下文）
      3. 提取透传请求头（嵌入模式 SSO）
      4. 调 stream_graph() 跑 LangGraph + 推送 SSE

    返回 StreamingResponse（text/event-stream），前端用 ReadableStream 逐事件解析。
    """
    graph = request.app.state.graph  # LangGraph 编排器，类比 Spring 的 DispatcherServlet
    current_config = _load_current_config(request, req.conversation_id)  # 已有配置（影响意图）
    history = _load_history(request, req.conversation_id)  # 对话历史（LLM 上下文）
    fwd = _extract_forward_headers(request)  # 透传头（嵌入模式 SSO）

    # 延迟导入 stream_graph：避免模块加载时的循环依赖
    # 类比 Java 的懒加载，运行时才解析依赖
    from engine.stream import stream_graph

    async def stream():
        # stream_graph 是 async generator，yield 出 SSE 格式的事件字符串
        # async for：异步迭代，类比 Java 的 Flux/Reactor 流
        async for event in stream_graph(
            graph=graph,
            user_input=req.message,
            conversation_id=req.conversation_id,
            user_id=request.headers.get("X-User-Id", ""),
            answers=req.answers,  # ← 追问回答（非空表示追问恢复）
            image_base64=req.image_base64,  # ← 图片 base64（图片识别）
            conversation_store=request.app.state.conversation_store,
            conversation_history=history,
            current_config=current_config,
            forward_headers=fwd,
        ):
            yield event  # 把每个事件推给前端（SSE）

    return StreamingResponse(
        stream(),  # 传入 async generator 作为响应体
        media_type="text/event-stream",  # SSE 的 MIME 类型
        # Cache-Control: no-cache 防缓存
        # X-Accel-Buffering: no 禁止 Nginx 缓冲（SSE 要实时推送，缓冲会导致延迟）
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/generate")
async def generate(req: GenerateRequest, request: Request):
    """[DEPRECATED] 使用 /api/chat 替代。保留向后兼容,内部转发到 chat。"""
    # 已废弃:旧版生成接口,内部转发到统一的 stream_graph
    # 保留是为兼容未迁移的前端代码(类比 Spring 的 @Deprecated 路由)
    graph = request.app.state.graph
    history = _load_history(request, req.conversation_id)  # 加载历史上下文
    fwd = _extract_forward_headers(request)  # 透传鉴权头

    from engine.stream import stream_graph

    async def stream():
        # 把 req.description 当 user_input 传给 stream_graph
        # current_config=None:旧接口无已有配置概念
        async for event in stream_graph(
            graph=graph,
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
    # 已废弃:旧版修改接口,内部转发到统一的 stream_graph
    graph = request.app.state.graph
    history = _load_history(request, req.conversation_id)
    fwd = _extract_forward_headers(request)

    from engine.stream import stream_graph

    async def stream():
        # 把 req.instruction 当 user_input,current_config 作为修改起点
        # 这两个参数是 modify 与 generate 的区别:modify 带已有配置
        async for event in stream_graph(
            graph=graph,
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
    """通过上游 API 同步校验表单配置。

    调上游 njmind-modeler 的校验接口，返回校验结果（含错误列表）。
    不走 LangGraph，直接同步调用。
    """
    upstream = request.app.state.upstream  # 上游客户端单例，挂在 app.state
    # 直接调上游校验，不走 LangGraph（轻量同步调用）
    # mode 缺省 CREATE：req.mode or "CREATE" 是 Python 的空值兜底，类比 Java 的 Optional.orElse
    result = upstream.validate_form(req.config, mode=req.mode or "CREATE")
    return result  # 直接返回校验结果 dict（FastAPI 自动序列化为 JSON）
