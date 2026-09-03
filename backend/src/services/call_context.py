"""请求级调用上下文(thread-local)—— 底层调用自动关联会话的"链路归属"载体。

【模块定位】
插件经 ctx.asset_client / ctx.llm_client 发起的底层调用,其日志
(call_logs)要关联到当前会话,管理端链路视图才能把"上游 API 调用"
与"LLM 调用"串进同一条时间线。但要求每个插件逐个透传 conv_id 既
繁琐又容易漏(事实上此前的上游调用就因此全部断链)。

本模块用 thread-local 把 conv_id 绑定在"正在执行 graph 的那个工作线程"
上(stream.py 在请求开始时绑定、结束时清理——与 forward_headers/
services 的既有绑定完全同构),UpstreamClient / LLMClient 的日志层
自动读取。插件零感知、零改动:底层封装好,插件只管业务逻辑。

【线程语义】
graph.stream 跑在 run_in_executor 的工作线程;节点内的工具调用与
上游/LLM 日志都发生在同一线程,因此 thread-local 天然请求级隔离,
并发对话互不串线。
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 与 upstream_client 的 _forward_headers / _request_services 同构的线程本地槽
_conversation = threading.local()


def bind_conversation(conv_id: Optional[str]) -> None:
    """在当前线程绑定会话 ID(graph 工作线程开始时调用)。

    Args:
        conv_id: 会话 ID;None 表示无会话上下文(脚本直跑图/联调),清空绑定。
    """
    _conversation.conv_id = conv_id or None


def clear_conversation() -> None:
    """清空当前线程的会话绑定(请求结束时调用,防线程池复用串线)。"""
    _conversation.conv_id = None


def current_conversation_id() -> Optional[str]:
    """读当前线程绑定的会话 ID;未绑定返回 None(调用方以此兜底)。"""
    return getattr(_conversation, "conv_id", None)
