"""
SSE 流式推送模块。

本模块定义了 SSE（Server-Sent Events）的事件格式和线程安全管理器。

核心概念（Java 视角）：
  - SSE：单向 HTTP 长连接，服务器持续推送事件。类比 Spring MVC 的 SseEmitter。
  - StreamManager：线程安全的事件队列。LangGraph 的 graph.stream 在工作线程跑，
    但 SSE 的 async generator 在事件循环线程跑，两者通过 call_soon_threadsafe 桥接。
    类比 Java 的 BlockingQueue + 跨线程通信。

事件流转链路：
  graph.stream (工作线程)
    → nodes.py 的 emit() 收集到 sse_events 列表
    → stream.py 的 stream_graph 消费 sse_events
    → StreamManager.stage() / emit_result() 推入 asyncio.Queue
    → StreamManager.stream() (async generator) 逐个 yield 给客户端

所有 SSE 流由 engine/stream.py::stream_graph() 统一消费。
"""

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)

# 流结束哨兵对象——用 object() 保证不会和任何真实事件冲突
# 类比 Java 的特殊返回值标记（如 null 表示结束），但用哨兵更安全
_STREAM_END = object()


class SSEEvent:
    """单个 SSE 事件封装。

    SSE 标准格式（每条事件用两个换行分隔）：
        event: <事件类型>
        data: <JSON 数据>

    类比 Java 的 SSE 事件对象，但 Python 用 dataclass 式的简单封装。
    """

    def __init__(self, event: str, data: Dict[str, Any]):
        """初始化 SSE 事件。

        Args:
            event: 事件类型（stage/result/error/done/pipeline_definition/needsClarification）
            data: 事件数据字典，会被 JSON 序列化
        """
        self.event = event
        self.data = data

    def to_sse(self) -> str:
        """格式化为 SSE 标准字符串。

        输出格式：`event: xxx\\ndata: {...}\\n\\n`（末尾两个换行是 SSE 规范要求的分隔符）。
        ensure_ascii=False 保留中文（否则会被转义成 \\uXXXX）。
        default=str 兜底处理不可 JSON 序列化的对象（如 datetime → str）。
        """
        data_str = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.event}\ndata: {data_str}\n\n"


class StreamManager:
    """线程安全的 SSE 事件队列管理器。

    解决的核心问题：LangGraph 的 graph.stream() 在工作线程（通过 asyncio.to_thread）执行，
    而 FastAPI 的 SSE 响应（async generator）在事件循环线程执行。
    两个线程不能直接操作同一个 asyncio.Queue（非线程安全），
    必须通过 loop.call_soon_threadsafe() 把操作投递回事件循环线程。

    类比 Java：BlockingQueue 是线程安全的，但 Python 的 asyncio.Queue 不是。
    所以需要 call_soon_threadsafe 做跨线程投递。

    使用方式：
        # 事件循环线程创建
        manager = StreamManager(asyncio.get_event_loop())

        # 工作线程推送事件（线程安全）
        manager.stage("generate", "正在生成配置...")

        # 事件循环线程消费（async generator）
        async for chunk in manager.stream():
            yield chunk
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        """初始化流管理器。

        Args:
            loop: 当前事件循环。工作线程通过它把事件投递回事件循环线程。
        """
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()

    def stage(self, stage: str, message: str, **extra):
        """推送阶段进度事件（线程安全，可从工作线程调用）。

        这是传给工具执行的进度回调。工具每完成一步就调这个方法，
        前端实时显示管线进度（如"正在获取填写指南..."）。

        类比 Java 的进度监听器回调，但通过队列异步推送。

        Args:
            stage: 阶段标识（如 fetch_guide/list_assets/parse_fields/generate/validate）
            message: 给用户看的进度文案
            **extra: 额外数据（如 pipeline_step 序号）
        """
        data = {"stage": stage, "message": message, **extra}
        # ★ call_soon_threadsafe：把 put_nowait 操作投递到事件循环线程执行
        # 工作线程不能直接操作 asyncio.Queue（非线程安全），必须通过这个桥接
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, SSEEvent("stage", data)
        )

    def pipeline_definition(self, tool_name: str, steps: list):
        """推送管线定义事件（线程安全）。

        在 CompositeTool 执行第一步前发送，告诉前端这个工具有哪些步骤，
        前端可以渲染完整的进度条骨架（而不是一步步出现）。

        类比 Java 的 Workflow 定义，但动态推送给前端。

        Args:
            tool_name: 工具名（如 create_form）
            steps: 步骤名列表（如 ["fetch_guide", "generate", "validate"]）
        """
        data = {"tool": tool_name, "steps": steps}
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, SSEEvent("pipeline_definition", data)
        )

    async def emit_result(self, data: Dict[str, Any]):
        """推送最终结果事件（只能在事件循环线程调用）。

        工具执行完成后，结果通过这个方法推送。data 格式取决于制品类型：
        - config 制品：{config, valid, validationErrors, summary, ...formatted}
        - data 制品：{artifactType: "data", data, summary, ...formatted}
        - 闲聊回复：{intent: "general", reply, summary}

        Args:
            data: 结果数据
        """
        await self._queue.put(SSEEvent("result", data))

    async def emit_error(self, message: str, **extra):
        """推送错误事件（只能在事件循环线程调用）。

        Args:
            message: 错误信息（会给用户看到）
            **extra: 额外错误上下文
        """
        await self._queue.put(SSEEvent("error", {"error": message, **extra}))

    async def emit_done(self):
        """推送完成事件并结束流。

        先发 done 事件告诉前端流程结束，再推入哨兵对象终止 stream() 循环。
        顺序很重要：必须先发 done 再发哨兵，否则前端收不到 done。
        """
        await self._queue.put(SSEEvent("done", {"status": "done"}))
        await self._queue.put(_STREAM_END)  # 哨兵终止 stream 循环

    async def stream(self) -> AsyncGenerator[str, None]:
        """SSE 事件流 async generator。

        FastAPI 的 StreamingResponse 消费这个 generator，逐个 yield SSE 字符串。

        120 秒超时机制：如果 120 秒内没有事件，发一个 keepalive 注释（`: keepalive\\n\\n`）
        保持连接不被代理/浏览器断开。SSE 注释以 `:` 开头，客户端会忽略。

        遇到哨兵 _STREAM_END 时终止流。

        Yields:
            SSE 格式字符串
        """
        while True:
            try:
                # 120 秒超时——LLM 调用可能很慢，用 keepalive 保活
                item = await asyncio.wait_for(self._queue.get(), timeout=120.0)
            except asyncio.TimeoutError:
                # 超时发 keepalive 注释，保持连接（SSE 注释以 : 开头，客户端忽略）
                yield ": keepalive\n\n"
                continue
            if item is _STREAM_END:
                # 收到哨兵，流结束
                break
            yield item.to_sse()
