"""ToolDispatcher - 工具调度器。

单轮多工具选择 + is_concurrency_safe 分批 + 追问重跑。

阶段 3 实现:
- _select_tool: 调一次 LLM,从 registry 选 1 个工具(单步,简化版)
- _run_single: validate_input 拦截 + execute + 三态分流(ask/result/error)
- 兼容 ClarificationRaised 异常

注:本阶段实现单工具选择(不实现 _select_tools 多工具并发),
保持与旧 graph 行为一致,降低首次可跑风险。
C.2-B 并发留到后续完善。
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from sdk.tool import Tool, ToolResult, ToolContext, AskSpec, AskQuestion, AskOption, ClarificationRaised
from sdk.registry import ToolRegistry
from engine.compression import build_compressed_history

logger = logging.getLogger(__name__)


class ToolDispatcher:
    """工具调度器:选工具 -> 校验 -> 执行 -> 分流。"""

    def __init__(
        self,
        registry: ToolRegistry,
        llm_client: Any,
        conversation_store: Any = None,
        prompt_loader: Any = None,
        asset_client: Any = None,
    ):
        self._registry = registry
        self._llm_client = llm_client
        self._conversation_store = conversation_store  # 兼容旧字段名
        self._conversation = conversation_store  # ConversationManager(新 API)
        self._prompt_loader = prompt_loader
        self._asset_client = asset_client  # 注入或延迟创建
        self._max_clarify_rounds = 3

    def run(
        self,
        user_input: str,
        conv_id: str,
        forward_headers: dict = None,
        current_config: dict = None,
        conversation_history: list = None,
        answers: dict = None,
        emit: Callable = None,
    ) -> ToolResult:
        """主入口:选工具 -> 执行 -> 返回 ToolResult。

        Args:
            user_input: 用户消息
            conv_id: 会话 ID
            forward_headers: 转发到上游的请求头
            current_config: 当前表单配置(如有)
            conversation_history: 对话历史
            answers: 追问恢复时的用户回答(可选)
            emit: SSE emit 回调

        Returns:
            ToolResult
        """
        if emit is None:
            # emit 缺省为空函数：类比 Java 的 no-op callback，避免调用方都要判空
            emit = lambda *a, **k: None  # noqa

        # 0. 追问恢复:如果有 pending_ask 且本次带了 answers,重跑工具
        # 这是旧式追问机制（save_pending_ask），与新 graph 的 interrupt 不同
        if self._conversation and answers and conv_id:
            pending = self._conversation.load_pending_ask(conv_id)  # 取上次存的追问
            if pending:
                # 有待处理的追问 + 用户带了答案：走恢复路径，不重新选工具
                return self._resume_ask(pending, answers, conv_id, emit)

        # 1. 构建 state
        # state 是工具内部状态字典，类比 Java 的 ThreadLocal context
        state = {
            "user_input": user_input,
            "compressed_history": build_compressed_history(conversation_history),  # 历史压缩
            "source_artifact": current_config,  # modify 用：已有配置作为修改起点
            "conversation_id": conv_id,
            "forward_headers": forward_headers or {},  # 防None，类比 Optional.orElse(emptyMap)
        }

        # 2. 选工具(单步,LLM 返回工具名)
        emit("stage", "classify_intent", "正在理解您的意图...")  # 推进度给前端
        tool = self._select_tool(user_input, state)  # 调 LLM 选工具
        if tool is None:
            # 兜底:走 fallback 工具
            # 类比 Java：try-catch 兜底，保证流程不因选工具失败而中断
            tool = self._get_fallback_tool()
            if tool is None:
                # 连兜底都没有：返回错误，让上层处理
                return ToolResult(
                    error_for_llm="无法选择工具且无兜底工具",
                    summary="工具选择失败",
                )

        # 3. 构建 ToolContext
        ctx = self._build_ctx(state, emit)  # 注入所有依赖（LLM/asset/emit 等）

        # 4. 执行拦截层:validate_input
        # 类比 Java 的 Bean Validation @Valid，执行前先校验输入合法性
        err = tool.validate_input(state)
        if err is not None:
            # 校验失败：直接返回错误，不执行工具（快速失败）
            return ToolResult(
                error_for_llm=err,
                summary=f"输入校验失败: {err}",
            )

        # 5. 执行工具(捕获 ClarificationRaised)
        try:
            result = tool.execute(state, ctx)  # 执行工具主逻辑
        except ClarificationRaised as e:
            # 兼容:旧式异常 -> 转 ToolResult.ask
            # 工具内部抛 ClarificationRaised 表示要追问，统一转成 ask 结构
            result = ToolResult(
                ask=AskSpec(questions=[
                    AskQuestion(question=q, header="追问", options=[])
                    for q in e.questions
                ])
            )
        except Exception as e:
            # 失败回流:异常包装成 error_for_llm
            # 其他异常：记录堆栈并包装成错误结果（不让异常向上传播）
            logger.exception(f"Tool {tool.name} execution failed")
            return ToolResult(
                error_for_llm=str(e),
                summary=f"工具执行失败: {e}",
            )

        # 6. 追问持久化:工具产出 ask -> 存 pending_ask
        # 如果工具返回了 ask（要追问），把追问状态存起来，等用户下次带 answers 来恢复
        if result.ask is not None and self._conversation and conv_id:
            self._conversation.save_pending_ask(
                conv_id=conv_id,
                tool_name=tool.name,  # 记录工具名，恢复时重跑这个工具
                ask_spec=result.ask.model_dump(),  # 序列化追问规格
                round_num=1,  # 第 1 轮追问
            )

        return result

    def _resume_ask(
        self,
        pending: dict,
        answers: dict,
        conv_id: str,
        emit: Callable,
    ) -> ToolResult:
        """追问恢复:带着 answers 重跑工具。

        Args:
            pending: load_pending_ask 返回的 dict(payload 含 tool/ask/round)
            answers: 用户的回答
            conv_id: 会话 ID
            emit: SSE emit 回调
        """
        payload = pending.get("payload", pending)  # 兼容两种格式（裸 dict 或包在 payload 里）
        tool_name = payload.get("tool", "")  # 上次要追问的工具名
        round_num = payload.get("round", 1) + 1  # 轮次 +1（本次是第 N 次追问）

        # 追问重跑上限
        # 防止无限追问死循环：超过 3 轮还问不清就放弃
        if round_num > self._max_clarify_rounds:
            logger.warning(f"Clarify round exceeded max ({self._max_clarify_rounds})")
            if self._conversation:
                # 清除 pending_ask：避免下次又走进恢复路径
                self._conversation.clear_pending_ask(conv_id)
            return ToolResult(
                error_for_llm="追问轮数超限,请重新描述需求",
                summary="追问超限",
            )

        tool = self._registry.get(tool_name)  # 从注册表取工具
        if tool is None:
            # 工具不存在（可能插件被卸载）：清除 pending_ask 并报错
            logger.warning(f"Resume ask: tool '{tool_name}' not found")
            if self._conversation:
                self._conversation.clear_pending_ask(conv_id)
            return ToolResult(
                error_for_llm=f"工具 {tool_name} 不存在",
                summary="追问恢复失败",
            )

        # 构建 state(含 answers)
        # 重跑时 user_input 留空：因为不重新选工具，直接用上次的工具跑
        state = {
            "user_input": "",  # 重跑时不重新选工具
            "clarify_answers": answers,  # 用户的回答，工具内部会消费
            "conversation_id": conv_id,
        }
        ctx = self._build_ctx(state, emit)  # 构建上下文

        # 清除旧 pending_ask,执行工具
        # 先清旧 ask 再跑：如果跑完又产生新 ask，下面会重新存
        if self._conversation:
            self._conversation.clear_pending_ask(conv_id)

        try:
            result = tool.execute(state, ctx)  # 带着答案重跑工具
        except ClarificationRaised as e:
            # 工具又抛追问异常（信息还不够）：转成 ask 结构
            result = ToolResult(
                ask=AskSpec(questions=[
                    AskQuestion(question=q, header="追问", options=[])
                    for q in e.questions
                ])
            )
        except Exception as e:
            # 重跑失败：包装错误返回
            logger.exception(f"Resume ask tool {tool_name} failed")
            return ToolResult(
                error_for_llm=str(e),
                summary=f"追问重跑失败: {e}",
            )

        # 如果仍然 ask,更新 pending_ask(round 递增)
        # 工具又要追问：存新的 pending_ask，round_num 递增
        if result.ask is not None and self._conversation:
            self._conversation.save_pending_ask(
                conv_id=conv_id,
                tool_name=tool_name,
                ask_spec=result.ask.model_dump(),
                round_num=round_num,  # 用递增后的轮次
            )

        return result

    def _select_tool(self, user_input: str, state: dict) -> Optional[Tool]:
        """调一次 LLM,从 registry 选 1 个工具。

        LLM 返回 {"tools": ["tool_name"], "reason": "..."}。
        取第一个工具。
        """
        # 判断是否有已有配置：决定可选工具集（如 modify 需要 source_artifact）
        has_existing_config = state.get("source_artifact") is not None

        # 动态构建意图识别 prompt
        # prompt 从 registry 动态生成，新插件自动被识别（无需改代码）
        system_prompt = self._build_intent_prompt(has_existing_config)

        # 构建 user message
        # 用 list + join 拼字符串，比 f-string 多行更可控（类比 Java StringBuilder）
        parts = []
        if state.get("compressed_history"):
            # 有历史才追加历史段（多轮上下文）
            parts.extend(["## 对话历史", state["compressed_history"], ""])
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

        try:
            # chat_json：要求 LLM 输出 JSON（含 tools 列表 + reason）
            parsed = self._llm_client.chat_json(messages)
            tool_names = parsed.get("tools", [])
            if not tool_names:
                # LLM 未返回 tools -> 兜底 chat
                # 这种情况通常 LLM 没理解意图，降级到闲聊
                logger.warning(f"LLM returned no tools, fallback to chat. Parsed: {parsed}")
                return self._get_fallback_tool()

            # 取第一个可用工具
            # 遍历 LLM 推荐的工具名，找第一个满足前置条件的
            # 类比 Java stream().filter(...).findFirst()
            for name in tool_names:
                tool = self._registry.get(name)
                if tool:
                    # 安全检查:需要已有配置的工具,如果没有配置则跳过
                    # 例：modify 工具需要 source_artifact，没有则不能选
                    if getattr(tool, 'requires_existing_artifact', False) and not has_existing_config:
                        logger.info(f"Safety: {name} requires existing config but none found, skipping")
                        continue  # 跳过不满足条件的，继续看下一个
                    return tool  # 命中第一个可用的

            # 所有工具都不适用 -> 兜底
            return self._get_fallback_tool()
        except Exception as e:
            # LLM 调用失败：降级到 fallback 工具，不让选工具失败导致整个请求崩
            logger.warning(f"Tool selection LLM failed: {e}, fallback to chat")
            return self._get_fallback_tool()

    def _build_intent_prompt(self, has_existing_config: bool) -> str:
        """动态构建意图识别 prompt,基于注册的工具列表。
        
        不再依赖 Jinja2 模板(模板会硬编码工具名,不利于插件化)。
        直接从 registry 动态生成工具描述,确保新插件自动被识别。
        """
        # 动态生成工具描述
        tools_desc = []
        for tool in self._registry.all():
            requires_artifact = getattr(tool, 'requires_existing_artifact', False)
            # 需要已有配置的工具加条件标记,提示 LLM 慎选
            condition = " (仅当 has_existing_config=true)" if requires_artifact else ""
            tools_desc.append(f"- {tool.name}: {tool.when}{condition}")

        tools_list = "\n".join(tools_desc)

        return (
            "你是意图识别器。根据用户消息选择最合适的工具,只返回 JSON。\n\n"
            f"可选工具:\n{tools_list}\n\n"
            f"当前 has_existing_config={has_existing_config}\n"
            '输出格式: {"tools": ["tool_name"], "reason": "简短理由"}'
        )

    def _get_fallback_tool(self) -> Optional[Tool]:
        """获取兜底工具。
        
        优先级：
        1. 名为 'chat' 的工具（兼容现有 pack）
        2. is_read_only=True 且 is_destructive=False 的安全工具
        3. 第一个不需要已有配置的工具
        """
        # 1. 尝试找 chat 工具
        # 优先 chat:兼容现有 pack,闲聊兜底最安全
        chat_tool = self._registry.get("chat")
        if chat_tool:
            return chat_tool

        # 2. 找安全的只读工具
        # 无 chat 时找只读且非破坏性的工具(类比 Java 的最小权限原则)
        for tool in self._registry.all():
            if getattr(tool, 'is_read_only', False) and not getattr(tool, 'is_destructive', True):
                return tool

        # 3. 返回第一个不需要已有配置的工具
        # 再不行找任何不需已有配置的工具(至少能跑)
        for tool in self._registry.all():
            if not getattr(tool, 'requires_existing_artifact', False):
                return tool

        # 实在没有就返回 None
        return None

    def _build_ctx(self, state: dict, emit: Callable) -> ToolContext:
        """构建 ToolContext,注入所有依赖。

        asset_client 复用(避免每次 run 都 new UpstreamClient 导致连接泄漏)。
        首次调用时延迟创建,后续复用。
        """
        if self._asset_client is None:
            # 延迟创建一次,后续复用
            # 延迟初始化：类比 Java 的 @Lazy，首次用时才创建，避免构造期循环依赖
            from src.services.upstream_client import UpstreamClient
            from adapters.http_asset_client import HttpAssetClient
            # 创建底层 UpstreamClient（带连接池），再用 HttpAssetClient 包装
            upstream = UpstreamClient(conversation_store=self._conversation_store)
            self._asset_client = HttpAssetClient(upstream=upstream)  # 缓存到实例属性复用

        # 构建 ToolContext：把所有依赖打包给工具
        # 类比 Java 的 @ContextObject，工具通过它访问 LLM/asset/emit 等
        ctx = ToolContext(
            llm_client=self._llm_client,
            asset_client=self._asset_client,
            conversation=self._conversation_store,
            emit=emit,
            forward_headers=state.get("forward_headers", {}),
            conv_id=state.get("conversation_id"),
            registry=self._registry,  # 只读引用,供工具查询其他工具能力
        )
        # 额外挂 prompt_loader
        # object.__setattr__：绕过 Pydantic 的 frozen 限制，类比 Java 反射注入非构造字段
        object.__setattr__(ctx, "prompt_loader", self._prompt_loader)
        return ctx
