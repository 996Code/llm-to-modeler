"""ChatTool - 闲聊工具。

处理与任何具体工具都不匹配的闲聊/打招呼/解释性问题。
声明 is_concurrency_safe=True、is_read_only=True(只读、可并发)。

身份描述:ChatTool 是所有插件共享的兜底工具,不应绑定特定领域。
system prompt 从注册的工具列表动态生成能力描述,确保新插件自动被介绍。
"""
from typing import Any, Dict

from sdk.tool import Tool, ToolResult, ToolContext


class ChatTool(Tool):
    """闲聊工具。对标 CC 的简单 Tool(非 CompositeTool)。"""

    name = "chat"
    description = "闲聊、打招呼、与任何工具都不匹配的问题"
    when = "用户打招呼、闲聊、问你是谁、或消息与任何工具都不匹配时"

    # 安全声明:只读 + 可并发
    is_destructive = False
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息"}
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """渲染 system prompt -> 调 LLM -> 返回文本回复。"""
        user_input = state.get("user_input", "")
        compressed_history = state.get("compressed_history", "")

        # 构建 messages
        system_prompt = self._render_system(ctx)
        user_message = self._build_user_message(user_input, compressed_history)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        reply = ctx.llm_client.chat(messages, temperature=0.7, conv_id=ctx.conv_id)

        return ToolResult(
            reply=reply,
            summary=reply[:200] if reply else "闲聊回复",
        )

    def _render_system(self, ctx: ToolContext) -> str:
        """动态生成 system prompt。

        优先级:
        1. prompt_loader 渲染 chat.j2(保留自定义能力)
        2. 动态生成:从 registry 读取所有工具的 when 描述,构建能力列表
        3. 兜底:通用 prompt
        """
        # 尝试用 prompt_loader 渲染自定义 prompt
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            try:
                return ctx.prompt_loader.render("njmind_form", "chat")
            except Exception:
                pass  # 渲染失败则降级到动态生成

        # 动态生成:从 registry 构建能力描述
        capabilities = self._build_capabilities(ctx)
        if capabilities:
            return (
                "你是低代码平台的智能助手。\n\n"
                "当用户的消息与任何具体工具都不匹配时（如打招呼、闲聊、问你是谁），"
                "用友好自然的中文回复。保持简洁，不要长篇大论。\n\n"
                "如果用户问你能做什么，简要说明你的能力：\n"
                f"{capabilities}\n\n"
                "如果是打招呼，回应问候即可。"
            )

        # 兜底:通用 prompt(不绑定任何领域)
        return (
            "你是一个友好的助手。\n"
            "用自然简洁的中文回应用户。"
        )

    def _build_capabilities(self, ctx: ToolContext) -> str:
        """从 registry 动态构建能力描述列表。

        读取所有非 chat 工具的 when 描述,生成 "- xxx" 列表。
        这样新插件注册后,ChatTool 自动能介绍它的能力,无需写死。
        """
        registry = getattr(ctx, 'registry', None)
        if registry is None:
            # registry 不可用时,返回通用兜底描述
            return "- 通过自然语言描述完成各种业务操作"

        lines = []
        for tool in registry.all():
            # 跳过 chat 自身(不需要介绍"我能闲聊")
            if tool.name == "chat":
                continue
            when_desc = getattr(tool, 'when', '') or getattr(tool, 'description', '')
            if when_desc:
                lines.append(f"- {when_desc}")

        return "\n".join(lines) if lines else "- 通过自然语言描述完成各种业务操作"

    def _build_user_message(self, user_input: str, compressed_history: str) -> str:
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 用户消息", user_input])
        return "\n".join(parts)
