"""ChatTool 测试。"""
import pytest
from unittest.mock import MagicMock, patch

from domains.njmind_form.tools.chat import ChatTool
from sdk.tool import ToolContext


def _make_ctx(llm_client=None, prompt_loader=None):
    """构造测试用 ToolContext。"""
    ctx = ToolContext(
        llm_client=llm_client or MagicMock(),
        asset_client=None,
        conversation=None,
        emit=lambda *a, **k: None,
    )
    # 额外挂 prompt_loader(Dispatcher 注入)
    object.__setattr__(ctx, "prompt_loader", prompt_loader)
    return ctx


class TestChatToolDeclaration:
    """安全声明:只读 + 可并发。"""

    def test_is_read_only(self):
        assert ChatTool().is_read_only is True

    def test_is_concurrency_safe(self):
        assert ChatTool().is_concurrency_safe is True

    def test_is_not_destructive(self):
        assert ChatTool().is_destructive is False

    def test_input_schema(self):
        schema = ChatTool().input_schema()
        assert schema["type"] == "object"
        assert "user_input" in schema["properties"]


class TestChatToolExecute:
    """执行:渲染 prompt -> 调 LLM -> 返回 reply。"""

    def test_chat_returns_reply(self):
        """正常闲聊:LLM 返回文本,ToolResult.reply 带文本。"""
        llm = MagicMock()
        llm.chat.return_value = "你好!我是表单助手,可以帮你创建或修改表单。"

        tool = ChatTool()
        ctx = _make_ctx(llm_client=llm)
        result = tool.execute({"user_input": "你好"}, ctx)

        assert result.reply == "你好!我是表单助手,可以帮你创建或修改表单。"
        assert result.summary  # 非空摘要进历史
        assert result.artifact is None  # 闲聊不产出制品

    def test_chat_includes_history(self):
        """有 compressed_history 时注入到 user message。"""
        llm = MagicMock()
        llm.chat.return_value = "回复"

        tool = ChatTool()
        ctx = _make_ctx(llm_client=llm)
        tool.execute({
            "user_input": "继续",
            "compressed_history": "之前讨论了请假表",
        }, ctx)

        # 验证 LLM 被调用,且 user message 含历史
        call_args = llm.chat.call_args
        messages = call_args[0][0]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "之前讨论了请假表" in user_msg["content"]
        assert "继续" in user_msg["content"]

    def test_chat_summary_truncated(self):
        """summary 超过 200 字符时截断(避免对话历史膨胀)。"""
        long_reply = "x" * 300
        llm = MagicMock()
        llm.chat.return_value = long_reply

        tool = ChatTool()
        ctx = _make_ctx(llm_client=llm)
        result = tool.execute({"user_input": "test"}, ctx)

        assert len(result.summary) <= 200

    def test_chat_uses_prompt_loader_when_available(self):
        """有 prompt_loader 时用模板渲染(不用内联兜底)。"""
        llm = MagicMock()
        llm.chat.return_value = "回复"

        loader = MagicMock()
        loader.render.return_value = "模板渲染的 system prompt"

        tool = ChatTool()
        ctx = _make_ctx(llm_client=llm, prompt_loader=loader)
        tool.execute({"user_input": "你好"}, ctx)

        loader.render.assert_called_once_with("njmind_form", "chat")
        call_args = llm.chat.call_args
        messages = call_args[0][0]
        system_msg = next(m for m in messages if m["role"] == "system")
        assert system_msg["content"] == "模板渲染的 system prompt"

    def test_chat_falls_back_to_inline_prompt(self):
        """无 prompt_loader 时用内联兜底 prompt(通用,无领域词)。"""
        llm = MagicMock()
        llm.chat.return_value = "回复"

        tool = ChatTool()
        ctx = _make_ctx(llm_client=llm, prompt_loader=None)
        tool.execute({"user_input": "你好"}, ctx)

        call_args = llm.chat.call_args
        messages = call_args[0][0]
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "友好的助手" in system_msg["content"]
