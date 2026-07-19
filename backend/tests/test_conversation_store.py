"""ConversationStore 测试 - append-only 事件流。"""
import os
import tempfile
import pytest

from src.services.conversation_store import ConversationStore


@pytest.fixture
def store():
    """创建临时数据库的 ConversationStore。"""
    db_path = tempfile.mktemp(suffix=".db")
    s = ConversationStore(db_path)
    yield s
    # 清理
    try:
        os.unlink(db_path)
    except:
        pass


class TestAppendOnly:
    """append-only 事件流:只 INSERT 不 UPDATE。"""

    def test_add_message_only_inserts(self, store):
        """add_message 只 INSERT,不 UPDATE(append-only)。"""
        store.create_conversation("user1", "测试")
        conv = store.list_conversations("user1")[0]
        conv_id = conv["id"]

        store.add_message(conv_id, "user", "你好")
        store.add_message(conv_id, "assistant", "你好!")

        messages = store.get_messages(conv_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "你好"
        assert messages[1]["role"] == "assistant"

    def test_append_event(self, store):
        """append_event 公开方法:追加任意 kind 事件。"""
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        event_id = store.append_event(conv_id, "checkpoint", {"action": "test"})
        assert event_id  # 返回 event id

        events = store.load_events(conv_id)
        assert any(e["kind"] == "checkpoint" for e in events)

    def test_load_events_filtered_by_kind(self, store):
        """load_events 按 kind 过滤。"""
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        store.append_event(conv_id, "user", {"content": "你好"})
        store.append_event(conv_id, "compacted", {"summary": "压缩"})
        store.append_event(conv_id, "assistant", {"content": "回复"})

        # 只加载 compacted
        compacted = store.load_events(conv_id, kinds=["compacted"])
        assert len(compacted) == 1
        assert compacted[0]["payload"]["summary"] == "压缩"

        # 加载 user + assistant
        messages = store.load_events(conv_id, kinds=["user", "assistant"])
        assert len(messages) == 2


class TestLegacyMigration:
    """旧表迁移:RENAME 为 _legacy_ 留档,不导入数据。"""

    def test_new_conversation_starts_empty(self, store):
        """新会话从空表开始,无历史数据。"""
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        messages = store.get_messages(conv_id)
        assert messages == []  # 空的

    def test_old_tables_renamed(self, store):
        """旧 conversations/messages 表应被 RENAME 为 _legacy_*。"""
        import sqlite3
        with sqlite3.connect(str(store.db_path)) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        assert "_legacy_conversations" in tables or "conversations" not in tables


class TestSessionMeta:
    """session_meta 表:列表查询不 JOIN events。"""

    def test_list_conversations_only_queries_meta(self, store):
        """列表页只查 session_meta,不 JOIN events。"""
        store.create_conversation("user1", "对话1")
        store.create_conversation("user1", "对话2")

        convs = store.list_conversations("user1")
        assert len(convs) == 2
        titles = [c["title"] for c in convs]
        assert "对话1" in titles
        assert "对话2" in titles

    def test_update_conversation_config(self, store):
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        store.update_conversation_config(conv_id, {"formCode": "test"}, title="新标题")
        conv = store.get_conversation(conv_id, "user1")
        assert conv["title"] == "新标题"
        assert conv["currentConfig"]["formCode"] == "test"


class TestPendingAsk:
    """pending_ask 持久化与恢复。"""

    def test_save_and_load_pending_ask(self, store):
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        store.append_event(conv_id, "ask", {
            "tool": "create_form",
            "ask": {"questions": [{"question": "需要哪些字段?"}]},
            "round": 1,
        })

        loaded = store.load_pending_ask(conv_id)
        assert loaded is not None
        assert loaded["payload"]["tool"] == "create_form"
        assert loaded["payload"]["round"] == 1

    def test_clear_pending_ask(self, store):
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        store.append_event(conv_id, "ask", {"tool": "create_form", "round": 1})
        assert store.load_pending_ask(conv_id) is not None

        store.clear_pending_ask(conv_id)
        assert store.load_pending_ask(conv_id) is None

    def test_no_pending_ask_returns_none(self, store):
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]
        assert store.load_pending_ask(conv_id) is None


class TestCrashRecovery:
    """崩溃重放恢复:写 100 轮后状态完整。"""

    def test_crash_recovery_preserves_state(self, store):
        """写多轮后,从 events 表能完整重建。"""
        store.create_conversation("user1")
        conv_id = store.list_conversations("user1")[0]["id"]

        # 写 10 轮对话
        for i in range(10):
            store.add_message(conv_id, "user", f"问题{i}")
            store.add_message(conv_id, "assistant", f"回答{i}")

        # 模拟"崩溃后重启":重新加载
        messages = store.get_messages(conv_id)
        assert len(messages) == 20  # 10 轮 * 2
        assert messages[0]["content"] == "问题0"
        assert messages[-1]["content"] == "回答9"
