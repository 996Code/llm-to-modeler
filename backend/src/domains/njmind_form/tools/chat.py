"""ChatTool - 闲聊工具。

处理与表单无关的闲聊/打招呼/解释性问题。
声明 is_concurrency_safe=True、is_read_only=True(只读、可并发)。
"""
from typing import Any, Dict

from sdk.tool import Tool, ToolResult, ToolContext


class ChatTool(Tool):
    """闲聊工具。对标 CC 的简单 Tool(非 CompositeTool)。"""

    name = "chat"
    description = "闲聊、打招呼、与表单无关的问题"
    when = "用户打招呼、闲聊、问你是谁、或消息与表单创建/修改无关时"

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
        """渲染 chat.j2 -> 调 LLM -> 返回文本回复。"""
        user_input = state.get("user_input", "")
        compressed_history = state.get("compressed_history", "")

        # 构建 messages
        system_prompt = self._render_system(ctx)
        user_message = self._build_user_message(user_input, compressed_history)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        reply = ctx.llm_client.chat(messages, temperature=0.7)

        return ToolResult(
            reply=reply,
            summary=reply[:200] if reply else "闲聊回复",
        )

    def _render_system(self, ctx: ToolContext) -> str:
        """渲染 chat.j2 system prompt。"""
        # ctx.prompt_loader 由 Dispatcher 注入(阶段 3 Task 5)
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render("njmind_form", "chat")
        # 兜底:内联 prompt
        return (
            "你是低代码平台的表单配置助手。\n"
            "当用户的消息与表单创建/修改无关时（如打招呼、闲聊、问你是谁），"
            "用友好自然的中文回复。保持简洁，不要长篇大论。"
        )

    def _build_user_message(self, user_input: str, compressed_history: str) -> str:
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 用户消息", user_input])
        return "\n".join(parts)
