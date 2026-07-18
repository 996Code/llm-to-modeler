"""ToolRegistry — 工具注册表。

pack 启动时静态注册工具。describe_for_llm(state) 生成给 LLM 看的工具清单。
"""
from typing import Optional
from sdk.tool import Tool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def describe_for_llm(self, state: dict) -> str:
        """生成给 LLM 看的工具清单。
        当前简单列出 name/description/when。
        阶段 3 增强:按 state 过滤不可用工具(如无 artifact 时禁用 modify)。"""
        lines = ["可用工具:"]
        for tool in self._tools.values():
            lines.append(f"- {tool.name}: {tool.description} (适用: {tool.when})")
        return "\n".join(lines)
