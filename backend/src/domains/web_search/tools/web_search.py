"""Conversation tool for the shared web_search pack."""
from typing import Any, Optional

from sdk.tool import Tool, ToolContext, ToolResult
from domains.web_search import service, websearch


class WebSearchTool(Tool):
    name = "web_search"
    description = "联网搜索外部信息并根据问题综合回答，附带来源链接"
    when = "用户明确要求上网、联网、网上查询外部信息时使用，内部知识问题不使用"
    state_scope = "web_search"

    def __init__(self, app_state: Any = None):
        self._app_state = app_state

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {
            "query": {"type": "string", "description": "用户原始问题"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
        }, "required": ["query"]}

    def validate_input(self, state: dict) -> Optional[str]:
        if not str(state.get("query") or state.get("user_input") or "").strip():
            return "缺少搜索问题"
        return None

    def preflight(self, state: dict, ctx: ToolContext) -> Optional[ToolResult]:
        if not service.enabled(state):
            return ToolResult(error_for_llm="联网搜索未开启，请点亮输入框的「联网」开关后重试")
        if not websearch.settings_url(self._app_state):
            return ToolResult(error_for_llm="联网搜索未配置，请联系管理员配置 SearXNG 地址")
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        return service.answer(self._app_state, state, ctx)

    def format_result(self, artifact: dict) -> dict:
        return {"query": artifact.get("query") or "",
                "resultCount": len(artifact.get("results") or []),
                "cached": "是" if artifact.get("cached") else "否"}

    def summarize_artifact(self, artifact: dict) -> str:
        return f"联网搜索「{artifact.get('query', '')}」命中 {len(artifact.get('results') or [])} 条结果"
