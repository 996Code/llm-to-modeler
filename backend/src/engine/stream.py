"""SSE 桥接 - 把 LangGraph StateGraph 的执行桥接到 SSE 流。

stream_graph:走 LangGraph StateGraph 的 SSE 流(新架构)。

替代旧 stream_dispatcher,改为消费 LangGraph 的事件流:
- graph.stream(input, config) 产出节点更新(同步)
- 在线程池中逐 chunk 处理,实时推 SSE 事件(不等全部完成)
- 解析 sse_events 列表 → 发 SSE stage/pipeline_definition/result 事件
- 处理 GraphInterrupt 事件 → 发 SSE needsClarification
- 追问恢复:传入 Command(resume=answers) 继续执行
"""
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from langgraph.types import Command

from api.sse import SSEEvent, StreamManager
from sdk.tool import ToolResult
from engine.compression import build_compressed_history

logger = logging.getLogger(__name__)


async def stream_graph(
    graph,
    user_input: str,
    conversation_id: str,
    user_id: str,
    answers: dict = None,
    image_base64: str = None,
    conversation_store=None,
    conversation_history: list = None,
    current_config: dict = None,
    forward_headers: dict = None,
) -> AsyncGenerator[str, None]:
    """走 LangGraph StateGraph 的 SSE 流。

    核心设计:
    - graph.stream 是同步 API,在线程池中执行
    - 每个 chunk 产出后立即通过 StreamManager 推 SSE 事件(实时进度)
    - interrupt 时 graph.stream 自动停止,检查 graph.get_state() 获取 interrupt 数据

    Args:
        graph: CompiledStateGraph 实例
        user_input: 用户消息
        conversation_id: 会话 ID(用作 checkpoint thread_id)
        user_id: 用户 ID
        answers: 追问回答(非空时走 Command(resume=answers) 路径)
        image_base64: 图片 base64 编码(用于 ImageFormTool)
        conversation_store: ConversationStore 实例
        conversation_history: 对话历史
        current_config: 当前已有配置
        forward_headers: 嵌入模式透传的请求头
    """
    loop = asyncio.get_running_loop()
    sm = StreamManager(loop)

    # 构建 input
    if answers:
        input_data = Command(resume=answers)
    else:
        input_data = {
            "user_input": user_input,
            "conversation_history": conversation_history or [],
            "compressed_history": build_compressed_history(conversation_history),
            "conversation_id": conversation_id,
            "forward_headers": forward_headers or {},
            "current_config": current_config,
            "tool_name": "",
            "intent_reason": "",
            "tool_state": {"image_base64": image_base64} if image_base64 else {},
            "tool_result": None,
            "pending_questions": [],
            "clarify_answers": {},
            "sse_events": [],
        }

    # Checkpoint config:用 conversation_id 做 thread_id
    config = {"configurable": {"thread_id": conversation_id or "default"}}

    # 用于在线程和异步之间传递结果
    result_holder = {"last_result": None, "had_interrupt": False}

    async def execute():
        try:
            # ── 在线程池中执行 graph.stream,逐 chunk 实时推 SSE ──
            def _process_chunk(chunk):
                """在线程池中处理单个 chunk,通过 call_soon_threadsafe 推 SSE。"""
                # 错误
                if "__error__" in chunk:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(sm.emit_error(chunk["__error__"]))
                    )
                    return

                # chunk 格式: {node_name: state_update}
                for node_name, state_update in chunk.items():
                    if not isinstance(state_update, dict):
                        continue

                    # 1. 处理 sse_events(节点产出的事件)
                    sse_events = state_update.get("sse_events", [])
                    for event in sse_events:
                        event_type = event.get("type", "")

                        if event_type == "stage":
                            sm.stage(
                                event.get("stage", ""),
                                event.get("message", ""),
                            )

                        elif event_type == "pipeline_definition":
                            data = event.get("data", {})
                            sm.pipeline_definition(
                                data.get("tool", ""),
                                data.get("steps", []),
                            )

                        elif event_type == "result":
                            result_data = event.get("data", {})
                            result_holder["last_result"] = result_data
                            loop.call_soon_threadsafe(
                                lambda rd=result_data: asyncio.ensure_future(sm.emit_result(rd))
                            )

                        elif event_type == "error":
                            loop.call_soon_threadsafe(
                                lambda: asyncio.ensure_future(
                                    sm.emit_error(event.get("data", {}).get("error", "未知错误"))
                                )
                            )

            def _run_graph():
                """在线程池中执行 graph.stream,逐 chunk 处理。"""
                try:
                    for chunk in graph.stream(input_data, config):
                        _process_chunk(chunk)
                except Exception as e:
                    logger.exception(f"Graph execution failed: {e}")
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(sm.emit_error(str(e)))
                    )

            await loop.run_in_executor(None, _run_graph)

            # ── 检查是否有 interrupt 未处理 ──
            try:
                state_snapshot = graph.get_state(config)
                if state_snapshot and state_snapshot.tasks:
                    for task in state_snapshot.tasks:
                        if task.interrupts:
                            result_holder["had_interrupt"] = True
                            for intr in task.interrupts:
                                intr_value = intr.value if hasattr(intr, 'value') else intr
                                if isinstance(intr_value, dict):
                                    await sm.emit_result({
                                        "needsClarification": True,
                                        "questions": intr_value.get("questions", []),
                                        "summary": intr_value.get("summary", "需要补充信息"),
                                    })
                                    _save_conversation(
                                        conversation_store, conversation_id, user_id,
                                        user_input, intr_value.get("summary", "需要补充信息"),
                                    )
            except Exception as e:
                logger.warning(f"Failed to check graph state for interrupts: {e}")

            # 保存正常结果的对话
            if result_holder["last_result"] and not result_holder["had_interrupt"]:
                _save_result_conversation(
                    conversation_store, conversation_id, user_id,
                    user_input, result_holder["last_result"], current_config,
                )

            await sm.emit_done()

        except Exception as e:
            logger.exception("Graph stream execution failed")
            await sm.emit_error(str(e), type=type(e).__name__)
            await sm.emit_done()

    task = asyncio.create_task(execute())

    # 流式产出 SSE 事件
    async for event in sm.stream():
        yield event

    await task


# ── 辅助函数 ──────────────────────────────────────────────


def _save_conversation(store, conv_id, user_id, user_input, assistant_content):
    """保存对话到 store(简单版,异常不崩)。"""
    if not store or not conv_id or not user_id:
        return
    try:
        store.add_message(conv_id=conv_id, role="user", content=user_input)
        store.add_message(conv_id=conv_id, role="assistant", content=assistant_content)
    except Exception as e:
        logger.warning(f"Failed to save conversation: {e}")


def _save_result_conversation(store, conv_id, user_id, user_input, result_data, current_config):
    """保存工具结果到对话历史。"""
    if not store or not conv_id or not user_id:
        return
    try:
        summary = result_data.get("summary", "")

        store.add_message(conv_id=conv_id, role="user", content=user_input)
        store.add_message(conv_id=conv_id, role="assistant", content=summary)

        # 配置结果:存 config_snapshot + 更新对话配置
        config = result_data.get("config")
        if config:
            store.add_message(
                conv_id=conv_id, role="assistant",
                content=summary, config_snapshot=config,
            )
            if current_config:
                store.update_conversation_config(conv_id, config)
            else:
                title = result_data.get("formName", result_data.get("title", "新对话"))
                store.update_conversation_config(conv_id, config, title=title)
    except Exception as e:
        logger.warning(f"Failed to save result conversation: {e}")
