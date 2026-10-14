"""P0#4 KG rerank 接线测试 —— 开关/重排/降级/编号权威源。"""
from types import SimpleNamespace

from domains.knowledge_graph import retrieval

from tests.domains.test_knowledge_graph_m4 import KB1, env  # noqa: F401


def _retrieved_two_chunks():
    """两 chunk:顺序 [handbook, alert],rerank 将把 alert(索引1)提前。"""
    return {
        "subgraph": {"nodes": [{"id": "kb1:甲", "name": "甲"}], "edges": []},
        "chunks": [
            {"docId": "d1", "docName": "handbook.md", "seq": 0,
             "text": "甲担任A部门经理。", "score": 0.9},
            {"docId": "d2", "docName": "alert-rules.md", "seq": 1,
             "text": "告警升级路径。", "score": 0.3},
        ],
        "intent": {"entities": ["甲"], "keywords": [], "hop": 1},
    }


class TestKgRerank:

    def test_disabled_by_default_unchanged(self, env):
        """默认关:chunks 顺序/answer 全链与现状一致(评测集护栏的根基)。"""
        env.llm.query_result = {"entities": ["甲"], "keywords": [], "hop": 1}
        out = retrieval.answer_question(env.app_state, KB1, "甲在哪?",
                                        retrieved=_retrieved_two_chunks())
        assert [c["docName"] for c in out["sources"]["chunks"]] == \
            ["handbook.md", "alert-rules.md"]

    def test_rerank_reorders_chunks(self, env, monkeypatch):
        """开关开+rerank 成功:chunks 按相关度重排,sources.chunks 顺序
        =新权威源(引用编号自动对齐,P0#3 遗产约束)。"""
        monkeypatch.setattr(retrieval, "_cfg",
                            lambda s, k, d: True if k == "rerank_enabled" else d)
        calls = []

        def fake_rerank(query, texts, conv_id=None, stage=None):
            calls.append((query, texts, stage))
            return [1, 0]   # alert-rules(索引1)最相关

        env.llm.rerank = fake_rerank
        out = retrieval.answer_question(env.app_state, KB1, "甲在哪?",
                                        retrieved=_retrieved_two_chunks())
        assert [c["docName"] for c in out["sources"]["chunks"]] == \
            ["alert-rules.md", "handbook.md"]
        assert calls and calls[0][2] == "kg.rerank"
        assert calls[0][1][0].startswith("甲担任")  # documents=chunk 原文

    def test_rerank_failure_degrades_original_order(self, env, monkeypatch):
        """rerank 失败(异常)→降级原序,answer 链路完整不阻断(蓝图验收)。"""
        monkeypatch.setattr(retrieval, "_cfg",
                            lambda s, k, d: True if k == "rerank_enabled" else d)

        def boom(query, texts, conv_id=None, stage=None):
            raise RuntimeError("rerank endpoint down")

        env.llm.rerank = boom
        out = retrieval.answer_question(env.app_state, KB1, "甲在哪?",
                                        retrieved=_retrieved_two_chunks())
        assert out["answer"].startswith("模拟回答")
        assert [c["docName"] for c in out["sources"]["chunks"]] == \
            ["handbook.md", "alert-rules.md"]

    def test_rerank_skipped_when_no_chunks(self, env, monkeypatch):
        """chunks 空(纯图谱/向量零命中)不调 rerank——零文档重排无意义。"""
        monkeypatch.setattr(retrieval, "_cfg",
                            lambda s, k, d: True if k == "rerank_enabled" else d)
        called = []
        env.llm.rerank = lambda *a, **kw: called.append(1) or [0]
        empty = _retrieved_two_chunks()
        empty["chunks"] = []
        retrieval.answer_question(env.app_state, KB1, "甲在哪?",
                                  retrieved=empty)
        assert called == []
