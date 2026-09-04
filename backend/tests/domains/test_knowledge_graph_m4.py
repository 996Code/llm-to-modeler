"""knowledge_graph M4 检索问答单测 —— mock LLM + 内存图/向量存储。

覆盖:检索意图解析降级 / 混合检索(图谱路+向量路+来源映射)/ 上下文线性化 /
answer_question / KbSearchTool(唯一库自动/多库追问/无库提示/schema 校验)/
POST /search 端点(用户级)。
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import retrieval, runtime
from domains.knowledge_graph.store import KGStore
from domains.knowledge_graph.tools.kb_search import KbSearchTool
from sdk.tool import ToolContext


# ── 测试替身 ─────────────────────────────────────────────────

class MockLLM:
    """kg.query → 固定意图;kg.answer → 固定回答;embeddings → 固定向量。"""

    def __init__(self):
        self.query_result = {"entities": ["甲"], "keywords": [], "hop": 1}
        self.chat_calls: list = []

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None):
        if stage == "kg.query":
            return self.query_result
        return {}

    def chat(self, messages, temperature=None, max_tokens=None, conv_id=None, stage=None):
        self.chat_calls.append((stage, messages[0]["content"]))
        return "模拟回答:甲任职于A部门。"

    def embeddings(self, texts, conv_id=None, stage=None):
        return [[0.1, 0.2, 0.3] for _ in texts]


class RetrievalFakeGraph:
    """带检索语义的内存图(find_entities / subgraph_around)。"""

    def __init__(self):
        # (kb, normalized) -> node
        self.nodes = {
            ("kb1", "甲"): {"id": "kb1:甲", "name": "甲", "normalized": "甲",
                            "type": "person", "description": "一号人物",
                            "aliases": [], "sourceDocs": [], "typeStatus": "approved",
                            "updatedAt": ""},
            ("kb1", "a部门"): {"id": "kb1:a部门", "name": "A部门", "normalized": "a部门",
                                "type": "department", "description": "", "aliases": [],
                                "sourceDocs": [], "typeStatus": "approved", "updatedAt": ""},
        }
        self.edges = [{
            "id": "e1", "kb": "kb1", "doc_id": "d1",
            "source": "kb1:甲", "target": "kb1:a部门",
            "type": "任职于", "description": "担任职务", "evidence": "甲担任A部门经理",
        }]

    def find_entities(self, kb_id, terms, limit=20):
        out = []
        for t in terms:
            for (kb, normalized), n in self.nodes.items():
                if kb == kb_id and (normalized == t.lower() or t in n["name"]):
                    out.append(n)
        return out[:limit]

    def subgraph_around(self, kb_id, seed_names, hops=2, max_nodes=80, max_edges=150):
        seeds = [n for (kb, nn), n in self.nodes.items()
                 if kb == kb_id and nn in seed_names]
        nodes, edges = list(seeds), []
        ids = {n["id"] for n in nodes}
        for e in self.edges:
            if e["kb"] != kb_id:
                continue
            for nid in (e["source"], e["target"]):
                other = self.nodes.get((kb_id, nid.rsplit(":", 1)[-1]))
                if other and other["id"] not in ids and len(nodes) < max_nodes:
                    nodes.append(other)
                    ids.add(other["id"])
            if e["source"] in ids and e["target"] in ids:
                edges.append(e)
        return {"nodes": nodes[:max_nodes], "edges": edges[:max_edges]}


class RetrievalFakeVector:
    def __init__(self):
        self.hits = [{"chunkId": "c1", "docId": "d1", "seq": 0,
                      "text": "甲担任A部门经理,负责研发。", "score": 0.88}]

    def search(self, kb_id, query_vector, top_k=5, doc_id=None):
        return self.hits[:top_k]


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "kg.db"))
    monkeypatch.setenv("KG_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
    runtime.reset_runtime_cache()

    llm = MockLLM()
    graph, vector = RetrievalFakeGraph(), RetrievalFakeVector()
    monkeypatch.setattr(runtime, "get_graph", lambda state: graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: vector)
    app_state = SimpleNamespace(llm_client=llm, settings_store=None)

    store = runtime.get_kg_store(app_state)
    yield SimpleNamespace(store=store, llm=llm, graph=graph, vector=vector,
                          app_state=app_state)
    runtime.reset_runtime_cache()


KB1 = {"id": "kb1", "name": "一号库", "description": "", "schema": {"relation_types": []},
       "vectorEnabled": True, "vectorDim": 3}


def _ctx(env):
    return ToolContext(llm_client=env.llm, asset_client=None, conversation=None,
                       emit=lambda *a, **k: None, conv_id="conv-test")


# ── 检索编排 ─────────────────────────────────────────────────

class TestRetrieval:

    def test_hybrid_retrieve_graph_and_vector(self, env):
        env.store.create_document("d1-doc", "handbook.md", "text/markdown", 10, "", "h1")
        # 让来源映射能找到文档名:直接造一条属于 kb1 的文档记录
        env.store.create_kb("一号库")
        doc = env.store.create_document("kb1", "handbook.md", "text/markdown", 10, "", "h1")
        env.vector.hits[0]["docId"] = doc["id"]

        out = retrieval.hybrid_retrieve(env.app_state, KB1, "甲在哪个部门?")
        assert out["intent"]["entities"] == ["甲"]
        assert [n["name"] for n in out["seeds"]] == ["甲"]
        assert {n["name"] for n in out["subgraph"]["nodes"]} >= {"甲", "A部门"}
        assert out["subgraph"]["edges"][0]["type"] == "任职于"
        assert out["chunks"] and out["chunks"][0]["docName"] == "handbook.md"

    def test_intent_parse_degrades_on_llm_failure(self, env):
        def boom(*a, **k):
            raise RuntimeError("llm down")
        env.llm.chat_json = boom
        out = retrieval.parse_query_intent(env.app_state, "任意问题?", [])
        assert out["keywords"] == ["任意问题?"] and out["hop"] == 1

    def test_linearize_context(self, env):
        retrieved = {"subgraph": {"nodes": list(env.graph.nodes.values()),
                                  "edges": env.graph.edges}, "chunks": [
            {"docName": "handbook.md", "text": "甲担任A部门经理。", "score": 0.9}]}
        ctx = retrieval.linearize_context(retrieved)
        assert any("任职于" in t for t in ctx["triples"])
        assert any("甲" in n for n in ctx["node_details"])
        assert any("handbook.md" in c for c in ctx["chunk_texts"])

    def test_answer_question(self, env):
        result = retrieval.answer_question(env.app_state, KB1, "甲在哪?")
        assert result["answer"].startswith("模拟回答")
        assert result["sources"]["entities"] and "甲" in result["sources"]["entities"]
        assert (result["subgraph"]["nodes"])


# ── kb_search 工具 ───────────────────────────────────────────

class TestKbSearchTool:

    def _retarget_graph(self, env, kb_id: str):
        """把内存图的 kb1 键重定向到新建库 id(真实建库是 uuid)。"""
        env.graph.nodes = {(kb_id, nn): {**n, "id": f"{kb_id}:{nn}"}
                           for (_, nn), n in env.graph.nodes.items()}
        for e in env.graph.edges:
            if e["kb"] == "kb1":
                e["kb"] = kb_id
                e["source"] = f"{kb_id}:{e['source'].rsplit(':', 1)[-1]}"
                e["target"] = f"{kb_id}:{e['target'].rsplit(':', 1)[-1]}"

    def test_single_kb_auto_selected(self, env):
        kb = env.store.create_kb("唯一库")
        self._retarget_graph(env, kb["id"])
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?"}, _ctx(env))
        # 三态契约:reply 与 artifact 互斥,回答文本进 summary(气泡),子图进 artifact(数据卡)
        assert result.summary.startswith("模拟回答")
        assert result.reply is None
        assert result.artifact_type == "data"
        assert result.artifact["type"] == "kg_search_result"
        assert result.artifact["kb"]["name"] == "唯一库"
        fmt = tool.format_result(result.artifact)
        assert fmt["nodeCount"] == 2 and fmt["edgeCount"] == 1

    def test_multiple_kbs_asks(self, env):
        env.store.create_kb("库一"); env.store.create_kb("库二")
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?"}, _ctx(env))
        assert result.ask is not None
        assert {o.label for o in result.ask.questions[0].options} == {"库一", "库二"}

        # 追问恢复:引擎注入 clarify_answers(与 njmind_form 同一约定)后选中库二
        resumed = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"kb": "库二"}}, _ctx(env))
        assert resumed.artifact and resumed.artifact["kb"]["name"] == "库二"

    def test_kb_hint_resolves_by_name(self, env):
        env.store.create_kb("指定库")
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?", "kb": "指定库"}, _ctx(env))
        assert result.artifact["kb"]["name"] == "指定库"

    def test_unknown_kb_hint(self, env):
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "x", "kb": "不存在"}, _ctx(env))
        assert result.error_for_llm and "不存在" in result.error_for_llm

    def test_no_kb_reply(self, env):
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "x"}, _ctx(env))
        assert result.reply and "知识库" in result.reply

    def test_validate_input(self, env):
        assert KbSearchTool(env.app_state).validate_input({}) is not None
        assert KbSearchTool(env.app_state).validate_input({"user_input": "q"}) is None


# ── /search 端点 ─────────────────────────────────────────────

@pytest.fixture()
def search_client(env, monkeypatch):
    from domains.knowledge_graph.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    return TestClient(app), env


class TestSearchEndpoint:

    def test_search_single_kb(self, search_client):
        client, env = search_client
        env.store.create_kb("唯一库")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "甲在哪个部门?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer"].startswith("模拟回答")
        assert body["user"] == "anonymous"  # 无 X-User-Id 头时的缺省
        assert body["kb"]["name"] == "唯一库"

    def test_search_requires_query(self, search_client):
        client, _ = search_client
        assert client.post("/api/packs/knowledge_graph/search",
                           json={"query": " "}).status_code == 422

    def test_search_multi_kb_needs_kb_param(self, search_client):
        client, env = search_client
        env.store.create_kb("库一"); env.store.create_kb("库二")
        r = client.post("/api/packs/knowledge_graph/search", json={"query": "x"})
        # 用户级端点不回显库名清单(枚举探测面)——只给数量与指引
        assert r.status_code == 422 and "2 个知识库" in r.text
        assert "库一" not in r.text and "库二" not in r.text

    def test_search_no_kb_at_all(self, search_client):
        client, _ = search_client
        assert client.post("/api/packs/knowledge_graph/search",
                           json={"query": "x"}).status_code == 404
