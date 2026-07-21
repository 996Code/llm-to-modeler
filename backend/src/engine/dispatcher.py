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
            emit = lambda *a, **k: None  # noqa

        # 0. 追问恢复:如果有 pending_ask 且本次带了 answers,重跑工具
        if self._conversation and answers and conv_id:
            pending = self._conversation.load_pending_ask(conv_id)
            if pending:
                return self._resume_ask(pending, answers, conv_id, emit)

        # 1. 构建 state
        state = {
            "user_input": user_input,
            "compressed_history": self._build_compressed_history(conversation_history),
            "source_artifact": current_config,  # modify 用
            "conversation_id": conv_id,
            "forward_headers": forward_headers or {},
        }

        # 2. 选工具(单步,LLM 返回工具名)
        emit("stage", "classify_intent", "正在理解您的意图...")
        tool = self._select_tool(user_input, state)
        if tool is None:
            # 兜底:走 fallback 工具
            tool = self._get_fallback_tool()
            if tool is None:
                return ToolResult(
                    error_for_llm="无法选择工具且无兜底工具",
                    summary="工具选择失败",
                )

        # 3. 构建 ToolContext
        ctx = self._build_ctx(state, emit)

        # 4. 执行拦截层:validate_input
        err = tool.validate_input(state)
        if err is not None:
            return ToolResult(
                error_for_llm=err,
                summary=f"输入校验失败: {err}",
            )

        # 5. 执行工具(捕获 ClarificationRaised)
        try:
            result = tool.execute(state, ctx)
        except ClarificationRaised as e:
            # 兼容:旧式异常 -> 转 ToolResult.ask
            result = ToolResult(
                ask=AskSpec(questions=[
                    AskQuestion(question=q, header="追问", options=[])
                    for q in e.questions
                ])
            )
        except Exception as e:
            # 失败回流:异常包装成 error_for_llm
            logger.exception(f"Tool {tool.name} execution failed")
            return ToolResult(
                error_for_llm=str(e),
                summary=f"工具执行失败: {e}",
            )

        # 6. 追问持久化:工具产出 ask -> 存 pending_ask
        if result.ask is not None and self._conversation and conv_id:
            self._conversation.save_pending_ask(
                conv_id=conv_id,
                tool_name=tool.name,
                ask_spec=result.ask.model_dump(),
                round_num=1,
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
        payload = pending.get("payload", pending)  # 兼容两种格式
        tool_name = payload.get("tool", "")
        round_num = payload.get("round", 1) + 1

        # 追问重跑上限
        if round_num > self._max_clarify_rounds:
            logger.warning(f"Clarify round exceeded max ({self._max_clarify_rounds})")
            if self._conversation:
                self._conversation.clear_pending_ask(conv_id)
            return ToolResult(
                error_for_llm="追问轮数超限,请重新描述需求",
                summary="追问超限",
            )

        tool = self._registry.get(tool_name)
        if tool is None:
            logger.warning(f"Resume ask: tool '{tool_name}' not found")
            if self._conversation:
                self._conversation.clear_pending_ask(conv_id)
            return ToolResult(
                error_for_llm=f"工具 {tool_name} 不存在",
                summary="追问恢复失败",
            )

        # 构建 state(含 answers)
        state = {
            "user_input": "",  # 重跑时不重新选工具
            "clarify_answers": answers,
            "conversation_id": conv_id,
        }
        ctx = self._build_ctx(state, emit)

        # 清除旧 pending_ask,执行工具
        if self._conversation:
            self._conversation.clear_pending_ask(conv_id)

        try:
            result = tool.execute(state, ctx)
        except ClarificationRaised as e:
            result = ToolResult(
                ask=AskSpec(questions=[
                    AskQuestion(question=q, header="追问", options=[])
                    for q in e.questions
                ])
            )
        except Exception as e:
            logger.exception(f"Resume ask tool {tool_name} failed")
            return ToolResult(
                error_for_llm=str(e),
                summary=f"追问重跑失败: {e}",
            )

        # 如果仍然 ask,更新 pending_ask(round 递增)
        if result.ask is not None and self._conversation:
            self._conversation.save_pending_ask(
                conv_id=conv_id,
                tool_name=tool_name,
                ask_spec=result.ask.model_dump(),
                round_num=round_num,
            )

        return result

    def _select_tool(self, user_input: str, state: dict) -> Optional[Tool]:
        """调一次 LLM,从 registry 选 1 个工具。

        LLM 返回 {"tools": ["tool_name"], "reason": "..."}。
        取第一个工具。
        """
        has_existing_config = state.get("source_artifact") is not None

        # 动态构建意图识别 prompt
        system_prompt = self._build_intent_prompt(has_existing_config)

        # 构建 user message
        parts = []
        if state.get("compressed_history"):
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
            parsed = self._llm_client.chat_json(messages)
            tool_names = parsed.get("tools", [])
            if not tool_names:
                # LLM 未返回 tools -> 兜底 chat
                logger.warning(f"LLM returned no tools, fallback to chat. Parsed: {parsed}")
                return self._get_fallback_tool()

            # 取第一个可用工具
            for name in tool_names:
                tool = self._registry.get(name)
                if tool:
                    # 安全检查:需要已有配置的工具,如果没有配置则跳过
                    if getattr(tool, 'requires_existing_artifact', False) and not has_existing_config:
                        logger.info(f"Safety: {name} requires existing config but none found, skipping")
                        continue
                    return tool

            # 所有工具都不适用 -> 兜底
            return self._get_fallback_tool()
        except Exception as e:
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
        chat_tool = self._registry.get("chat")
        if chat_tool:
            return chat_tool
        
        # 2. 找安全的只读工具
        for tool in self._registry.all():
            if getattr(tool, 'is_read_only', False) and not getattr(tool, 'is_destructive', True):
                return tool
        
        # 3. 返回第一个不需要已有配置的工具
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
            from src.services.upstream_client import UpstreamClient
            from adapters.http_asset_client import HttpAssetClient
            upstream = UpstreamClient(conversation_store=self._conversation_store)
            self._asset_client = HttpAssetClient(upstream=upstream)

        ctx = ToolContext(
            llm_client=self._llm_client,
            asset_client=self._asset_client,
            conversation=self._conversation_store,
            emit=emit,
            forward_headers=state.get("forward_headers", {}),
            conv_id=state.get("conversation_id"),
        )
        # 额外挂 prompt_loader
        object.__setattr__(ctx, "prompt_loader", self._prompt_loader)
        return ctx

    def _build_compressed_history(self, history: list) -> str:
        """把对话历史格式化为文本。

        TODO(阶段 4): 接压缩器,实现:
        1. token 估算(estimate_tokens)
        2. 70% 阈值触发压缩
        3. LLM 摘要旧历史
        4. 状态补偿(summarize_artifact)
        当前简单截断最近 6 条,每条 200 字符。
        """
        if not history:
            return ""
        parts = []
        for msg in history[-6:]:  # 最近 3 轮(6 条)
            role = "用户" if msg.get("role") == "user" else "助手"
            content = msg.get("content", "")[:200]
            parts.append(f"{role}: {content}")
        return "\n".join(parts)
