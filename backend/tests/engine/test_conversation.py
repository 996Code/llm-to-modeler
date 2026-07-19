"""ConversationManager 测试。"""
import pytest
from unittest.mock import MagicMock

from engine.conversation import ConversationManager
from sdk.tool import ToolResult


class TestAppend:
    def test_append_event(self):
        store = MagicMock()
        store.append_event.return_value = "event-1"
        cm = ConversationManager(store=store)

        event_id = cm.append("conv1", "user", {"content": "你好"})
        assert event_id == "event-1"
        store.append_event.assert_called_once_with("conv1", "user", {"content": "你好"})

    def test_append_without_store(self):
        """无 store 时优雅降级(不崩)。"""
        cm = ConversationManager(store=None)
        event_id = cm.append("conv1", "user", {"content": "你好"})
        assert event_id == ""


class TestLoad:
    def test_load_rebuilds_messages(self):
        """load 按 kind 分流重建 messages。"""
        store = MagicMock()
        store.load_events.return_value = [
            {"kind": "user", "payload": {"role": "user", "content": "你好"}},
            {"kind": "assistant", "payload": {"role": "assistant", "content": "回复"}},
            {"kind": "tool_result", "payload": {"role": "assistant", "content": "生成完成"}},
        ]
        cm = ConversationManager(store=store)

        state = cm.load("conv1")
        assert len(state["messages"]) == 3
        assert state["messages"][0]["content"] == "你好"
        assert state["messages"][2]["content"] == "生成完成"

    def test_load_detects_pending_ask(self):
        """load 检测 pending_ask(最新一条 kind=ask)。"""
        store = MagicMock()
        store.load_events.return_value = [
            {"kind": "user", "payload": {"content": "创建表单"}},
            {"kind": "ask", "payload": {"tool": "create_form", "round": 1}},
        ]
        cm = ConversationManager(store=store)

        state = cm.load("conv1")
        assert state["pending_ask"] is not None
        assert state["pending_ask"]["tool"] == "create_form"

    def test_load_collects_checkpoints(self):
        """checkpoint 收集到列表。"""
        store = MagicMock()
        store.load_events.return_value = [
            {"kind": "checkpoint", "payload": {"action": "created"}},
            {"kind": "checkpoint", "payload": {"action": "artifact_saved"}},
        ]
        cm = ConversationManager(store=store)

        state = cm.load("conv1")
        assert len(state["checkpoints"]) == 2

    def test_load_empty_conversation(self):
        store = MagicMock()
        store.load_events.return_value = []
        cm = ConversationManager(store=store)

        state = cm.load("conv1")
        assert state["messages"] == []
        assert state["pending_ask"] is None


class TestSave:
    def test_save_user_and_assistant(self):
        """save:追加 user + assistant(summary)。"""
        store = MagicMock()
        cm = ConversationManager(store=store)

        result = ToolResult(reply="你好!", summary="你好!")
        cm.save("conv1", "你好", result)

        assert store.append_event.call_count == 2
        # 第一次:user
        assert store.append_event.call_args_list[0][0] == ("conv1", "user", {"role": "user", "content": "你好"})
        # 第二次:assistant(reply)
        assert store.append_event.call_args_list[1][0] == ("conv1", "assistant", {"role": "assistant", "content": "你好!"})

    def test_save_artifact_to_checkpoint(self):
        """save:artifact 写 checkpoint,不进 messages。"""
        store = MagicMock()
        cm = ConversationManager(store=store)

        result = ToolResult(
            artifact={"formCode": "test"},
            summary="已生成表单",
        )
        cm.save("conv1", "创建表单", result)

        # 3 次调用:user + assistant(summary) + checkpoint(artifact)
        assert store.append_event.call_count == 3
        # checkpoint 调用
        checkpoint_call = store.append_event.call_args_list[2]
        assert checkpoint_call[0][1] == "checkpoint"
        assert checkpoint_call[0][2]["artifact"]["formCode"] == "test"

    def test_save_extra_not_in_history(self):
        """save:extra 不入历史(避免膨胀)。"""
        store = MagicMock()
        cm = ConversationManager(store=store)

        result = ToolResult(
            summary="完成",
            extra={"validation_errors": ["err1"], "formatted": {"fieldCount": 5}},
        )
        cm.save("conv1", "创建", result)

        # assistant 事件只含 summary,不含 extra
        assistant_call = store.append_event.call_args_list[1]
        payload = assistant_call[0][2]
        assert payload["content"] == "完成"
        assert "extra" not in payload
        assert "validation_errors" not in payload


class TestPendingAsk:
    def test_save_pending_ask(self):
        store = MagicMock()
        cm = ConversationManager(store=store)

        cm.save_pending_ask("conv1", "create_form", {"questions": []}, 1)
        store.append_event.assert_called_once_with("conv1", "ask", {
            "tool": "create_form",
            "ask": {"questions": []},
            "round": 1,
        })

    def test_load_pending_ask(self):
        store = MagicMock()
        store.load_pending_ask.return_value = {
            "payload": {"tool": "create_form", "round": 1},
        }
        cm = ConversationManager(store=store)

        result = cm.load_pending_ask("conv1")
        assert result["payload"]["tool"] == "create_form"

    def test_clear_pending_ask(self):
        store = MagicMock()
        cm = ConversationManager(store=store)

        cm.clear_pending_ask("conv1")
        store.clear_pending_ask.assert_called_once_with("conv1")


class TestListMeta:
    def test_list_meta_does_not_join_events(self):
        store = MagicMock()
        store.list_conversations.return_value = [{"id": "conv1", "title": "对话1"}]
        cm = ConversationManager(store=store)

        result = cm.list_meta("user1")
        assert len(result) == 1
        store.list_conversations.assert_called_once_with("user1")
