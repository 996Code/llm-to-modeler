"""
SSE Streaming Module

Streams workflow execution progress via Server-Sent Events.

Key design: the workflow runs in a thread pool (run_in_executor), but
the SSE queue is an asyncio.Queue (event loop thread). We bridge the
two using loop.call_soon_threadsafe() so nodes running in the worker
thread can push progress events safely.
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


async def stream_workflow(
    workflow,
    user_input: str,
    current_config: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    conversation_store=None,
    conversation_history: Optional[list] = None,
    forward_headers: Optional[Dict[str, str]] = None,
) -> AsyncGenerator[str, None]:
    """
    Execute workflow in a thread pool and stream real-time progress via SSE.

    The progress_callback (sm.stage) is passed to the workflow, so each
    node reports its status as it runs. The callback is thread-safe.

    Args:
        forward_headers: Headers to forward to upstream modeler (set as
                         thread-local before running the workflow).
    """
    loop = asyncio.get_running_loop()
    sm = StreamManager(loop)

    # The progress callback — called from worker thread by nodes
    def progress(stage: str, message: str, **extra):
        sm.stage(stage, message, **extra)

    def _run_workflow():
        """Runs in worker thread — set forward headers before executing."""
        from src.services.upstream_client import set_forward_headers
        set_forward_headers(forward_headers)
        try:
            return workflow.run(
                user_input=user_input,
                current_config=current_config,
                conversation_history=conversation_history,
                conversation_id=conversation_id,
                user_id=user_id,
                progress_callback=progress,
            )
        finally:
            set_forward_headers(None)  # clean up thread-local

    async def execute():
        try:
            final_state = await loop.run_in_executor(None, _run_workflow)

            # Check general reply (闲聊)
            if final_state.intent == "general" and final_state.general_reply:
                reply = final_state.general_reply
                await sm.emit_result({
                    "intent": "general",
                    "summary": reply,
                })
                if conversation_store and conversation_id and user_id:
                    try:
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="user", content=user_input)
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="assistant", content=reply)
                    except Exception as e:
                        logger.warning(f"Failed to save conversation: {e}")
                await sm.emit_done()
                return

            config = final_state.current_config
            is_modify = final_state.intent == "modify"

            # Check if workflow stopped for clarification
            if final_state.needs_clarification:
                questions = final_state.clarification_questions
                clarification_text = "我需要确认一些信息：\n" + "\n".join(
                    f"{i+1}. {q}" for i, q in enumerate(questions)
                )

                await sm.emit_result({
                    "needsClarification": True,
                    "questions": questions,
                    "summary": clarification_text,
                })

                # Save clarification question to conversation
                if conversation_store and conversation_id and user_id:
                    try:
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="user",
                            content=user_input,
                        )
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="assistant",
                            content=clarification_text,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save conversation: {e}")

                await sm.emit_done()
                return

            if config:
                field_count = len(config.get("formFieldConfigVos", []))
                is_valid = len(final_state.validation_errors) == 0

                # Summary differs for create vs modify
                if is_modify:
                    summary_text = f"已修改「{config.get('formName', '表单')}」，当前 {field_count} 个字段"
                else:
                    summary_text = f"已生成「{config.get('formName', '表单')}」，包含 {field_count} 个字段"
                summary_text += "，校验通过" if is_valid else f"，{len(final_state.validation_errors)} 个校验问题"

                await sm.emit_result({
                    "config": config,
                    "valid": is_valid,
                    "fieldCount": field_count,
                    "formName": config.get("formName", ""),
                    "formCode": config.get("formCode", ""),
                    "validationErrors": final_state.validation_errors,
                    "summary": summary_text,
                })

                # Save to conversation history
                if conversation_store and conversation_id and user_id:
                    try:
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="user",
                            content=user_input,
                        )
                        conversation_store.add_message(
                            conv_id=conversation_id,
                            role="assistant",
                            content=summary_text,
                            config_snapshot=config,
                        )
                        # Only update title on CREATE; preserve title on modify
                        if is_modify:
                            conversation_store.update_conversation_config(
                                conversation_id, config
                            )
                        else:
                            title = config.get("formName", "新对话")
                            conversation_store.update_conversation_config(
                                conversation_id, config, title=title
                            )
                    except Exception as e:
                        logger.warning(f"Failed to save conversation: {e}")
            else:
                await sm.emit_error("未能生成配置")

            await sm.emit_done()

        except Exception as e:
            logger.exception("Workflow execution failed")
            await sm.emit_error(str(e), type=type(e).__name__)
            await sm.emit_done()

    task = asyncio.create_task(execute())

    async for event in sm.stream():
        yield event

    await task
