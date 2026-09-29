"""BPM 领域闲聊兜底工具。

system prompt 渲染 prompts/chat.j2(prompt_loader 可用时);缺 loader
或模板时退化为内置文案。只读可并发。
"""
from typing import Any, Dict

from sdk.tool import Tool, ToolResult, ToolContext


class ChatTool(Tool):
    """BPM pack 闲聊工具(名字与 form 的 chat 区分,工具名全局唯一)。"""

    name = "bpm_chat"
    description = "流程配置领域的闲聊、能力询问与兜底回复"
    when = "用户打招呼、询问流程 AI 能力、或消息与任何工具都不匹配时"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息"}
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        user_input = state.get("user_input", "")
        system_prompt = self._render_system(ctx)
        reply = ctx.llm_client.chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_input}],
            temperature=0.7, conv_id=ctx.conv_id, stage="bpm.chat.reply")
        return ToolResult(reply=reply, summary=(reply or "")[:200])

    def _render_system(self, ctx: ToolContext) -> str:
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            try:
                return ctx.prompt_loader.render("njmind_bpm", "chat")
            except Exception:
                logger.warning("chat.j2 渲染失败,降级内置文案", exc_info=True)
        return ("你是 njmind 低代码平台的流程配置 AI 助手。回答流程设计问题,"
                "不伪造流程配置结果。")
