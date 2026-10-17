"""D7 文档元数据过滤 —— 列迁移/CRUD/白名单解析/两路检索下推/两生效点。

覆盖:set_doc_metadata+列表带出、filter_doc_ids_by_metadata(单值/
数组/AND 组合/无匹配空集/条件空 None)、hybrid_retrieve doc_filter
两路过滤(向量/全文)、kb_search 工具(pack_params 传入生效+未传
不过滤回归)、REST /search(body doc_filter 同义)。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import retrieval, runtime
from domains.knowledge_graph.api import router as kg_router
from domains.knowledge_graph.tools.kb_search import KbSearchTool
from sdk.tool import ToolContext

# 复用 m4 检索 fake 环境
from tests.domains.test_knowledge_graph_m4 import _ctx, env  # noqa: F401


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    monkeypatch.setattr(runtime, "get_graph", lambda state: env.graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: env.vector)
    return TestClient(app), env


def _mk_docs(env, n=3):
    """建库+n 文档(带块,向量/全文两路都能命中)。"""
    kb = env.store.create_kb("元数据库")
    docs = []
    for i in range(n):
        content = f"文档{i}的内容,包含关键词元数据测试。".encode()
        import hashlib
        d = env.store.create_document(
            kb["id"], f"doc{i}.md", "text/markdown", len(content), "",
            hashlib.sha256(content).hexdigest())
        env.store.replace_chunks(doc_id=d["id"], kb_id=kb["id"],
                                chunks=[{"seq": 0, "text": f"文档{i} 元数据测试关键词块"}])
        docs.append(d)
    return kb, docs


class TestStoreMetadata:

    def test_set_and_list(self, env):
        kb, docs = _mk_docs(env, 2)
        assert env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"部门": "销售"})
        listed = env.store.list_documents(kb["id"])
        by_id = {d["id"]: d for d in listed}
        assert by_id[docs[0]["id"]]["docMetadata"] == {"部门": "销售"}
        assert by_id[docs[1]["id"]]["docMetadata"] == {}   # 未设置=空

    def test_filter_semantics(self, env):
        kb, docs = _mk_docs(env, 3)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"],
                                   {"部门": "销售", "年份": "2026"})
        env.store.set_doc_metadata(docs[1]["id"], kb["id"], {"部门": "HR"})
        # 单值命中
        assert env.store.filter_doc_ids_by_metadata(kb["id"], {"部门": "销售"}) == {docs[0]["id"]}
        # AND 组合:两维都匹配才进
        assert env.store.filter_doc_ids_by_metadata(
            kb["id"], {"部门": "销售", "年份": "2026"}) == {docs[0]["id"]}
        assert env.store.filter_doc_ids_by_metadata(
            kb["id"], {"部门": "销售", "年份": "2027"}) == set()
        # 数组 OR 任一
        got = env.store.filter_doc_ids_by_metadata(
            kb["id"], {"部门": ["销售", "HR"]})
        assert got == {docs[0]["id"], docs[1]["id"]}
        # 无匹配=空集(白名单语义:零召回不是全放行)
        assert env.store.filter_doc_ids_by_metadata(kb["id"], {"部门": "法务"}) == set()
        # 条件空=None(不过滤)
        assert env.store.filter_doc_ids_by_metadata(kb["id"], {}) is None

    def test_metadata_endpoint(self, client):
        c, env = client
        kb, docs = _mk_docs(env, 1)
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{docs[0]['id']}/metadata",
                  json={"metadata": {"部门": "销售", "标签": ["内部", "公开"]}})
        assert r.status_code == 200, r.text
        assert r.json()["docMetadata"] == {"部门": "销售", "标签": ["内部", "公开"]}
        # 列表带出
        listed = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents").json()["items"]
        assert listed[0]["docMetadata"]["部门"] == "销售"
        # 清除
        assert c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{docs[0]['id']}/metadata",
                     json={"metadata": {}}).status_code == 200
        assert c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/nope/metadata",
                     json={"metadata": {}}).status_code == 404


class TestRetrieveFilter:

    def test_hybrid_filters_both_paths(self, env):
        """doc_filter 白名单:向量路与全文路命中都按白名单滤。"""
        kb, docs = _mk_docs(env, 3)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"部门": "销售"})
        env.llm.query_result = {"entities": ["查无"], "keywords": [], "hop": 1}
        # 向量路:fake hits 换成真文档块(带 #i 子块形态)
        chunks = env.store.list_chunks(docs[0]["id"])
        env.vector.hits = [{
            "chunkId": f"{chunks[0]['id']}#0", "docId": docs[0]["id"],
            "seq": 0, "text": chunks[0]["text"], "score": 0.9}]
        # 全文路会命中所有含关键词的块;白名单只留 docs[0]
        out = retrieval.hybrid_retrieve(
            env.app_state, kb, "元数据测试关键词",
            doc_filter={docs[0]["id"]})
        assert out["chunks"]
        assert all(h.get("docId") == docs[0]["id"] for h in out["chunks"])

        # 空集白名单:零召回(不是全放行)
        out2 = retrieval.hybrid_retrieve(
            env.app_state, kb, "元数据测试关键词", doc_filter=set())
        assert not out2["chunks"]

        # None:不过滤(回归)
        out3 = retrieval.hybrid_retrieve(
            env.app_state, kb, "元数据测试关键词", doc_filter=None)
        assert out3["chunks"]

    def test_kb_search_pack_params_filter(self, env):
        """kb_search 工具:pack_params 传 doc_filter 生效;未传不过滤。"""
        kb, docs = _mk_docs(env, 2)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"部门": "销售"})
        env.llm.query_result = {"entities": ["查无"], "keywords": [], "hop": 1}
        env.vector.hits = []   # 向量路关掉,只走全文路(真库可控)
        tool = KbSearchTool(env.app_state)
        state = {"user_input": "元数据测试关键词", "kb": kb["name"],
                 "pack_params": {"knowledge_graph": {
                     "doc_filter": {"部门": "销售"}}}}
        result = tool.execute(state, _ctx(env))
        assert result.artifact and not result.reply
        for c in result.artifact["sources"]["chunks"]:
            assert c.get("docId") == docs[0]["id"]

        # 未传 doc_filter:不过滤(所有块都可见)
        result2 = tool.execute({"user_input": "元数据测试关键词", "kb": kb["name"]}, _ctx(env))
        assert len(result2.artifact["sources"]["chunks"]) >= 2
        assert not result2.reply

    def test_rest_search_doc_filter(self, client, monkeypatch):
        """REST /search:body doc_filter 同义生效。"""
        c, env = client
        kb, docs = _mk_docs(env, 2)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"部门": "销售"})
        # 图谱零命中走全文(意图实体查无);mock vector 换无命中——
        # 全文路真库可控
        class _NoVec:
            def search(self, *a, **k):
                return []
        monkeypatch.setattr(runtime, "get_vector", lambda state: _NoVec())
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "元数据测试关键词", "kb": kb["name"],
                         "doc_filter": {"部门": "销售"}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert not body.get("annotationHit")
        doc_names = {c.get("docName") for c in body.get("sources", {}).get("chunks", [])}
        assert doc_names <= {"doc0.md"}

        # 过滤维度无匹配 → 检索仍 200 但零块(不是 404:过滤是检索内语义)
        r2 = c.post("/api/packs/knowledge_graph/search",
                    json={"query": "元数据测试关键词", "kb": kb["name"],
                          "doc_filter": {"部门": "法务"}})
        assert r2.status_code == 200
        assert not r2.json().get("sources", {}).get("chunks")
