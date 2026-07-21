"""
SSE Streaming Module

Streams ToolDispatcher execution progress via Server-Sent Events.

Key design: the dispatcher runs in a thread pool (run_in_executor), but
the SSE queue is an asyncio.Queue (event loop thread). We bridge the
two using loop.call_soon_threadsafe() so tools running in the worker
thread can push progress events safely.

Note: The old stream_workflow() function (LangGraph-based) has been removed.
All SSE streaming now goes through engine/stream.py::stream_dispatcher().
"""

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)

_STREAM_END = object()  # Sentinel to signal stream end


class SSEEvent:
    """Server-Sent Event."""

    def __init__(self, event: str, data: Dict[str, Any]):
        self.event = event
        self.data = data

    def to_sse(self) -> str:
        """Format as SSE string (terminated with \\n\\n per spec)."""
        data_str = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.event}\ndata: {data_str}\n\n"


class StreamManager:
    """Manages SSE event queue (thread-safe via loop bridge)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()

    def stage(self, stage: str, message: str, **extra):
        """
        Push a stage event. Thread-safe — can be called from worker threads.

        This is the progress_callback passed to the workflow.
        """
        data = {"stage": stage, "message": message, **extra}
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, SSEEvent("stage", data)
        )

    def pipeline_definition(self, tool_name: str, steps: list):
        """
        Push pipeline definition event. Thread-safe.
        
        This allows frontend to dynamically render pipeline steps
        instead of hardcoding them.
        """
        data = {"tool": tool_name, "steps": steps}
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, SSEEvent("pipeline_definition", data)
        )

    async def emit_result(self, data: Dict[str, Any]):
        await self._queue.put(SSEEvent("result", data))

    async def emit_error(self, message: str, **extra):
        await self._queue.put(SSEEvent("error", {"error": message, **extra}))

    async def emit_done(self):
        await self._queue.put(SSEEvent("done", {"status": "done"}))
        await self._queue.put(_STREAM_END)

    async def stream(self) -> AsyncGenerator[str, None]:
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=120.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is _STREAM_END:
                break
            yield item.to_sse()
