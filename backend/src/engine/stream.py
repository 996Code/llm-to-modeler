"""SSE 桥接 - 把 ToolDispatcher 的执行桥接到 SSE 流。

stream_dispatcher:走 ToolDispatcher 的 SSE 流(新架构)。
"""
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from api.sse import SSEEvent, StreamManager
from sdk.tool import ToolResult

logger = logging.getLogger(__name__)


async def stream_dispatcher(
    dispatcher,
    user_input: str,
    conversation_id: str,
    user_id: str,
    conversation_store=None,
    conversation_history: list = None,
    current_config: dict = None,
    forward_headers: dict = None,
) -> AsyncGenerator[str, None]:
    """走 ToolDispatcher 的 SSE 流(新架构)。

    在线程池执行 dispatcher.run,emit 回调线程安全桥接到 SSE。
    """
    loop = asyncio.get_running_loop()
    sm = StreamManager(loop)

    # 线程安全的 emit 回调
    def emit(event_type: str, message: str = "", **extra):
        sm.stage(event_type, message, **extra)

    def _run_dispatcher():
        """在线程池执行 dispatcher.run。"""
        from src.services.upstream_client import set_forward_headers
        set_forward_headers(forward_headers)
        try:
            return dispatcher.run(
                user_input=user_input,
                conv_id=conversation_id,
                forward_headers=forward_headers,
                current_config=current_config,
                conversation_history=conversation_history,
                emit=emit,
            )
        finally:
            set_forward_headers(None)

    async def execute():
        try:
            result = await loop.run_in_executor(None, _run_dispatcher)

            # 按 ToolResult 三态分流
            if result.ask is not None:
                # 追问
                questions_text = "我需要确认一些信息：\n" + "\n".join(
                    f"{i+1}. {q.question}" for i, q in enumerate(result.ask.questions)
                )
                await sm.emit_result({
                    "needsClarification": True,
                    "questions": [q.model_dump() for q in result.ask.questions],
                    "summary": questions_text,
                })
                _save_conversation(conversation_store, conversation_id, user_id,
                                   user_input, questions_text)

            elif result.error_for_llm:
                # 错误回流
                await sm.emit_result({
                    "error": True,
                    "message": result.error_for_llm,
                    "summary": result.summary,
                })
                _save_conversation(conversation_store, conversation_id, user_id,
                                   user_input, result.summary)

            elif result.reply:
                # 闲聊回复
                await sm.emit_result({
                    "intent": "general",
                    "reply": result.reply,
                    "summary": result.summary,
                })
                _save_conversation(conversation_store, conversation_id, user_id,
                                   user_input, result.reply)

            elif result.artifact:
                # 配置制品 - 通过 tool.format_result() 钩子化提取前端字段
                # Engine 不直接读制品内部结构(架构试金石)
                config = result.artifact
                # pack 通过 ToolResult.extra["formatted"] 传递格式化结果
                # Engine 只做透传,不解析制品字段名
                formatted = result.extra.get("formatted", {})
                is_valid = len(result.extra.get("validation_errors", [])) == 0

                # SSE payload:通用字段 + pack 的 formatted 透传
                sse_payload = {
                    "config": config,
                    "valid": is_valid,
                    "validationErrors": result.extra.get("validation_errors", []),
                    "summary": result.summary,
                }
                sse_payload.update(formatted)  # 透传 pack 提供的字段(fieldCount 等)
                await sm.emit_result(sse_payload)

                # 保存到对话历史
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
                            content=result.summary,
                            config_snapshot=config,
                        )
                        # 更新对话配置
                        if current_config:
                            conversation_store.update_conversation_config(
                                conversation_id, config
                            )
                        else:
                            # 标题从 pack 的 formatted 透传获取
                            title = formatted.get("title", "新对话")
                            conversation_store.update_conversation_config(
                                conversation_id, config, title=title
                            )
                    except Exception as e:
                        logger.warning(f"Failed to save conversation: {e}")
            else:
                await sm.emit_error("未能生成结果")

            await sm.emit_done()

        except Exception as e:
            logger.exception("Dispatcher execution failed")
            await sm.emit_error(str(e), type=type(e).__name__)
            await sm.emit_done()

    task = asyncio.create_task(execute())

    # 流式产出 SSE 事件
    async for event in sm.stream():
        yield event

    await task


def _save_conversation(store, conv_id, user_id, user_input, assistant_content):
    """保存对话到 store(简单版,异常不崩)。"""
    if not store or not conv_id or not user_id:
        return
    try:
        store.add_message(conv_id=conv_id, role="user", content=user_input)
        store.add_message(conv_id=conv_id, role="assistant", content=assistant_content)
    except Exception as e:
        logger.warning(f"Failed to save conversation: {e}")
