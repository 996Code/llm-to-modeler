"""LangGraph 节点函数 — StateGraph 的各个节点实现。

节点:
  - classify_intent: LLM 选工具(从 registry 动态生成 prompt)
  - execute_tool:    执行选中的工具,支持 interrupt/restore 追问
  - handle_result:   处理工具结果,分流到 SSE

设计原则:
  - 节点函数签名: (state: GraphState) -> dict(部分更新)
  - 工具内部 state 通过 tool_state 透传,Graph 不读内部结构
  - 追问通过 LangGraph interrupt() 实现,不用自研 save_pending_ask
  - emit 回调通过 sse_events 列表传递,由 stream.py 消费
"""
import logging
from typing import Any, Dict, Optional

from langgraph.types import interrupt

from engine.graph_state import GraphState
from sdk.tool import Tool, ToolResult, ToolContext, AskSpec
from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── 节点间共享的依赖(由 graph.py 在构建时注入) ──
# LangGraph 节点函数签名只能是 (state) -> dict,
# 外部依赖通过闭包或 module-level 变量注入。
_registry: Optional[ToolRegistry] = None
_llm_client: Any = None
_asset_client: Any = None
_conversation: Any = None
_prompt_loader: Any = None


def configure(
    registry: ToolRegistry,
    llm_client: Any,
    asset_client: Any,
    conversation: Any = None,
    prompt_loader: Any = None,
):
    """注入共享依赖(由 graph.py 构建时调用一次)。"""
    global _registry, _llm_client, _asset_client, _conversation, _prompt_loader
    _registry = registry
    _llm_client = llm_client
    _asset_client = asset_client
    _conversation = conversation
    _prompt_loader = prompt_loader


# ── 节点函数 ──────────────────────────────────────────────


def classify_intent_node(state: GraphState) -> dict:
    """LLM 意图识别:从 registry 动态生成 prompt,选择最合适的工具。

    输出: tool_name, intent_reason, tool_state, sse_events
    """
    user_input = state.get("user_input", "")
    compressed_history = state.get("compressed_history", "")
    has_existing_config = state.get("current_config") is not None

    # SSE 事件:告知前端正在识别意图
    sse_events = [{"type": "stage", "stage": "classify_intent", "message": "正在理解您的意图..."}]

    # 动态构建意图识别 prompt
    system_prompt = _build_intent_prompt(has_existing_config)

    # 构建 user message
    parts = []
    if compressed_history:
        parts.extend(["## 对话历史", compressed_history, ""])
    parts.extend([
        f"## 是否有已有配置：{'是' if has_existing_config else '否'}",
        "",
        "## 用户消息",
        user_input,
        "",
        "请判断意图并输出 JSON。",
    ])
    user_msg = "\n".join(parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    tool_name = ""
    intent_reason = ""

    try:
        parsed = _llm_client.chat_json(messages)
        tool_names = parsed.get("tools", [])
        intent_reason = parsed.get("reason", "")

        if tool_names:
            for name in tool_names:
                tool = _registry.get(name)
                if tool:
                    # 安全检查:需要已有配置的工具,如果没有配置则跳过
                    if getattr(tool, 'requires_existing_artifact', False) and not has_existing_config:
                        logger.info(f"Safety: {name} requires existing config but none found, skipping")
                        continue
                    tool_name = name
                    break
    except Exception as e:
        logger.warning(f"Intent classification LLM failed: {e}")

    # 兜底:没选到工具时用 fallback
    if not tool_name:
        tool_name = _get_fallback_tool_name()
        intent_reason = "fallback"

    return {
        "tool_name": tool_name,
        "intent_reason": intent_reason,
        "tool_state": {
            "user_input": user_input,
            "compressed_history": compressed_history,
            "source_artifact": state.get("current_config"),
            "conversation_id": state.get("conversation_id", ""),
            "forward_headers": state.get("forward_headers", {}),
        },
        "sse_events": sse_events,
    }


def execute_tool_node(state: GraphState) -> dict:
    """执行选中的工具。

    核心逻辑:
    1. 从 registry 取工具,构建 ToolContext
    2. 执行 tool.execute()
    3. 如果 ToolResult.ask 非空 → 调用 interrupt() 挂起
       - interrupt value = {questions, summary}
       - resume 后 answers 会作为 interrupt() 的返回值
    4. 如果是恢复(resume),把 answers 注入 tool_state 后重跑工具
    """
    tool_name = state.get("tool_name", "")
    tool_state = state.get("tool_state", {})

    tool = _registry.get(tool_name)
    if tool is None:
        return {
            "tool_result": ToolResult(
                error_for_llm=f"工具 {tool_name} 不存在",
                summary="工具选择失败",
            ).model_dump(),
            "sse_events": [],
        }

    # 构建 SSE emit 回调 → 收集到 sse_events 列表
    sse_events = []

    def emit(*args, **kwargs):
        """emit(event_type, stage_name, message, **extra)"""
        if len(args) >= 3:
            sse_events.append({
                "type": "stage",
                "stage": args[1],
                "message": args[2],
            })
        elif len(args) == 2:
            event_type = args[0]
            if event_type == "pipeline_definition":
                sse_events.append({
                    "type": "pipeline_definition",
                    "data": args[1],
                })
            else:
                sse_events.append({
                    "type": "stage",
                    "stage": args[1],
                    "message": "",
                })

    # 构建 ToolContext
    ctx = ToolContext(
        llm_client=_llm_client,
        asset_client=_asset_client,
        conversation=_conversation,
        emit=emit,
        forward_headers=tool_state.get("forward_headers", {}),
        conv_id=tool_state.get("conversation_id"),
        registry=_registry,
    )
    object.__setattr__(ctx, "prompt_loader", _prompt_loader)

    # ── 追问恢复:把 clarify_answers 注入 tool_state ──
    clarify_answers = state.get("clarify_answers", {})
    if clarify_answers:
        tool_state["clarify_answers"] = clarify_answers

    # ── 执行工具 ──
    try:
        result = tool.execute(tool_state, ctx)
    except Exception as e:
        logger.exception(f"Tool {tool_name} execution failed")
        result = ToolResult(
            error_for_llm=str(e),
            summary=f"工具执行失败: {e}",
        )

    # ── 处理追问:interrupt! ──
    if result.ask is not None:
        # 构建 interrupt value(发给前端的数据)
        questions_data = [q.model_dump() for q in result.ask.questions]
        questions_text = "我需要确认一些信息：\n" + "\n".join(
            f"{i+1}. {q.question}" for i, q in enumerate(result.ask.questions)
        )

        interrupt_value = {
            "questions": questions_data,
            "summary": questions_text,
        }

        # ★ LangGraph interrupt:挂起执行,等待 Command(resume=answers)
        # resume 后,answer 就是用户在前端输入的回答
        answer = interrupt(interrupt_value)

        # ── resume 后到这里 ──
        # 把用户的回答注入 tool_state,准备重跑工具
        logger.info(f"Resumed with answer: {answer}")
        tool_state["clarify_answers"] = answer if isinstance(answer, dict) else {"text": str(answer)}

        # ★ 关键:清除上一轮的中断标记,否则 run_pipeline 会在第一步前就 break,
        #   导致 _step_parse_info 永远不会消费 clarify_answers
        tool_state.pop("_need_clarify", None)
        tool_state.pop("_clarify_spec", None)
        tool_state.pop("_clarify_summary", None)

        # 清空 tool_result 和 pending_questions,触发重跑
        return {
            "tool_state": tool_state,
            "tool_result": None,
            "pending_questions": [],
            "clarify_answers": {},
            "sse_events": sse_events,
        }

    # 正常完成:返回工具结果
    return {
        "tool_result": result.model_dump(),
        "sse_events": sse_events,
    }


def handle_result_node(state: GraphState) -> dict:
    """处理工具结果:根据 ToolResult 三态分流。

    三态:
    - error_for_llm: 错误回流
    - reply: 闲聊回复
    - ask: 追问(已在 execute_tool 中处理 interrupt)
    - artifact: 制品结果(config/data)
    - 都没有: 未知结果

    输出 sse_events 供 stream.py 消费。
    """
    tool_result_data = state.get("tool_result")
    if tool_result_data is None:
        # 可能是追问后需要重跑,不应该到这里
        return {"sse_events": []}

    # 从 dict 重建 ToolResult(方便读取字段)
    result = ToolResult.model_validate(tool_result_data)

    sse_events = []

    if result.error_for_llm:
        sse_events.append({
            "type": "result",
            "data": {"error": True, "message": result.error_for_llm, "summary": result.summary},
        })

    elif result.reply:
        sse_events.append({
            "type": "result",
            "data": {"intent": "general", "reply": result.reply, "summary": result.summary},
        })

    elif result.artifact:
        artifact_type = getattr(result, 'artifact_type', 'config')
        config = result.artifact
        formatted = result.extra.get("formatted", {})
        is_valid = len(result.extra.get("validation_errors", [])) == 0

        if artifact_type == "data":
            payload = {
                "artifactType": "data",
                "data": config,
                "summary": result.summary,
            }
            payload.update(formatted)
            sse_events.append({"type": "result", "data": payload})
        else:
            # 归一化 validationErrors:统一为 [{message: str}] 格式
            raw_errors = result.extra.get("validation_errors", [])
            normalized_errors = [
                {"message": e} if isinstance(e, str) else e
                for e in raw_errors
            ]
            payload = {
                "config": config,
                "valid": is_valid,
                "validationErrors": normalized_errors,
                "summary": result.summary,
            }
            payload.update(formatted)
            sse_events.append({"type": "result", "data": payload})

    else:
        sse_events.append({
            "type": "error",
            "data": {"error": "未能生成结果"},
        })

    return {"sse_events": sse_events}


# ── 条件边函数 ──────────────────────────────────────────────


def route_by_tool(state: GraphState) -> str:
    """classify_intent 之后:根据 tool_name 路由。

    所有工具都走 execute_tool 节点(包括 chat),
    因为 execute_tool 内部会统一处理 ToolResult 三态。
    """
    tool_name = state.get("tool_name", "")
    if tool_name:
        return "tool"
    return "end"


def route_after_result(state: GraphState) -> str:
    """handle_result 之后:如果 tool_result 为空说明需要重跑(追问恢复)。"""
    tool_result = state.get("tool_result")
    if tool_result is None and state.get("tool_name"):
        # 追问恢复后重跑工具
        return "rerun"
    return "done"


# ── 辅助函数 ──────────────────────────────────────────────


def _build_intent_prompt(has_existing_config: bool) -> str:
    """动态构建意图识别 prompt,从 registry.all() 读取工具描述。"""
    tools_desc = []
    for tool in _registry.all():
        requires_artifact = getattr(tool, 'requires_existing_artifact', False)
        condition = " (仅当 has_existing_config=true)" if requires_artifact else ""
        tools_desc.append(f"- {tool.name}: {tool.when}{condition}")

    tools_list = "\n".join(tools_desc)

    return (
        "你是意图识别器。根据用户消息选择最合适的工具,只返回 JSON。\n\n"
        f"可选工具:\n{tools_list}\n\n"
        f"当前 has_existing_config={has_existing_config}\n"
        '输出格式: {"tools": ["tool_name"], "reason": "简短理由"}'
    )


def _get_fallback_tool_name() -> str:
    """获取兜底工具名(优先级:chat → 安全只读工具 → 第一个非 artifact 工具)。"""
    chat_tool = _registry.get("chat")
    if chat_tool:
        return "chat"
    for tool in _registry.all():
        if getattr(tool, 'is_read_only', False) and not getattr(tool, 'is_destructive', True):
            return tool.name
    for tool in _registry.all():
        if not getattr(tool, 'requires_existing_artifact', False):
            return tool.name
    return "chat"  # 兜底
