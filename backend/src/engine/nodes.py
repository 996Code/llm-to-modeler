"""LangGraph 节点函数 — StateGraph 的各个节点实现。

【本文件在链路中的位置】
一条用户消息从 HTTP 进来后的完整路径（★标注本文件的参与点）：

  POST /api/config/chat (api/config.py)
    │  组装 context_artifact（宿主下发的画布制品）+ 历史透传头
    ▼
  stream_graph (engine/stream.py)
    │  在工作线程内绑 thread-local（透传头/服务地址/实时推送）
    ▼
★classify_intent_node ─── 两级路由：
    │    一级 _route_pack：请求属于哪个领域（单 pack 直通/多 pack LLM 选）
    │    二级 pack router.route()：领域内选哪个工具（领域知识归 pack）
    ▼
  route_by_tool（条件边：tool_name 空 → END）
    ▼
★execute_tool_node ────── 调 tool.execute(state, ctx)
    │    工具可能 raise ClarificationRaised → 转 LangGraph interrupt()
    │    工具经 ctx.emit 实时推 SSE 进度（thread-local → stream → 前端）
    ▼
★handle_result_node ───── ToolResult 三态（reply/artifact/ask）分流为 SSE 事件
    ▼
  route_after_result（条件边：needs_rerun → execute_tool 重试 / done → END）

设计原则:
  - 节点函数签名: (state: GraphState) -> dict(部分更新)
  - 工具内部 state 通过 tool_state 透传,Graph 不读内部结构（开闭原则）
  - 追问通过 LangGraph interrupt() 暂停 + Command(resume) 恢复（checkpointer 存档）
"""
import logging
from typing import Any, Dict, Optional

from langgraph.types import interrupt

from engine.graph_state import GraphState
from engine.state_keys import STATE_CONTEXT_ARTIFACT as CONTEXT_ARTIFACT
from sdk.tool import Tool, ToolResult, ToolContext, AskSpec, AskQuestion, AskOption, ClarificationRaised
from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── 节点间共享的依赖(由 graph.py 在构建时注入) ──
# LangGraph 节点函数签名只能是 (state) -> dict,
# 外部依赖通过闭包或 module-level 变量注入。
import threading as _threading
# 实时推送通道（请求级，threading.local 绑定；None = 走列表兜底）
_realtime_emitter = _threading.local()


def set_realtime_emitter(fn):
    """为本线程绑定实时 SSE 推送函数（stream.py 在工作线程内调用）。

    fn 签名: fn(kind, payload, message) —— kind ∈ {"stage", "pipeline_definition"}
    传 None 解绑（请求结束/兜底路径）。
    """
    _realtime_emitter.fn = fn


_registry: Optional[ToolRegistry] = None
# pack → 二级路由（两级路由架构：引擎选领域、pack 选工具）
_pack_routers: dict = {}
_llm_client: Any = None
_asset_client: Any = None
_conversation: Any = None
_prompt_loader: Any = None
# pack → domain 声明（description/fallback，来自各 pack config.yaml）。
# 由 graph.py 构建时注入（main 启动装配 pack 时已加载过，这里复用同一份，
# 引擎不 import domains——零领域知识铁律，同时避免重复加载 manifest）。
_pack_configs: dict = {}


def configure(
    registry: ToolRegistry,
    llm_client: Any,
    asset_client: Any,
    conversation: Any = None,
    prompt_loader: Any = None,
    pack_routers: dict = None,
    pack_configs: dict = None,
):
    """注入共享依赖(由 graph.py 构建时调用一次)。"""
    global _registry, _llm_client, _asset_client, _conversation, _prompt_loader, _pack_routers, _pack_configs
    _registry = registry
    _pack_routers = pack_routers or {}
    _pack_configs = pack_configs or {}
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
    has_existing_config = state.get(CONTEXT_ARTIFACT) is not None

    # SSE 事件:告知前端正在识别意图
    # 类比 Java：服务端推送状态给前端，让 UI 显示 loading 文案
    sse_events = [{"type": "stage", "stage": "classify_intent", "message": "正在理解您的意图..."}]

    # ── 两级路由 ──────────────────────────────────────────
    # 一级（引擎，领域无关）：请求属于哪个领域（pack）？单 pack 直通（零 LLM 调用）。
    # 二级（pack，领域知识归 pack）：领域内选哪个工具（如"画布有内容+增量话术
    # =修改类"这类判断写在 pack 的 router 里，不再泄漏进引擎）。
    pack_name = _route_pack(user_input, compressed_history)

    # 构建 user message
    tool_name = ""  # 选中的工具名，空表示未选中
    intent_reason = f"pack={pack_name}"  # 理由带 pack 信息，日志可观测两级路由

    router = _pack_routers.get(pack_name)
    if router is not None:
        try:
            # 二级路由由 pack 提供领域规则；引擎只传消息/画布/历史与 LLM 客户端
            name = router.route(
                user_input,
                state.get(CONTEXT_ARTIFACT),  # 画布（含未保存草稿）——pack 判断"修改 vs 创建"的依据
                history=compressed_history,
                llm_client=_llm_client,
            )
            # 名字校验（LLM 可能编造不存在的工具）；"需要画布但画布空"的
            # 防线在 pack 路由（前置铁律）与工具自身 validate_input，引擎不再重复
            if name and _registry.get(name):
                tool_name = name
        except Exception as e:
            # pack 路由失败：记 warning 不崩，走兜底（与旧意图 LLM 失败同策略）
            logger.warning(f"pack[{pack_name}] router failed: {e}")

    # 兜底:没选到工具时用 fallback
    # 类比 Java：try-catch 兜底默认值，保证流程不中断
    if not tool_name:
        tool_name = _get_fallback_tool_name()  # 通常兜底到 chat 闲聊工具
        intent_reason = "fallback"  # 标记为兜底，便于日志区分

    # 路由观测日志：一次 INFO 看全"画布上下文到没到 + 选了什么工具"。
    # 背景：曾出现"画布明明有表单却走了 create_form 生成全新 key"的事故，
    # 排查要跨三端挖会话库——这行日志让定位缩短到一眼（artifact_bytes=0
    # = 宿主上下文没到后端，查嵌入链路；非 0 但选了 create = 话术命中创建规则）。
    # 注意：引擎不解析 artifact 内部结构（字段数是领域语义，归 pack），
    # 只用序列化字节数做通用度量。
    _artifact = state.get(CONTEXT_ARTIFACT)
    _artifact_bytes = len(str(_artifact)) if _artifact else 0
    logger.info(
        f"route decide: tool={tool_name}, artifact_bytes={_artifact_bytes}, "
        f"reason={intent_reason}, input={user_input[:40]!r}"
    )

    return {
        "tool_name": tool_name,  # 选中的工具名（路由依据）
        "intent_reason": intent_reason,  # 选择理由（日志用）
        # tool_state：透传给工具的内部状态，Graph 不读结构
        # 类比 Java：把 request-scoped 数据放进 ThreadLocal 传给下游 service
        "tool_state": {
            "user_input": user_input,
            "compressed_history": compressed_history,
            "source_artifact": state.get(CONTEXT_ARTIFACT),  # 已有配置（修改类工具需要）
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

    # 构建 SSE emit 回调
    # 两条通道（互斥，避免重复推送）：
    #   实时通道：本请求的 StreamManager 直推（sm.stage 线程安全）——工具每 emit
    #             一步前端立刻看到，不用等节点跑完（大表单 modify 100s 期间进度
    #             全靠它，之前憋到节点结束才吐是"卡在上个阶段"的根因）；
    #   列表通道：append 进 sse_events，随节点 chunk 由 _process_chunk 推送——
    #             MCP 等不走 StreamManager 的调用方的兜底。
    # 绑定用 threading.local：graph 在工作线程跑节点，stream.py 在同一线程注入，
    # 并发请求互不串线。
    sse_events = []

    def emit(*args, **kwargs):
        """emit(event_type, stage_name, message, **extra)"""
        rt = getattr(_realtime_emitter, "fn", None)
        if rt is not None:
            # 实时通道：直接推给 StreamManager（内部 call_soon_threadsafe 回事件循环）
            if len(args) >= 3:
                rt("stage", args[1], args[2])
            elif len(args) == 2:
                if args[0] == "pipeline_definition":
                    rt("pipeline_definition", args[1], None)
                else:
                    rt("stage", args[1], "")
            return  # 已实时推送，不再入列表（避免 chunk 阶段重复推）

        # 列表通道（兜底）
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
    except ClarificationRaised as ce:
        # 工具请求追问（旧协议：抛异常）。必须先于 Exception 捕获——
        # 否则会被下面的兜底 except 吞成 error_for_llm，前端看到的是
        # "工具执行失败: ['请问…']" 而不是追问卡片。
        # 转成 v4 的 ToolResult.ask，走下方统一的 interrupt 机制。
        result = ToolResult(
            ask=AskSpec(questions=[
                AskQuestion(
                    question=q,
                    header="补充信息",
                    # 旧协议是开放问题（无选项）；给一个自由输入项，前端渲染文本输入
                    options=[AskOption(label="自行输入", description="")],
                )
                for q in ce.questions
            ]),
            summary="需要补充信息",
        )
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


def _route_pack(user_input: str, history: str = "") -> str:
    """一级路由（领域无关）：请求属于哪个领域（pack）。

    - 单 pack：直通（零 LLM 调用——两级路由在单领域部署下不引入额外延迟）；
    - 多 pack：LLM 从各 pack 的 manifest domain.description 里选；无匹配时
      落到声明 fallback 的 pack（无 fallback 声明则取第一个）。

    domain 声明来自 pack 的 config.yaml，由 configure(pack_configs=...) 注入
    （main 启动装配时加载，与 pack_routers 同源——引擎不 import domains）。
    """
    if len(_pack_routers) <= 1:
        only = next(iter(_pack_routers), "")
        logger.debug(f"route: single pack '{only}' passthrough")
        return only

    # domain 声明视图：pack → {description, fallback}
    pack_domains = {
        name: (cfg.get("domain") or {}) for name, cfg in _pack_configs.items()
    }
    entries = [
        f"- {name}: {(d.get('description') or '(无描述)')}"
        + (" [fallback]" if d.get("fallback") else "")
        for name, d in pack_domains.items() if name in _pack_routers
    ]
    fallback = next((n for n, d in pack_domains.items()
                     if d.get("fallback") and n in _pack_routers),
                    next(iter(_pack_routers)))

    prompt = (
        "你是领域路由器。判断用户消息属于哪个领域，只返回 JSON。\n\n"
        f"候选领域:\n{'\n'.join(entries)}\n\n"
        "规则：不确定或不属于任何领域时，选标注 [fallback] 的领域。\n"
        '输出格式: {"pack": "领域名"}'
    )
    parts = []
    if history:
        parts.extend(["## 对话历史", history, ""])
    parts.extend(["## 用户消息", user_input, "", "请输出 JSON。"])
    try:
        parsed = _llm_client.chat_json([
            {"role": "system", "content": prompt},
            {"role": "user", "content": "\n".join(parts)},
        ])
        name = parsed.get("pack") if isinstance(parsed, dict) else None
        if name in _pack_routers:
            logger.info(f"route: '{user_input[:30]}' -> pack '{name}'")
            return name
    except Exception as e:
        logger.warning(f"pack routing LLM failed: {e}")
    logger.info(f"route: fallback -> pack '{fallback}'")
    return fallback


# 一级路由的 domain 声明缓存（pack_routers 同生命周期）



def _get_fallback_tool_name() -> str:
    """获取兜底工具名(优先级:chat → 第一个工具)。

    chat 不存在时退第一个工具（其 validate_input 会挡不合法调用）。
    """
    # 兜底优先级 1：优先用 chat 闲聊工具（最安全，不会改数据）
    chat_tool = _registry.get("chat")
    if chat_tool:
        return "chat"
    # 兜底优先级 2：任意第一个工具（聊胜于无；工具自身 validate_input 会再挡）
    tools = _registry.all()
    return tools[0].name if tools else "chat"
