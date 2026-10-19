"""D20 会话软删。

软删:delete 打标记(非物理删)、用户列表/嵌入列表过滤、恢复回列表、
events 全程保留。(D20 建议问法功能已整体下线——用户裁决:没意义还占位置)
"""
import pytest

from services.conversation_store import ConversationStore

from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401


@pytest.fixture()
def conv_store(tmp_path):
    return ConversationStore(str(tmp_path / "soft_del.db"))


class TestSoftDelete:

    def test_删除打标记非物理删(self, conv_store):
        c = conv_store.create_conversation("u1")
        cid = c["id"]
        assert conv_store.delete_conversation(cid, "u1") is True
        # 物理行还在(软删)
        with conv_store._get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM session_meta WHERE conv_id = ?",
                (cid,)).fetchone()["c"]
        assert n == 1

    def test_用户列表过滤(self, conv_store):
        c1 = conv_store.create_conversation("u1")
        conv_store.create_conversation("u1")
        conv_store.delete_conversation(c1["id"], "u1")
        items = conv_store.list_conversations("u1")
        assert len(items) == 1 and items[0]["id"] != c1["id"]

    def test_重复删返回False(self, conv_store):
        c = conv_store.create_conversation("u1")
        assert conv_store.delete_conversation(c["id"], "u1") is True
        assert conv_store.delete_conversation(c["id"], "u1") is False

    def test_非本人删不动(self, conv_store):
        c = conv_store.create_conversation("u1")
        assert conv_store.delete_conversation(c["id"], "u2") is False

    def test_恢复回列表(self, conv_store):
        c = conv_store.create_conversation("u1")
        conv_store.delete_conversation(c["id"], "u1")
        assert conv_store.restore_conversation(c["id"]) is True
        assert len(conv_store.list_conversations("u1")) == 1
        # 未删态恢复=False(幂等诚实)
        assert conv_store.restore_conversation(c["id"]) is False

    def test_已删列表(self, conv_store):
        c = conv_store.create_conversation("u1")
        conv_store.delete_conversation(c["id"], "u1")
        deleted = conv_store.list_deleted_conversations()
        assert len(deleted) == 1 and deleted[0]["id"] == c["id"]
        assert deleted[0]["deletedAt"]
