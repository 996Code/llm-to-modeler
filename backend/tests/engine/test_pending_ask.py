"""pending_ask 跨请求恢复测试(C.2-A)。"""
import pytest
from unittest.mock import MagicMock

from engine.dispatcher import ToolDispatcher
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext, ClarificationRaised


class AskTool(Tool):
    """测试用:首次执行抛 ClarificationRaised,带 answers 后正常返回。"""
    name = "ask_tool"
    description = "d"
    when = "w"

    def input_schema(self): return {"type": "object"}

    def execute(self, state, ctx):
        if state.get("clarify_answers"):
            return ToolResult(reply="已收到回答", summary="追问后完成")
        raise ClarificationRaised(questions=["需要哪些字段?"])


def _make_dispatcher(conversation=None):
    """构造带 conversation manager 的 dispatcher。"""
    registry = ToolRegistry()
    registry.register(AskTool())
    registry.register(_FakeChat())
    llm = MagicMock()
    llm.chat_json.return_value = {"tools": ["ask_tool"]}
    return ToolDispatcher(
        registry, llm,
        conversation_store=conversation,
        asset_client=MagicMock(),
    )


class _FakeChat(Tool):
    name = "chat"
    description = "d"
    when = "w"
    def input_schema(self): return {"type": "object"}
    def execute(self, state, ctx): return ToolResult(reply="ok")


class TestPendingAskRoundTrip:
    """追问完整流程:首次 ask -> 持久化 -> 带 answers 重跑。"""

    def test_first_run_saves_pending_ask(self):
        """首次执行工具抛 ClarificationRaised -> 保存 pending_ask。"""
        conv = MagicMock()
        dispatcher = _make_dispatcher(conversation=conv)

        result = dispatcher.run("创建表单", "conv1")

        # 应产出 ask
        assert result.ask is not None
        # 应保存 pending_ask
        conv.save_pending_ask.assert_called_once()
        call_kwargs = conv.save_pending_ask.call_args
        assert call_kwargs[1]["tool_name"] == "ask_tool"
        assert call_kwargs[1]["round_num"] == 1

    def test_resume_with_answers_reruns_tool(self):
        """带 answers 重发 -> 检测 pending_ask -> 重跑工具 -> 正常返回。"""
        conv = MagicMock()
        conv.load_pending_ask.return_value = {
            "payload": {
                "tool": "ask_tool",
                "ask": {"questions": [{"question": "需要哪些字段?"}]},
                "round": 1,
            }
        }
        dispatcher = _make_dispatcher(conversation=conv)

        result = dispatcher.run(
            "回答", "conv1",
            answers={"需要哪些字段?": ["姓名", "日期"]},
        )

        # 应正常返回(不再 ask)
        assert result.reply == "已收到回答"
        # 应清除旧 pending_ask
        conv.clear_pending_ask.assert_called_once_with("conv1")

    def test_no_pending_ask_no_resume(self):
        """无 pending_ask 时不走 resume,正常选工具。"""
        conv = MagicMock()
        conv.load_pending_ask.return_value = None
        dispatcher = _make_dispatcher(conversation=conv)

        result = dispatcher.run("创建表单", "conv1")

        # 应走正常流程(抛 ClarificationRaised)
        assert result.ask is not None

    def test_resume_round_limit_exceeded(self):
        """追问超过 max_clarify_rounds -> 返回 error。"""
        conv = MagicMock()
        conv.load_pending_ask.return_value = {
            "payload": {"tool": "ask_tool", "round": 3},  # 已 3 轮
        }
        dispatcher = _make_dispatcher(conversation=conv)
        dispatcher._max_clarify_rounds = 3

        result = dispatcher.run(
            "回答", "conv1",
            answers={"q": "a"},
        )

        assert result.error_for_llm is not None
        assert "超限" in result.error_for_llm
        conv.clear_pending_ask.assert_called_once_with("conv1")

    def test_resume_tool_not_found(self):
        """pending_ask 里的工具名不存在 -> 清除 + 返回 error。"""
        conv = MagicMock()
        conv.load_pending_ask.return_value = {
            "payload": {"tool": "nonexistent", "round": 1},
        }
        dispatcher = _make_dispatcher(conversation=conv)

        result = dispatcher.run(
            "回答", "conv1",
            answers={"q": "a"},
        )

        assert result.error_for_llm is not None
        assert "不存在" in result.error_for_llm
        conv.clear_pending_ask.assert_called_once_with("conv1")

    def test_resume_still_ask_increments_round(self):
        """重跑后仍然 ask -> round 递增保存。"""
        class AlwaysAskTool(Tool):
            name = "ask_tool"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                raise ClarificationRaised(questions=["再问一次"])

        registry = ToolRegistry()
        registry.register(AlwaysAskTool())
        registry.register(_FakeChat())
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["ask_tool"]}

        conv = MagicMock()
        conv.load_pending_ask.return_value = {
            "payload": {"tool": "ask_tool", "round": 1},
        }
        dispatcher = ToolDispatcher(
            registry, llm,
            conversation_store=conv,
            asset_client=MagicMock(),
        )

        result = dispatcher.run("回答", "conv1", answers={"q": "a"})

        # 仍然 ask
        assert result.ask is not None
        # 保存了 round=2
        conv.save_pending_ask.assert_called_once()
        assert conv.save_pending_ask.call_args[1]["round_num"] == 2
