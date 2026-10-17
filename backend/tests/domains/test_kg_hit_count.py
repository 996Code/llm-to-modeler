"""D29 块命中统计——被引用块 hit_count+1("哪些知识真被用")。

覆盖:store.bump_chunk_hits 计数正确/空参零副作用/不存在 id 安全;
answer_question 后被引用块([片段:N])计数+1、未引用块不动。
"""
import pytest

from domains.knowledge_graph import retrieval
from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401


class TestBumpChunkHits:

    def test_计数递增(self, env):
        store = env.store
        kb = store.create_kb("命中测试库", "")
        doc = store.create_document(kb["id"], "t.txt", "text/plain", 5,
                                    "/dev/null", "h-hit")
        store.replace_chunks(doc["id"], kb["id"],
                             [{"seq": 0, "text": "第一块"}, {"seq": 1, "text": "第二块"}])
        ids = [c["id"] for c in store.list_chunks(doc["id"])]

        assert store.bump_chunk_hits([ids[0]]) == 1
        assert store.bump_chunk_hits([ids[0]]) == 1
        assert store.bump_chunk_hits([ids[0], ids[1]]) == 2

        rows = store.list_chunks(doc["id"])
        by_seq = {r["seq"]: r for r in rows}
        assert by_seq[0]["hitCount"] == 3
        assert by_seq[1]["hitCount"] == 1

    def test_空列表零调用(self, env):
        assert env.store.bump_chunk_hits([]) == 0

    def test_不存在id安全(self, env):
        assert env.store.bump_chunk_hits(["no-such-id"]) == 0


class TestAnswerBumpsHits:

    def test_被引用块计数(self, env, monkeypatch):
        """answer_question 返回带 [片段:1] → chunks[0] 的 hit_count+1。"""
        from domains.knowledge_graph import runtime

        store = env.store
        kb = store.create_kb("回答命中库", "")
        doc = store.create_document(kb["id"], "t.txt", "text/plain", 5,
                                    "/dev/null", "h-ans")
        store.replace_chunks(doc["id"], kb["id"],
                             [{"seq": 0, "text": "第一块内容"},
                              {"seq": 1, "text": "第二块内容"}])
        ids = [c["id"] for c in store.list_chunks(doc["id"])]

        # 固定检索结果:两块召回
        fake_retrieved = {
            "intent": {}, "seeds": [], "subgraph": {"nodes": [], "edges": []},
            "chunks": [
                {"chunkId": ids[0], "docId": doc["id"], "docName": "t.txt",
                 "seq": 0, "score": 0.9, "text": "第一块内容"},
                {"chunkId": ids[1], "docId": doc["id"], "docName": "t.txt",
                 "seq": 1, "score": 0.5, "text": "第二块内容"},
            ],
            "diagnostics": {},
        }
        # LLM 回答只引用片段 1(引用编号权威源=chunks 顺序)
        monkeypatch.setattr(env.llm, "chat",
                            lambda *a, **k: "答案:是这样。[片段:1]")
        monkeypatch.setattr(runtime, "get_kg_store", lambda state: store)

        retrieval.answer_question(env.app_state, kb, "问题", retrieved=fake_retrieved)

        by_seq = {r["seq"]: r for r in store.list_chunks(doc["id"])}
        assert by_seq[0]["hitCount"] == 1   # 被引用 +1
        assert by_seq[1]["hitCount"] == 0   # 未引用不动


class TestChunksEndpointEcho:
    """审查修复锚:chunks 端点 hitCount 回显(store 返回 camelCase 键)。"""

    def test_端点回显hitCount(self, env, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from domains.knowledge_graph import runtime
        from domains.knowledge_graph.api import router as kg_router

        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        app = FastAPI()
        app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
        app.state.llm_client = env.llm
        app.state.settings_store = None
        monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
        client = TestClient(app)

        kb = env.store.create_kb("端点回显库", "")
        doc = env.store.create_document(kb["id"], "t.txt", "text/plain", 5,
                                        "/dev/null", "h-echo")
        env.store.replace_chunks(doc["id"], kb["id"],
                                 [{"seq": 0, "text": "唯一块"}])
        cid = env.store.list_chunks(doc["id"])[0]["id"]
        env.store.bump_chunk_hits([cid])

        r = client.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}"
                       f"/documents/{doc['id']}/chunks")
        assert r.status_code == 200
        items = r.json()["items"]
        assert items[0]["hitCount"] == 1  # 此前读 snake 键恒 0


class TestSummaryNotInChunkList:
    """审查修复锚:摘要块(seq=-1)不进抽取块口径。"""

    def test_list_chunks默认排除摘要(self, env):
        kb = env.store.create_kb("口径库", "")
        doc = env.store.create_document(kb["id"], "t.txt", "text/plain", 5,
                                        "/dev/null", "h-cal")
        env.store.replace_chunks(doc["id"], kb["id"],
                                 [{"seq": 0, "text": "正文块"}])
        env.store.upsert_summary_chunk(doc["id"], kb["id"], "摘要块")
        # 默认:只有正文块(total 不被摘要污染)
        assert len(env.store.list_chunks(doc["id"])) == 1
        # 显式包含:两块
        assert len(env.store.list_chunks(doc["id"], include_summary=True)) == 2
