"""D20 会话软删 + KG 建议问题。

软删:delete 打标记(非物理删)、用户列表/嵌入列表过滤、恢复回列表、
events 全程保留。建议问题:高 hit 块生成、冷库兜底、ACL 过滤。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router

from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401


@pytest.fixture()
def conv_store(tmp_path):
    from services.conversation_store import ConversationStore
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


@pytest.fixture()
def kg_client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    return TestClient(app), env


class TestSuggestQuestions:

    def test_高命中块生成(self, kg_client):
        client, env = kg_client
        kb = env.store.create_kb("建议库", "")
        doc = env.store.create_document(kb["id"], "t.txt", "text/plain", 5,
                                        "/dev/null", "h-sug")
        env.store.replace_chunks(doc["id"], kb["id"],
                                 [{"seq": 0, "text": "劳动合同解除的法定条件与程序"}])
        cid = env.store.list_chunks(doc["id"])[0]["id"]
        env.store.bump_chunk_hits([cid])

        r = client.get("/api/packs/knowledge_graph/suggest-questions")
        assert r.status_code == 200
        qs = r.json()["questions"]
        assert any("劳动合同解除" in q for q in qs)

    def test_冷库兜底不空(self, kg_client):
        client, env = kg_client
        env.store.create_kb("冷库", "")
        r = client.get("/api/packs/knowledge_graph/suggest-questions")
        assert r.status_code == 200
        assert len(r.json()["questions"]) >= 3  # 通用引导兜底
