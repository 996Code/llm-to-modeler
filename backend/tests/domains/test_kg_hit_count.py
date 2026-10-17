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
