"""ToolDispatcher 测试。"""
import pytest
from unittest.mock import MagicMock, patch

from engine.dispatcher import ToolDispatcher
from engine.compression import build_compressed_history
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext, ClarificationRaised


class FakeTool(Tool):
    """测试用工具。"""
    def __init__(self, name, result=None, requires_existing_artifact=False):
        self.name = name
        self.description = f"{name} 工具"
        self.when = "测试"
        self.requires_existing_artifact = requires_existing_artifact
        self._result = result or ToolResult(reply="ok", summary="ok")

    def input_schema(self): return {"type": "object"}
    def execute(self, state, ctx): return self._result


def _make_registry(*tools):
    r = ToolRegistry()
    for t in tools:
        r.register(t)
    return r


class TestSelectTool:
    """工具选择:LLM 返回工具名。"""

    def test_select_create_form(self):
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("modify_form", requires_existing_artifact=True),
            FakeTool("chat"),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["create_form"], "reason": "用户想建表"}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("创建请假表", {})
        assert tool.name == "create_form"

    def test_select_chat_for_greeting(self):
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("chat"),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"], "reason": "打招呼"}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("你好", {})
        assert tool.name == "chat"

    def test_select_modify_without_config_falls_back_to_create(self):
        """安全兜底:无 source_artifact 选 modify -> 降级 create。"""
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("modify_form", requires_existing_artifact=True),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["modify_form"]}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("加字段", {})  # 无 source_artifact
        assert tool.name == "create_form"  # 降级

    def test_select_modify_without_config_falls_back_to_chat(self):
        """无 source_artifact + 无 create_form -> 兜底 chat。"""
        registry = _make_registry(
            FakeTool("modify_form", requires_existing_artifact=True),
            FakeTool("chat"),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["modify_form"]}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("加字段", {})
        assert tool.name == "chat"

    def test_select_modify_with_config_allowed(self):
        """有 source_artifact 时选 modify 正常。"""
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("modify_form", requires_existing_artifact=True),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["modify_form"]}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("加字段", {"source_artifact": {"formCode": "x"}})
        assert tool.name == "modify_form"

    def test_select_llm_failure_falls_back_to_chat(self):
        """LLM 异常 -> 兜底 chat。"""
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("chat"),
        )
        llm = MagicMock()
        llm.chat_json.side_effect = Exception("LLM down")

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("任意", {})
        assert tool.name == "chat"

    def test_select_invalid_tool_name_falls_back_to_chat(self):
        """LLM 返回无效工具名 -> 兜底 chat。"""
        registry = _make_registry(
            FakeTool("create_form"),
            FakeTool("chat"),
        )
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["nonexistent_tool"]}

        dispatcher = ToolDispatcher(registry, llm)
        tool = dispatcher._select_tool("x", {})
        assert tool.name == "chat"


class TestRunExecution:
    """run 主流程:选 -> 校验 -> 执行。"""

    def test_run_returns_tool_result(self):
        registry = _make_registry(FakeTool("chat", ToolResult(reply="hi", summary="hi")))
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"]}

        dispatcher = ToolDispatcher(registry, llm)
        result = dispatcher.run("你好", "conv1")

        assert result.reply == "hi"
        assert result.summary == "hi"

    def test_run_catches_clarification_raised(self):
        """工具抛 ClarificationRaised -> 转 ToolResult.ask。"""
        class ClarifyTool(Tool):
            name = "create_form"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                raise ClarificationRaised(questions=["需要哪些字段?"])

        registry = _make_registry(ClarifyTool(), FakeTool("chat"))
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["create_form"]}

        dispatcher = ToolDispatcher(registry, llm)
        result = dispatcher.run("创建表单", "conv1")

        assert result.ask is not None
        assert len(result.ask.questions) == 1
        assert result.ask.questions[0].question == "需要哪些字段?"

    def test_run_catches_execution_exception(self):
        """工具抛异常 -> 包装成 error_for_llm。"""
        class CrashTool(Tool):
            name = "create_form"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                raise RuntimeError("boom")

        registry = _make_registry(CrashTool(), FakeTool("chat"))
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["create_form"]}

        dispatcher = ToolDispatcher(registry, llm)
        result = dispatcher.run("创建表单", "conv1")

        assert result.error_for_llm is not None
        assert "boom" in result.error_for_llm

    def test_run_validate_input_failure_returns_error(self):
        """validate_input 失败 -> 跳过 execute,返回 error_for_llm。

        场景:modify_form 需要 source_artifact,但无 source_artifact 时
        _select_tool 会降级到 create_form(安全兜底)。
        所以这里直接用一个无降级的 StrictTool 来测 validate_input。"""
        class StrictTool(Tool):
            name = "strict_tool"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def validate_input(self, state):
                if not state.get("source_artifact"):
                    return "需要已有配置"
                return None
            def execute(self, state, ctx):
                return ToolResult(reply="ok")

        registry = _make_registry(StrictTool(), FakeTool("chat"))
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["strict_tool"]}

        dispatcher = ToolDispatcher(registry, llm)
        # 无 source_artifact
        result = dispatcher.run("加字段", "conv1")

        assert result.error_for_llm == "需要已有配置"
        assert "输入校验失败" in result.summary


class TestBuildCompressedHistory:
    """历史格式化 — 已提取到 engine.compression 模块。"""

    def test_empty_history(self):
        assert build_compressed_history([]) == ""
        assert build_compressed_history(None) == ""

    def test_formats_recent_messages(self):
        history = [
            {"role": "user", "content": "创建表单"},
            {"role": "assistant", "content": "好的"},
        ]
        result = build_compressed_history(history)
        assert "用户: 创建表单" in result
        assert "助手: 好的" in result

    def test_truncates_to_recent_6(self):
        """只保留最近 6 条(3 轮)。"""
        history = [
            {"role": "user", "content": f"msg{i}"}
            for i in range(10)
        ]
        result = build_compressed_history(history)
        assert "msg9" in result  # 最近的
        assert "msg0" not in result  # 太旧的被截断
