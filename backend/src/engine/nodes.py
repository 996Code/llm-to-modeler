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
    user_input = state.get("user_input", "")  # 当前用户输入，类比 Java 的 request 参数
    compressed_history = state.get("compressed_history", "")  # 压缩后的历史，省 token
    # 是否已有配置：决定可用的工具集（如"修改"类工具需要已有配置）
    has_existing_config = state.get("current_config") is not None

    # SSE 事件:告知前端正在识别意图
    # 类比 Java：服务端推送状态给前端，让 UI 显示 loading 文案
    sse_events = [{"type": "stage", "stage": "classify_intent", "message": "正在理解您的意图..."}]

    # 动态构建意图识别 prompt（工具列表从 registry 读，而非硬编码）
    system_prompt = _build_intent_prompt(has_existing_config)

    # 构建 user message
    # 用列表 extend + join 拼字符串，比 f-string 多行更可控（类比 Java StringBuilder）
    parts = []
    if compressed_history:
        # 有历史才追加历史段，避免空段干扰 LLM
        parts.extend(["## 对话历史", compressed_history, ""])  # 末尾空串是 join 后的空行分隔
    parts.extend([
        f"## 是否有已有配置：{'是' if has_existing_config else '否'}",
        "",
        "## 用户消息",
        user_input,
        "",
        "请判断意图并输出 JSON。",
    ])
    user_msg = "\n".join(parts)  # 拼成多段 markdown 文本

    messages = [
        {"role": "system", "content": system_prompt},  # 系统指令：工具列表 + 输出格式
        {"role": "user", "content": user_msg},  # 用户输入 + 上下文
    ]

    tool_name = ""  # 选中的工具名，空表示未选中
    intent_reason = ""  # 选中理由，用于日志和调试

    try:
        # chat_json：要求 LLM 输出 JSON，自动走降级（json_object / 纯文本提取）
        parsed = _llm_client.chat_json(messages)
        tool_names = parsed.get("tools", [])  # LLM 返回的工具名列表
        intent_reason = parsed.get("reason", "")  # LLM 给的选择理由

        if tool_names:
            # 遍历 LLM 推荐的工具名，取第一个"可用"的
            # 类比 Java stream().filter(...).findFirst()
            for name in tool_names:
                tool = _registry.get(name)
                if tool:
                    # 安全检查:需要已有配置的工具,如果没有配置则跳过
                    # 例：update_form 需要 current_config，没有配置时不能用
                    if getattr(tool, 'requires_existing_artifact', False) and not has_existing_config:
                        logger.info(f"Safety: {name} requires existing config but none found, skipping")
                        continue  # 跳过不满足前置条件的工具，继续看下一个推荐
                    tool_name = name  # 命中第一个可用的，跳出循环
                    break
    except Exception as e:
        # LLM 调用失败：记 warning 但不崩，走兜底逻辑
        logger.warning(f"Intent classification LLM failed: {e}")

    # 兜底:没选到工具时用 fallback
    # 类比 Java：try-catch 兜底默认值，保证流程不中断
    if not tool_name:
        tool_name = _get_fallback_tool_name()  # 通常兜底到 chat 闲聊工具
        intent_reason = "fallback"  # 标记为兜底，便于日志区分

    return {
        "tool_name": tool_name,  # 选中的工具名（路由依据）
        "intent_reason": intent_reason,  # 选择理由（日志用）
        # tool_state：透传给工具的内部状态，Graph 不读结构
        # 类比 Java：把 request-scoped 数据放进 ThreadLocal 传给下游 service
        "tool_state": {
            "user_input": user_input,
            "compressed_history": compressed_history,
            "source_artifact": state.get("current_config"),  # 已有配置（修改类工具需要）
            "conversation_id": state.get("conversation_id", ""),
            "forward_headers": state.get("forward_headers", {}),  # 透传鉴权头
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
    tool_name = state.get("tool_name", "")  # 上游 classify_intent 选中的工具
    tool_state = state.get("tool_state", {})  # 工具内部状态

    tool = _registry.get(tool_name)  # 从注册表取工具实例
    if tool is None:
        # 工具不存在：构造一个 error 结果返回，不让流程崩
        # 类比 Java：抛业务异常但包装成统一响应体
        return {
            "tool_result": ToolResult(
                error_for_llm=f"工具 {tool_name} 不存在",
                summary="工具选择失败",
            ).model_dump(),
            "sse_events": [],
        }

    # 构建 SSE emit 回调 → 收集到 sse_events 列表
    # 类比 Java：把观察者回调注入工具，工具内部 emit 时触发收集
    sse_events = []

    def emit(*args, **kwargs):
        """emit(event_type, stage_name, message, **extra)"""
        # 可变参数 *args：兼容不同签名（3参带消息 / 2参只有阶段名）
        if len(args) >= 3:
            # 3参签名：emit(type, stage, message) —— 标准阶段进度事件
            sse_events.append({
                "type": "stage",
                "stage": args[1],
                "message": args[2],
            })
        elif len(args) == 2:
            event_type = args[0]
            if event_type == "pipeline_definition":
                # pipeline_definition：特殊的结构化事件，传整个管线定义给前端渲染
                sse_events.append({
                    "type": "pipeline_definition",
                    "data": args[1],
                })
            else:
                # 2参但非 pipeline_definition：只有阶段名，消息留空
                sse_events.append({
                    "type": "stage",
                    "stage": args[1],
                    "message": "",
                })

    # 构建 ToolContext
    # 类比 Java：构造方法上下文对象，把所有依赖打包传给工具
    ctx = ToolContext(
        llm_client=_llm_client,  # LLM 客户端（工具内部可能要调 LLM）
        asset_client=_asset_client,  # 资产客户端（读写上游配置）
        conversation=_conversation,  # 会话存储（日志）
        emit=emit,  # SSE 回调（进度推送）
        forward_headers=tool_state.get("forward_headers", {}),  # 透传鉴权头
        conv_id=tool_state.get("conversation_id"),  # 会话 ID
        registry=_registry,  # 工具注册表（工具可能要调其他工具）
    )
    # object.__setattr__：绕过 Pydantic 的 frozen 限制，临时挂 prompt_loader
    # 类比 Java：反射注入非构造字段
    object.__setattr__(ctx, "prompt_loader", _prompt_loader)

    # ── 追问恢复:把 clarify_answers 注入 tool_state ──
    # 如果 state 里有 clarify_answers，说明这是 interrupt 恢复后的重跑
    clarify_answers = state.get("clarify_answers", {})
    if clarify_answers:
        tool_state["clarify_answers"] = clarify_answers  # 注入回答供工具消费

    # ── 执行工具 ──
    try:
        # 执行工具主逻辑，类比 Java toolService.execute(state, ctx)
        result = tool.execute(tool_state, ctx)
    except Exception as e:
        # 工具执行抛异常：包装成 error 结果，不让流程崩
        logger.exception(f"Tool {tool_name} execution failed")
        result = ToolResult(
            error_for_llm=str(e),
            summary=f"工具执行失败: {e}",
        )

    # ── 处理追问:interrupt! ──
    if result.ask is not None:
        # ask 非空：工具需要向用户追问更多信息
        # 构建 interrupt value(发给前端的数据)
        # model_dump()：Pydantic 对象转 dict，类比 Jackson 序列化
        questions_data = [q.model_dump() for q in result.ask.questions]
        # 拼人类可读的追问文案，供前端展示
        questions_text = "我需要确认一些信息：\n" + "\n".join(
            f"{i+1}. {q.question}" for i, q in enumerate(result.ask.questions)
        )

        interrupt_value = {
            "questions": questions_data,  # 结构化问题（前端渲染表单用）
            "summary": questions_text,  # 文本摘要（前端直接展示用）
        }

        # ★ LangGraph interrupt:挂起执行,等待 Command(resume=answers)
        # 这是整个追问机制的核心：interrupt() 会抛 PauseSignal 挂起当前执行
        # 框架把 interrupt_value 序列化保存，前端展示问题
        # 用户回答后，调用 Command(resume=answer)，框架恢复执行
        # resume 后,answer 就是用户在前端输入的回答
        answer = interrupt(interrupt_value)

        # ── resume 后到这里 ──
        # 把用户的回答注入 tool_state,准备重跑工具
        logger.info(f"Resumed with answer: {answer}")
        # answer 可能是 dict（表单回答）或字符串，统一成 dict 结构
        tool_state["clarify_answers"] = answer if isinstance(answer, dict) else {"text": str(answer)}

        # ★ 关键:清除上一轮的中断标记,否则 run_pipeline 会在第一步前就 break,
        #   导致 _step_parse_info 永远不会消费 clarify_answers
        # pop 第二参 None：key 不存在不报错，类比 Java map.remove + null check
        tool_state.pop("_need_clarify", None)
        tool_state.pop("_clarify_spec", None)
        tool_state.pop("_clarify_summary", None)

        # 清空 tool_result 和 pending_questions,触发重跑
        # 返回 None 的 tool_result 会被 route_after_result 识别为"需要重跑"
        return {
            "tool_state": tool_state,
            "tool_result": None,  # 关键：None 触发 rerun 路由
            "pending_questions": [],  # 清空待问问题（已问过）
            "clarify_answers": {},  # 清空（已注入 tool_state，避免重复）
            "sse_events": sse_events,
        }

    # 正常完成:返回工具结果
    return {
        "tool_result": result.model_dump(),  # 序列化工具结果供 handle_result 消费
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
        # tool_result 为 None：说明追问恢复后需要重跑，本节点不该被调到
        # 返回空事件，让路由把流程带回 execute_tool 重跑
        return {"sse_events": []}

    # 从 dict 重建 ToolResult(方便读取字段)
    # model_validate 是 Pydantic 从 dict 反序列化，类比 Jackson readValue / @ModelAttribute
    result = ToolResult.model_validate(tool_result_data)

    sse_events = []

    # 根据 ToolResult 三态分流，类比 Java 的 if-else 状态机
    if result.error_for_llm:
        # 状态一：错误 —— 工具执行出错，推错误事件给前端
        sse_events.append({
            "type": "result",
            "data": {"error": True, "message": result.error_for_llm, "summary": result.summary},
        })

    elif result.reply:
        # 状态二：闲聊回复 —— chat 工具的直接文本回复
        sse_events.append({
            "type": "result",
            "data": {"intent": "general", "reply": result.reply, "summary": result.summary},
        })

    elif result.artifact:
        # 状态三：制品结果 —— 生成了表单配置或数据
        artifact_type = getattr(result, 'artifact_type', 'config')  # config 或 data
        config = result.artifact  # 制品内容（表单 JSON 或查询数据）
        formatted = result.extra.get("formatted", {})  # 格式化后的展示数据
        # 校验无错误才算 valid：影响前端是否显示"保存"按钮
        is_valid = len(result.extra.get("validation_errors", [])) == 0

        if artifact_type == "data":
            # 数据型制品（如查询结果表格）
            payload = {
                "artifactType": "data",
                "data": config,
                "summary": result.summary,
            }
            payload.update(formatted)  # 合并格式化字段
            sse_events.append({"type": "result", "data": payload})
        else:
            # 配置型制品（表单配置）—— 需要带校验错误给前端
            # 归一化 validationErrors:统一为 [{message: str}] 格式
            raw_errors = result.extra.get("validation_errors", [])
            # 列表推导：字符串包装成对象，统一结构便于前端渲染
            normalized_errors = [
                {"message": e} if isinstance(e, str) else e
                for e in raw_errors
            ]
            payload = {
                "config": config,  # 表单配置 JSON
                "valid": is_valid,  # 是否通过校验
                "validationErrors": normalized_errors,  # 校验错误列表
                "summary": result.summary,
            }
            payload.update(formatted)
            sse_events.append({"type": "result", "data": payload})

    else:
        # 状态四：都没有 —— 未知结果，兜底报错
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
        # 有工具名：路由到 execute_tool 节点执行
        return "tool"
    # 无工具名：直接结束（极端兜底情况）
    return "end"


def route_after_result(state: GraphState) -> str:
    """handle_result 之后:如果 tool_result 为空说明需要重跑(追问恢复)。"""
    tool_result = state.get("tool_result")
    if tool_result is None and state.get("tool_name"):
        # tool_result 为 None + 有 tool_name：说明是追问恢复，需要重跑工具
        # 类比 Java：状态机检测到 retry 信号，回到执行节点
        return "rerun"
    return "done"


# ── 辅助函数 ──────────────────────────────────────────────


def _build_intent_prompt(has_existing_config: bool) -> str:
    """动态构建意图识别 prompt,从 registry.all() 读取工具描述。"""
    tools_desc = []
    # 遍历所有注册工具，拼接成"工具名: 触发条件"的列表
    for tool in _registry.all():
        requires_artifact = getattr(tool, 'requires_existing_artifact', False)
        # 需要已有配置的工具加条件标注，提示 LLM 何时才选它
        condition = " (仅当 has_existing_config=true)" if requires_artifact else ""
        tools_desc.append(f"- {tool.name}: {tool.when}{condition}")

    tools_list = "\n".join(tools_desc)  # 拼成多行字符串

    # 返回组装好的 system prompt，包含工具列表 + 当前状态 + 输出格式要求
    return (
        "你是意图识别器。根据用户消息选择最合适的工具,只返回 JSON。\n\n"
        f"可选工具:\n{tools_list}\n\n"
        f"当前 has_existing_config={has_existing_config}\n"
        '输出格式: {"tools": ["tool_name"], "reason": "简短理由"}'
    )


def _get_fallback_tool_name() -> str:
    """获取兜底工具名(优先级:chat → 安全只读工具 → 第一个非 artifact 工具)。"""
    # 兜底优先级 1：优先用 chat 闲聊工具（最安全，不会改数据）
    chat_tool = _registry.get("chat")
    if chat_tool:
        return "chat"
    # 兜底优先级 2：找安全的只读工具（is_read_only=True 且非破坏性）
    # 类比 Java：stream().filter(t -> t.isReadOnly() && !t.isDestructive()).findFirst()
    for tool in _registry.all():
        if getattr(tool, 'is_read_only', False) and not getattr(tool, 'is_destructive', True):
            return tool.name
    # 兜底优先级 3：找不需要已有配置的工具
    for tool in _registry.all():
        if not getattr(tool, 'requires_existing_artifact', False):
            return tool.name
    return "chat"  # 兜底 —— 实在没找到，硬编码返回 chat
