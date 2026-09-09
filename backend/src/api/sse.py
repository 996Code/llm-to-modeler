"""向后兼容转发:SSE 事件管线已下沉 engine/sse.py(纯 asyncio 原语,
引擎自持;api/tasks 等经 engine 引用)。"""
from engine.sse import SSEEvent, StreamManager  # noqa: F401
