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
from tests.domains.test_knowledge_graph_m4 import KB1, _ctx, env  # noqa: F401


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

    @pytest.mark.parametrize("scope", [set(), {"d1"}])
    def test_scoped_graph_is_omitted_even_when_entities_match(self, env, scope):
        from tests.domains.test_knowledge_graph_m4 import KB1
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "q", doc_filter=scope)
        assert out["subgraph"] == {"nodes": [], "edges": []}
        assert out["seeds"] == []
        assert out["diagnostics"]["graphOmitted"] == "document_scope"
        assert not [c for c in env.obs_calls if c["call_type"] == "graph"]

    def test_single_kb_multi_keeps_document_scope(self, env):
        kb, docs = _mk_docs(env, 2)
        env.vector.hits = []
        out = retrieval.answer_question_multi(
            env.app_state, [kb], "元数据测试关键词", doc_filter={docs[0]["id"]})
        assert out["chunks"]
        assert {c["docId"] for c in out["chunks"]} == {docs[0]["id"]}

    def test_evidence_scope_explains_omission_and_keeps_only_allowed_docs(self, env):
        from domains.knowledge_graph.service import retrieve_evidence
        from tests.domains.test_knowledge_graph_m4 import TestKbSearchTool
        kb, docs = _mk_docs(env, 2)
        TestKbSearchTool()._retarget_graph(env, kb["id"])
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"department": "sales"})
        env.vector.hits = []
        out = retrieve_evidence(env.app_state, kb["name"], "元数据测试关键词",
                                metadata_filter={"department": "sales"})
        assert out["chunks"]
        assert {c["docId"] for c in out["chunks"]} == {docs[0]["id"]}
        assert out["subgraph"] == {"nodes": [], "edges": []}
        assert out["diagnostics"]["graphOmitted"] == "document_scope"
        assert out["diagnostics"]["docFilterCount"] == 1
        assert not any(k in c for c in out["chunks"] for k in ("childText", "parentChars"))

    def test_cross_kb_scope_keeps_first_library_documents(self, env):
        kb, docs = _mk_docs(env, 2)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"department": "sales"})
        env.store.create_kb("empty-library")
        env.vector.hits = []
        out = KbSearchTool(env.app_state).execute(
            {"user_input": "元数据测试关键词", "pack_params": {
                "knowledge_graph": {"doc_filter": {"department": "sales"}}}}, _ctx(env))
        assert out.artifact
        assert {c["docId"] for c in out.artifact["sources"]["chunks"]} == {docs[0]["id"]}

    @pytest.mark.parametrize("metadata", [{"department": "missing"}, {"department": []}])
    def test_evidence_empty_scope_never_expands(self, env, metadata):
        from domains.knowledge_graph.service import retrieve_evidence
        kb, _ = _mk_docs(env, 2)
        out = retrieve_evidence(env.app_state, kb["name"], "元数据测试关键词",
                                metadata_filter=metadata)
        assert out["chunks"] == []
        assert out["subgraph"] == {"nodes": [], "edges": []}
        assert out["diagnostics"]["docFilterCount"] == 0

    @pytest.mark.parametrize("filter_value", ["sales", "missing"])
    def test_kb_search_scope_skips_library_annotation(self, env, filter_value):
        from domains.knowledge_graph import annotations
        kb, docs = _mk_docs(env, 2)
        env.store.set_kb_vector_info(kb["id"], "test-embed", 3, True)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"department": "sales"})
        annotations.upsert_annotation(env.app_state, kb["id"], "q", "outside-scope-secret")
        out = KbSearchTool(env.app_state).execute(
            {"user_input": "元数据测试关键词", "kb": kb["name"],
             "pack_params": {"knowledge_graph": {"doc_filter": {"department": filter_value}}}},
            _ctx(env))
        assert out.artifact and not out.artifact.get("annotationHit")
        assert "outside-scope-secret" not in str(out.artifact)

    @pytest.mark.parametrize("filter_value", ["sales", "missing"])
    def test_rest_scope_skips_library_annotation(self, client, filter_value):
        from domains.knowledge_graph import annotations
        c, env = client
        kb, docs = _mk_docs(env, 2)
        env.store.set_kb_vector_info(kb["id"], "test-embed", 3, True)
        env.store.set_doc_metadata(docs[0]["id"], kb["id"], {"department": "sales"})
        annotations.upsert_annotation(env.app_state, kb["id"], "q", "outside-scope-secret")
        response = c.post("/api/packs/knowledge_graph/search", json={
            "query": "元数据测试关键词", "kb": kb["name"],
            "doc_filter": {"department": filter_value}})
        assert response.status_code == 200
        assert not response.json().get("annotationHit")
        assert "outside-scope-secret" not in response.text

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


class TestDocumentEntityScope:
    """文档实体驱动的检索收敛(通用,零业务词):意图种子命中 document
    类型实体 → 向量/全文路收敛到其实体来源文档;未命中行为不变;
    收敛后零召回回退全库;调用方显式 doc_filter 优先不被覆盖。"""

    def test_document_seed_limits_vector_hits_to_its_docs(self, env):
        kb, docs = _mk_docs(env, 3)
        env.graph.nodes[("kb1", "规程甲")] = {
            "id": "kb1:规程甲", "name": "规程甲大纲", "normalized": "规程甲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["规程甲"], "relation_types": [],
                          "keywords": [], "hop": 1}
        # fake 向量命中按真实文档注入:目标文档高分在前,兄弟文档在后
        env.vector.hits = [
            {"chunkId": f"{docs[0]['id']}-c0", "docId": docs[0]["id"], "seq": 0,
             "text": "规程甲的环境要求块", "score": 0.9},
            {"chunkId": f"{docs[1]['id']}-c0", "docId": docs[1]["id"], "seq": 0,
             "text": "兄弟文档同款条款块", "score": 0.85},
        ]
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲的环境要求")
        assert out["chunks"], "应召回目标文档片段"
        assert all(c["docId"] == docs[0]["id"] for c in out["chunks"])
        assert out["diagnostics"].get("docScopeFromEntity") == [docs[0]["id"]]

    def test_no_document_seed_keeps_full_library_behavior(self, env):
        kb, docs = _mk_docs(env, 3)
        env.graph.nodes[("kb1", "甲")] = {  # 已有普通实体(person)
            "id": "kb1:甲", "name": "甲", "normalized": "甲", "type": "person",
            "description": "", "aliases": [], "sourceDocs": [docs[0]["id"]],
            "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["甲"], "relation_types": [],
                          "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲的资料")
        assert "docScopeFromEntity" not in out["diagnostics"]

    def test_empty_scope_after_convergence_relaxes_to_full_library(self, env):
        kb, docs = _mk_docs(env, 2)
        # document 实体指向 docA,但向量路只命中 docB(收敛后零块)
        env.graph.nodes[("kb1", "规程甲")] = {
            "id": "kb1:规程甲", "name": "规程甲大纲", "normalized": "规程甲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": ["doc-missing"], "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["规程甲"], "relation_types": [],
                          "keywords": [], "hop": 1}
        env.vector.hits = [
            {"chunkId": f"{docs[1]['id']}-c0", "docId": docs[1]["id"], "seq": 0,
             "text": "兄弟文档块", "score": 0.8}]
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲的环境要求")
        assert out["chunks"], "收敛零召回应回退全库,不能空手而归"
        assert out["diagnostics"].get("docScopeRelaxed") is True

    def test_explicit_doc_filter_is_not_overridden(self, env):
        kb, docs = _mk_docs(env, 3)
        env.graph.nodes[("kb1", "规程甲")] = {
            "id": "kb1:规程甲", "name": "规程甲大纲", "normalized": "规程甲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["规程甲"], "relation_types": [],
                          "keywords": [], "hop": 1}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"文档{i}块", "score": 0.9 - i * 0.05} for i in range(3)]
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲的环境要求",
                                        doc_filter={docs[1]["id"]})
        assert all(c["docId"] == docs[1]["id"] for c in out["chunks"])
        assert "docScopeFromEntity" not in out["diagnostics"]

    def test_mid_substring_document_hit_does_not_converge(self, env):
        """泛指型号词仅是文档名中段子串(CONTAINS 兜底命中)→ 不收敛。

        场景:库里有多份"水分测定仪"规程,用户泛问型号词;短词是长文档
        实体名的中段——命中种子但不是点名,误收敛会挡掉同类多文档素材。
        """
        kb, docs = _mk_docs(env, 3)
        env.graph.nodes[("kb1", "jjf1381-2012原棉水分测定仪型式评价大纲")] = {
            "id": "kb1:d1381", "name": "JJF 1381-2012 原棉水分测定仪型式评价大纲",
            "normalized": "jjf1381-2012原棉水分测定仪型式评价大纲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"水分测定仪文档{i}块", "score": 0.9 - i * 0.05} for i in range(3)]
        env.llm.query_result = {"entities": ["水分测定仪"], "relation_types": [],
                          "keywords": [], "hop": 1}
        # fake graph 按 normalized 精确匹配:意图词是中段子串,真实图
        # CONTAINS 兜底会命中——这里直接模拟该兜底结果(种子已返回)
        env.graph.nodes[("kb1", "水分测定仪")] = env.graph.nodes[
            ("kb1", "jjf1381-2012原棉水分测定仪型式评价大纲")]
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "水分测定仪怎么校准")
        assert "docScopeFromEntity" not in out["diagnostics"]
        assert {c["docId"] for c in out["chunks"]} == {d["id"] for d in docs}

    def test_multiple_document_seeds_merge_scope(self, env):
        """多个 document 实体(同一规程的不同版本)合并为范围。"""
        kb, docs = _mk_docs(env, 3)
        for key, name, doc in (
                ("jjf1规程甲大纲", "JJF 1 规程甲大纲", docs[0]),
                ("jjf2规程甲大纲", "JJF 2 规程甲大纲", docs[1])):
            env.graph.nodes[("kb1", key)] = {
                "id": f"kb1:{key}", "name": name, "normalized": key,
                "type": "document", "description": "", "aliases": [],
                "sourceDocs": [doc["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"规程甲v{i}块", "score": 0.9 - i * 0.05} for i in range(3)]
        env.llm.query_result = {"entities": ["JJF 1", "JJF 2"],
                          "relation_types": [], "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲新旧版本差异")
        assert set(out["diagnostics"]["docScopeFromEntity"]) == {
            docs[0]["id"], docs[1]["id"]}
        assert {c["docId"] for c in out["chunks"]} == {docs[0]["id"], docs[1]["id"]}

    def test_more_than_three_source_docs_means_generic_not_named(self, env):
        """document 实体来源文档过多=泛指引用,不收敛。"""
        kb, docs = _mk_docs(env, 4)
        env.graph.nodes[("kb1", "总纲")] = {
            "id": "kb1:总纲", "name": "总纲", "normalized": "总纲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [d["id"] for d in docs], "typeStatus": "approved",
            "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"文档{i}块", "score": 0.9 - i * 0.05} for i in range(4)]
        env.llm.query_result = {"entities": ["总纲"], "relation_types": [],
                          "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "总纲相关要求")
        assert "docScopeFromEntity" not in out["diagnostics"]

    def test_scope_widens_vector_window_before_filter(self, env):
        """P1:收敛时放大检索窗再过滤——全库 top_k 里兄弟占席时,目标
        文档排在 LIMIT 之后的块也要拿得到(先过滤后截断)。"""
        kb, docs = _mk_docs(env, 3)
        env.graph.nodes[("kb1", "规程甲")] = {
            "id": "kb1:规程甲", "name": "规程甲大纲", "normalized": "规程甲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["规程甲"], "relation_types": [],
                                "keywords": [], "hop": 1}
        # 8 条命中:兄弟文档 4 条在前,目标文档 4 条排在 6-9 位
        # (默认 top_k=5 只能取到第 1 条目标块;放大窗后应取足 5 条目标块)
        env.vector.hits = (
            [{"chunkId": f"{docs[1]['id']}-b{i}", "docId": docs[1]["id"], "seq": i,
              "text": f"兄弟块{i}", "score": 0.9 - i * 0.01} for i in range(4)] +
            [{"chunkId": f"{docs[0]['id']}-a{i}", "docId": docs[0]["id"], "seq": i,
              "text": f"目标块{i}", "score": 0.85 - i * 0.01} for i in range(4)])
        seen = {}
        env.vector.search = lambda kb_id, qvec, top_k=5: (
            seen.setdefault('k', []).append(top_k),
            env.vector.hits[:top_k])[1]
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲的环境要求")
        # 目标文档 4 条块全部取到(含排在全库第 8 位的 a3——旧的全库
        # top_k=5 后过滤只能拿到 1 条);兄弟块 0 条
        assert len(out["chunks"]) == 4, "放大窗后应取足目标文档全部块"
        assert all(c["docId"] == docs[0]["id"] for c in out["chunks"])
        assert max(seen['k']) >= 8, "检索窗确实放大到覆盖第 8 位"

    def test_short_genre_word_is_not_naming(self, env):
        """归一化后 <3 字的体裁词(大纲/总纲/规程)不算点名,不收敛。"""
        kb, docs = _mk_docs(env, 2)
        env.graph.nodes[("kb1", "大纲")] = {
            "id": "kb1:大纲", "name": "检定规程大纲", "normalized": "大纲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"文档{i}块", "score": 0.9 - i * 0.05} for i in range(2)]
        env.llm.query_result = {"entities": ["大纲"], "relation_types": [],
                                "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "大纲要求")
        assert "docScopeFromEntity" not in out["diagnostics"]

    def test_vector_search_failure_still_relaxes(self, env):
        """收敛场景向量检索抛异常:不提前放弃,回退轮接住(第二次成功)。"""
        kb, docs = _mk_docs(env, 2)
        env.graph.nodes[("kb1", "规程甲")] = {
            "id": "kb1:规程甲", "name": "规程甲大纲", "normalized": "规程甲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.llm.query_result = {"entities": ["规程甲"], "relation_types": [],
                                "keywords": [], "hop": 1}
        good = [{"chunkId": f"{docs[0]['id']}-c0", "docId": docs[0]["id"],
                 "seq": 0, "text": "目标块", "score": 0.9}]
        calls = {"n": 0}

        def flaky(kb_id, qvec, top_k=5):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("vector backend down")
            return good
        env.vector.search = flaky
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "规程甲的环境要求")
        assert out["chunks"], "检索失败后回退轮应接住,不能空手而归"
        assert out["diagnostics"].get("vectorDegraded") is True
        assert out["diagnostics"].get("docScopeRelaxed") is True

    def test_generic_word_prefix_of_doc_name_is_not_naming(self, env):
        """通用器具词恰为文档名前缀("压力表"→"压力表检定规程")且无
        编号特征、覆盖不过半 → 不算点名,不收敛(多份同类规程素材保留)。"""
        kb, docs = _mk_docs(env, 2)
        env.graph.nodes[("kb1", "压力表检定规程")] = {
            "id": "kb1:p", "name": "压力表检定规程", "normalized": "压力表检定规程",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"压力表规程{i}块", "score": 0.9 - i * 0.05} for i in range(2)]
        env.llm.query_result = {"entities": ["压力表"], "relation_types": [],
                                "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "压力表怎么检定")
        assert "docScopeFromEntity" not in out["diagnostics"]
        assert {c["docId"] for c in out["chunks"]} == {d["id"] for d in docs}

    def test_identifier_prefix_is_naming_even_when_short_coverage(self, env):
        """编号前缀("JJF 1381"覆盖长标题不足半)仍是点名——标识符信号。"""
        kb, docs = _mk_docs(env, 2)
        env.graph.nodes[("kb1", "jjf1381-2012原棉水分测定仪型式评价大纲")] = {
            "id": "kb1:1381", "name": "JJF 1381-2012 原棉水分测定仪型式评价大纲",
            "normalized": "jjf1381-2012原棉水分测定仪型式评价大纲",
            "type": "document", "description": "", "aliases": [],
            "sourceDocs": [docs[0]["id"]], "typeStatus": "approved", "updatedAt": ""}
        env.vector.hits = [
            {"chunkId": f"{docs[i]['id']}-c0", "docId": docs[i]["id"], "seq": 0,
             "text": f"文档{i}块", "score": 0.9 - i * 0.05} for i in range(2)]
        env.llm.query_result = {"entities": ["JJF 1381"], "relation_types": [],
                                "keywords": [], "hop": 1}
        out = retrieval.hybrid_retrieve(env.app_state, KB1, "JJF 1381 的实验环境")
        assert out["diagnostics"].get("docScopeFromEntity") == [docs[0]["id"]]
