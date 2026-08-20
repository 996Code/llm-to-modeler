"""
配置 API 路由模块 —— 用户对话的唯一 HTTP 入口。

【在链路中的位置】
浏览器 → ★chat() → stream_graph(engine/stream.py) → graph(nodes.py 三节点)
                                                → SSE 实时推送回浏览器

端点清单：
  POST /api/config/chat     → SSE 流式（LangGraph StateGraph 统一入口，支持追问恢复）

核心设计（Java 视角）：
  - 统一入口：/chat 是唯一的对话端点，意图由后端 LangGraph 分类。
    类比 Spring MVC 的 DispatcherServlet 统一分发。
  - 上下文优先级：context.artifact（宿主当次下发的画布）> 会话存储的
    上次产出——防止陈旧基线覆盖用户手动修改。
  - 请求头透传：嵌入模式下，宿主系统的认证头透传到上游 njmind-modeler API。
    类比 Spring 的 SecurityContext 跨系统传递。
  - SSE 流式：StreamingResponse + text/event-stream，实时推送管线进度。
    响应头 no-transform 防 rsbuild 等代理的 gzip 缓冲（见 stream.py 模块注释）。
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from engine.state_keys import CONTEXT_ARTIFACT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])


class ChatRequest(BaseModel):
    """统一对话请求——意图由后端 LangGraph 自动分类。

    追问恢复机制：
        answers 非空时，走 LangGraph Command(resume=answers) 路径，
        从上次 interrupt 的断点继续执行（而非当作新消息重新分类意图）。
        类比 Java 的 wait()/notify()——挂起时 HTTP 响应结束，
        恢复时是新的 HTTP 请求带 answers。

    图片识别：
        image_base64 非空时，传给 ImageFormTool 做图片识别（多模态）。

    嵌入模式扩展（P1）：
        context: 宿主下发的当前制品（覆盖会话旧配置再进图，防止陈旧基线覆盖手动修改）。
        services: 宿主提供的服务地址表（如 {njmind-modeler: origin+/codeBack}），
                  按请求切换上游地址（过白名单，见 upstream_client.resolve_base）。

    Attributes:
        message: 用户消息文本
        conversation_id: 会话 ID（首次对话可不传，后端自动创建）
        answers: 追问回答（非空表示追问恢复）
        image_base64: 图片 base64 编码（用于图片识别表单）
        context: 宿主当前上下文（可选）
        services: 宿主服务地址表（可选）
    """
    message: str = Field(..., description="User message")
    conversation_id: Optional[str] = None
    answers: Optional[Dict[str, Any]] = None
    image_base64: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    services: Optional[Dict[str, str]] = None


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
# 可被环境变量 FORWARD_HEADER_PREFIXES 覆盖（逗号分隔）：接入新宿主时头名不同，
# 改环境变量即可，无需改代码。默认值含 enterprise，保证 mind-designer 的
# enterprise-id 头不被丢弃。
_DEFAULT_FORWARD_PREFIXES = ("x-", "authorization", "cookie", "tenant", "enterprise", "accept-language")
_FORWARD_PREFIXES = tuple(
    p.strip().lower()
    for p in os.getenv("FORWARD_HEADER_PREFIXES", ",".join(_DEFAULT_FORWARD_PREFIXES)).split(",")
    if p.strip()
)


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
    # 宿主显式下发的上下文优先于会话库旧配置（P1：防止陈旧基线覆盖手动修改）
    context_artifact = (
        req.context.get(CONTEXT_ARTIFACT) if req.context and req.context.get(CONTEXT_ARTIFACT) else None
    ) or _load_current_config(request, req.conversation_id)  # 宿主上下文优先，会话存储兜底
    history = _load_history(request, req.conversation_id)  # 对话历史（LLM 上下文）
    fwd = _extract_forward_headers(request)  # 透传头（嵌入模式 SSO）

    # 宿主服务地址表经 stream_graph 传入，在工作线程内绑定 thread-local
    #（不能在这里绑：graph 跑在 run_in_executor 的工作线程，事件循环线程的
    #  thread-local 传不过去，见 stream.py _run_graph 的说明）

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
            user_id=request.headers.get("X-User-Id") or "anonymous",  # 与 conversations API 缺省一致（"" 会让 chat 流静默跳过落库）
            answers=req.answers,  # ← 追问回答（非空表示追问恢复）
            image_base64=req.image_base64,  # ← 图片 base64（图片识别）
            conversation_store=request.app.state.conversation_store,
            conversation_history=history,
            context_artifact=context_artifact,
            forward_headers=fwd,
            services=req.services,  # ← 宿主服务地址表（工作线程内绑定）
        ):
            yield event  # 把每个事件推给前端（SSE）

    return StreamingResponse(
        stream(),  # 传入 async generator 作为响应体
        media_type="text/event-stream",  # SSE 的 MIME 类型
        # Cache-Control: no-cache 防缓存；no-transform 禁止任何中间代理压缩/改写流
        #   （designer rsbuild 的 gzip 中间件会缓冲 SSE 攒到流结束才吐，必须跳过）
        # X-Accel-Buffering: no 禁止 Nginx 缓冲（SSE 要实时推送，缓冲会导致延迟）
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
