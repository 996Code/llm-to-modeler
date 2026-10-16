"""P0#2 检索测试页 —— hybrid_retrieve diagnostics 增量 + 管理端 retrieve-test 端点。"""
from types import SimpleNamespace

import pytest

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

    def test_diagnostics_vector_degraded_on_embeddings_failure(
            self, env, monkeypatch):
        """embeddings 失败发生在 vector.search 之前 → 走外层降级路径
        (degraded 日志那条,非内层 _search_failed 早退):图谱路照常,
        继续到函数末尾 return,chunks 空 + vectorDegraded=True。"""
        def boom(texts, conv_id=None, stage=None):
            raise RuntimeError("embedding down")
        monkeypatch.setattr(env.llm, "embeddings", boom)
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲在哪个部门?")
        assert out["chunks"] == []
        assert out["diagnostics"]["vectorDegraded"] is True
        # 外层降级不拖垮图谱路:种子/子图照常返回
        assert out["seeds"] and out["seeds"][0]["name"] == "甲"
        assert out["subgraph"]["nodes"]
        # 外层路径落且仅落 1 条 degraded 向量日志(与内层 search 失败的
        # 早退路径区分:那条的日志无 degraded 标记)
        vec_logs = [c for c in env.obs_calls if c["call_type"] == "vector"]
        assert len(vec_logs) == 1
        assert vec_logs[0]["request_data"].get("degraded") is True


# ── 管理端 retrieve-test 端点 ─────────────────────────────────

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def rt_client(env, monkeypatch):
    # 开放模式确定性:本 fixture 下走鉴权真实链(conftest 已注册
    # require_admin),ADMIN_TOKEN 未配置=放行;门控行为留给专属用例设 env
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from domains.knowledge_graph.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    # 观测 sink 一并挂上:检索调用日志(_log_retrieval_call)读
    # app_state.conversation_store,缺了会静默早退(top_k 锚定用例依赖它)
    app.state.conversation_store = env.app_state.conversation_store
    # 端点按 kb_id 查 store;fake 图谱的库固定 "kb1"——store 也走 fake 对齐
    fake_store = SimpleNamespace(
        get_kb=lambda kid: dict(KB1) if kid == "kb1" else None,
        list_kbs=lambda: [dict(KB1)],
        list_documents=lambda kb_id: [],
    )
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: fake_store)
    return TestClient(app), env


class TestRetrieveTestEndpoint:

    def test_endpoint_returns_both_paths(self, rt_client):
        client, env = rt_client
        r = client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                        json={"query": "甲在哪个部门?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kb"]["name"] == "一号库"
        assert body["intent"]["entities"] == ["甲"]
        assert body["diagnostics"]["termDetail"][0]["matched"] == "甲"
        assert body["chunks"] and body["chunks"][0]["score"] == 0.88

    def test_endpoint_retrieve_only_skips_answer_llm(self, rt_client):
        """retrieve-only 硬约束:不触发 kg.answer——省钱省时不烧回答 token。"""
        client, env = rt_client
        client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                    json={"query": "甲在哪?"})
        assert env.llm.chat_calls == []  # chat() 仅 kg.answer 用到;意图走 chat_json

    def test_endpoint_requires_query(self, rt_client):
        client, _ = rt_client
        assert client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                           json={"query": " "}).status_code == 422

    def test_endpoint_404_unknown_kb(self, rt_client):
        client, _ = rt_client
        assert client.post("/api/packs/knowledge_graph/kbs/nope/retrieve-test",
                           json={"query": "x"}).status_code == 404

    def test_endpoint_admin_gated(self, rt_client, monkeypatch):
        """ADMIN_TOKEN 配置时无口令 401——诊断面(seeds/水位)不裸奔到用户级。"""
        client, _ = rt_client
        monkeypatch.setenv("ADMIN_TOKEN", "t")
        r = client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                        json={"query": "x"})
        assert r.status_code == 401
        ok = client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                         json={"query": "甲在哪?"},
                         headers={"X-Admin-Token": "t"})
        assert ok.status_code == 200

    def test_endpoint_passes_top_k(self, rt_client):
        """top_k 端到端透传:前端 top 5/10/20 选择器断裂会静默失效——
        以向量检索日志的 topK 锚定(默认 5,传 1 须命中 1)。"""
        client, env = rt_client
        r = client.post("/api/packs/knowledge_graph/kbs/kb1/retrieve-test",
                        json={"query": "甲在哪?", "top_k": 1})
        assert r.status_code == 200
        vec_logs = [c for c in env.obs_calls if c["call_type"] == "vector"]
        assert vec_logs and vec_logs[0]["request_data"]["topK"] == 1


class TestDependencyStatusBackendAware:
    """向量后端下线后状态口径(Milvus 清理):依赖状态固定探 pg_vector
    (pgvector 单后端),不再有 backend 分流。"""

    def test_probes_pg_vector_only(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from domains.knowledge_graph.api import router
        from domains.knowledge_graph import probes as kg_probes

        called = []
        monkeypatch.setattr(kg_probes, "pg_vector",
                            lambda s: called.append("pg") or None)
        app = FastAPI()
        app.include_router(router, prefix="/api/packs/knowledge_graph")
        app.state.llm_client = None
        app.state.settings_store = None
        r = TestClient(app).get("/api/packs/knowledge_graph/dependency-status")
        assert r.status_code == 200
        body = r.json()
        assert "pg_vector" in body      # 固定键,不再随 env 分流
        assert called == ["pg"]
        assert not hasattr(kg_probes, "milvus")  # milvus 探针已删


