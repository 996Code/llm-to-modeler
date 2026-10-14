"""P0#2 检索测试页 —— hybrid_retrieve diagnostics 增量 + 管理端 retrieve-test 端点。"""
from types import SimpleNamespace

from domains.knowledge_graph import retrieval, runtime

# 复用 m4 的检索 fake 环境(MockLLM/RetrievalFakeGraph/RetrievalFakeVector/obs_calls)
from tests.domains.test_knowledge_graph_m4 import KB1, env  # noqa: F401


class TestHybridRetrieveDiagnostics:

    def test_diagnostics_basic(self, env):
        """返回体带 diagnostics:逐词命中明细+水位+降级标志(此前只进 call_logs)。"""
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲在哪个部门?")
        d = out["diagnostics"]
        assert d["termDetail"] == [{"term": "甲", "matched": "甲", "type": "person"}]
        assert d["nodesTruncated"] is False and d["edgesTruncated"] is False
        assert d["maxNodes"] == 80 and d["maxEdges"] == 150
        assert d["vectorDegraded"] is False

    def test_diagnostics_unmatched_term(self, env):
        """意图词在图谱零命中 → matched=None——"为什么没召回X"的直接答案。"""
        env.llm.query_result = {"entities": ["查无此人"], "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "查无此人在哪?")
        assert out["diagnostics"]["termDetail"] == [
            {"term": "查无此人", "matched": None, "type": None}]
        assert out["seeds"] == []

    def test_diagnostics_marks_truncation(self, env, monkeypatch):
        """贴上限=截断——水位必须如实上报(召回不完整的第一现场)。"""
        monkeypatch.setattr(retrieval, "_cfg",
                            lambda s, k, d: 1 if k == "graph_max_nodes" else d)
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲在哪个部门?")
        assert out["diagnostics"]["nodesTruncated"] is True
        assert out["diagnostics"]["maxNodes"] == 1

    def test_diagnostics_vector_degraded(self, env, monkeypatch):
        """向量路失败降级 → vectorDegraded=True 且 chunks 空(降级可见)。"""
        def boom(self_, qvec, top_k=5, **kw):
            raise RuntimeError("milvus down")
        monkeypatch.setattr(type(env.vector), "search", boom)
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲在哪个部门?")
        assert out["chunks"] == []
        assert out["diagnostics"]["vectorDegraded"] is True
