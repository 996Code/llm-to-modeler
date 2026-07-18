"""ToolRegistry 测试。"""
import pytest
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext


class FakeTool(Tool):
    def __init__(self, name, when="测试"):
        self.name = name
        self.description = f"{name} 工具"
        self.when = when

    def input_schema(self): return {"type": "object"}
    def execute(self, state, ctx): return ToolResult()


class TestToolRegistry:
    def test_register_and_get(self):
        r = ToolRegistry()
        t = FakeTool("create_form")
        r.register(t)
        assert r.get("create_form") is t

    def test_all_returns_registered(self):
        r = ToolRegistry()
        r.register(FakeTool("a"))
        r.register(FakeTool("b"))
        names = sorted(t.name for t in r.all())
        assert names == ["a", "b"]

    def test_get_missing_returns_none(self):
        r = ToolRegistry()
        assert r.get("nope") is None

    def test_describe_for_llm_lists_tools(self):
        r = ToolRegistry()
        r.register(FakeTool("create_form", when="用户想新建表单时"))
        r.register(FakeTool("chat", when="闲聊时"))
        desc = r.describe_for_llm(state={})
        assert "create_form" in desc
        assert "chat" in desc
        assert "用户想新建表单时" in desc
