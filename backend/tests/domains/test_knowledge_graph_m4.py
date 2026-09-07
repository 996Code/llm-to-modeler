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

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
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
    # 检索观测的 fake sink:收集 save_call_log 调用(真实库在 test_admin_api 覆盖,
    # 这里只验证检索侧写入的 call_type/stage/结构)
    calls: list = []
    fake_cs = SimpleNamespace(
        save_call_log=lambda **kw: calls.append(kw) or "log-id")
    app_state = SimpleNamespace(llm_client=llm, settings_store=None,
                                conversation_store=fake_cs)

    store = runtime.get_kg_store(app_state)
    yield SimpleNamespace(store=store, llm=llm, graph=graph, vector=vector,
                          app_state=app_state, obs_calls=calls, tmp_path=tmp_path)
    runtime.reset_runtime_cache()


KB1 = {"id": "kb1", "name": "一号库", "description": "", "schema": {"relation_types": []},
       "vectorEnabled": True, "vectorDim": 3}


def _ctx(env, with_memory=False, mem_conv=None):
    """构造工具上下文;with_memory=True 时挂真实会话记忆(临时 SQLite)。

    mem_conv:传入 (store, conv_id) 复用同一会话(模拟"同一会话的下一轮");
    不传则新建会话。
    """
    session_state = None
    if with_memory:
        from services.conversation_store import ConversationStore
        from sdk.tool import SessionStateHandle
        if mem_conv is not None:
            cs, conv_id = mem_conv
        else:
            cs = ConversationStore(str(env.tmp_path / "conv_mem.db"))
            conv_id = cs.create_conversation("tester")["id"]
        session_state = SessionStateHandle(cs, conv_id, "knowledge_graph")
    return ToolContext(llm_client=env.llm, asset_client=None, conversation=None,
                       emit=lambda *a, **k: None, conv_id="conv-test",
                       session_state=session_state)


def _mem_conv(env):
    """建一个真实会话记忆底座(store, conv_id),跨轮共享用。"""
    from services.conversation_store import ConversationStore
    cs = ConversationStore(str(env.tmp_path / "conv_mem.db"))
    return cs, cs.create_conversation("tester")["id"]


# ── 检索编排 ─────────────────────────────────────────────────

class TestRetrieval:

    def test_vector_failure_single_degraded_log(self, env, monkeypatch):
        """【回归锚】向量路失败只落一条降级日志(search 失败与前置失败均单条)。"""
        from domains.knowledge_graph import retrieval

        class BoomVector:
            def search(self, *a, **k):
                raise RuntimeError("milvus down")

        monkeypatch.setattr(env, "vector", BoomVector())
        monkeypatch.setattr(retrieval.runtime, "get_vector", lambda state: BoomVector())
        monkeypatch.setattr(retrieval, "parse_query_intent",
                            lambda *a, **k: {"entities": ["x"], "keywords": [], "hop": 1})
        kb = dict(KB1)
        result = retrieval.hybrid_retrieve(env.app_state, kb, "q", conv_id="c1")
        vec_logs = [c for c in env.obs_calls if c["call_type"] == "vector"]
        assert len(vec_logs) == 1, f"search 失败应只落 1 条,实际 {len(vec_logs)}"
        assert vec_logs[0]["error_message"]
        assert result["chunks"] == []

    def test_retrieval_calls_logged(self, env):
        """【观测回归锚】混合检索的图/向量调用入 call_logs:
        类型/stage/请求参数/响应指标(命中/召回量/分数)全量可查。"""
        from domains.knowledge_graph import retrieval
        kb = dict(KB1)
        retrieval.hybrid_retrieve(env.app_state, kb, "张三负责什么?", conv_id="conv-obs")
        by_stage = {c["request_data"].get("stage"): c for c in env.obs_calls
                    if c["call_type"] in ("graph", "vector")}
        # 图谱两步
        assert "kg.find_entities" in by_stage
        fe = by_stage["kg.find_entities"]
        assert fe["request_data"]["terms"] and fe["conv_id"] == "conv-obs"
        assert fe["response_data"]["hits"] >= 1
        assert fe["response_data"]["termDetail"], "逐词命中明细缺失"
        assert "kg.subgraph" in by_stage
        sub = by_stage["kg.subgraph"]
        assert "nodes" in sub["response_data"] and "triples" in sub["response_data"]
        assert "nodesTruncated" in sub["response_data"], "截断水位缺失"
        # 向量一步
        assert "kg.vector_search" in by_stage
        vec = by_stage["kg.vector_search"]
        assert vec["response_data"]["hits"] >= 1
        assert vec["response_data"]["topScore"] is not None
        assert vec["response_data"]["results"], "逐条命中明细缺失"
        # 计时与错误字段存在
        for c in env.obs_calls:
            assert c["duration_ms"] >= 0 and c["error_message"] is None


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

    def test_multiple_kbs_asks_all_options(self, env):
        """选项不截断:5 个库全部出现在追问选项里(前端多选项自动折叠成可搜索下拉)。"""
        names = [f"库{i}" for i in range(5)]
        for n in names:
            env.store.create_kb(n)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?"}, _ctx(env))
        assert result.ask is not None
        assert {o.label for o in result.ask.questions[0].options} == set(names)

        # 追问恢复:引擎注入 clarify_answers(与 njmind_form 同一约定)后选中库2
        resumed = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"kb": "库2"}}, _ctx(env))
        assert resumed.artifact and resumed.artifact["kb"]["name"] == "库2"

    def test_clarify_text_answer_resolves_kb(self, env):
        """用户直接打字回答(前端 {text: 原话} 形态):精确名/唯一包含匹配。"""
        env.store.create_kb("诛仙测试库"); env.store.create_kb("产品手册库")
        tool = KbSearchTool(env.app_state)
        # 精确名
        r1 = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"text": "诛仙测试库"}}, _ctx(env))
        assert r1.artifact and r1.artifact["kb"]["name"] == "诛仙测试库"
        # 整句里的唯一包含("在诛仙测试库里查")
        r2 = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"text": "在诛仙测试库里查一下"}}, _ctx(env))
        assert r2.artifact and r2.artifact["kb"]["name"] == "诛仙测试库"
        # 多个包含命中(都含"库")→ 仍追问,不瞎猜
        r3 = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"text": "随便哪个库"}}, _ctx(env))
        assert r3.ask is not None

    def test_clarify_structured_answer_from_option_click(self, env):
        """前端追问卡片点选项({header: label} 结构化形态):按 header 取答案。"""
        env.store.create_kb("库一"); env.store.create_kb("库二")
        tool = KbSearchTool(env.app_state)
        resumed = tool.execute(
            {"user_input": "甲在哪?", "clarify_answers": {"知识库": "库一", "text": "库一"}},
            _ctx(env))
        assert resumed.artifact and resumed.artifact["kb"]["name"] == "库一"

    def test_pack_params_default_kb(self, env):
        """宿主注入 pack_params(knowledge_graph.kb)指定默认库:免追问直接检索。"""
        env.store.create_kb("库一"); env.store.create_kb("宿主指定库")
        self._retarget_graph(env, [k["id"] for k in env.store.list_kbs()
                                   if k["name"] == "宿主指定库"][0])
        tool = KbSearchTool(env.app_state)
        state = {"user_input": "甲在哪?",
                 "pack_params": {"knowledge_graph": {"kb": "宿主指定库"}}}
        result = tool.execute(state, _ctx(env))
        assert result.artifact and result.artifact["kb"]["name"] == "宿主指定库"
        # 用户显式指定(state.kb)优先于宿主默认
        state2 = {"user_input": "甲在哪?", "kb": "库一",
                  "pack_params": {"knowledge_graph": {"kb": "宿主指定库"}}}
        result2 = tool.execute(state2, _ctx(env))
        # 库一无数据,但解析到了"库一"而不是默认库(不报"知识库不存在"即证明)
        assert result2.artifact is not None or result2.error_for_llm is None

    def test_pack_params_unknown_kb(self, env):
        """宿主默认库不存在:明确报错(不静默降级到追问)。"""
        env.store.create_kb("库一")
        tool = KbSearchTool(env.app_state)
        result = tool.execute(
            {"user_input": "x", "pack_params": {"knowledge_graph": {"kb": "已删除的库"}}},
            _ctx(env))
        assert result.error_for_llm and "已删除的库" in result.error_for_llm

    def test_session_memory_remembers_kb_across_turns(self, env):
        """多轮记忆:首轮选定后写入会话记忆;下一轮无任何提示直接沿用,不再追问。"""
        env.store.create_kb("库一"); env.store.create_kb("库二")
        self._retarget_graph(env, [k["id"] for k in env.store.list_kbs()
                                   if k["name"] == "库二"][0])
        tool = KbSearchTool(env.app_state)
        mc = _mem_conv(env)  # 同一会话,两轮共享

        # 第 1 轮:追问 → 用户答"库二" → 检索 + 记忆写入
        ctx1 = _ctx(env, with_memory=True, mem_conv=mc)
        r1 = tool.execute({"user_input": "甲在哪?"}, ctx1)
        assert r1.ask is not None  # 首轮无提示,先追问
        r1b = tool.execute({"user_input": "甲在哪?",
                            "clarify_answers": {"知识库": "库二"}}, ctx1)
        assert r1b.artifact and r1b.artifact["kb"]["name"] == "库二"
        assert ctx1.session_state.get("kb") == "库二"  # 已记住

        # 第 2 轮:全新 tool_state(引擎每轮重建),同一会话记忆 → 免追问直接检索
        ctx2 = _ctx(env, with_memory=True, mem_conv=mc)
        assert ctx2 is not ctx1
        r2 = tool.execute({"user_input": "甲在哪?"}, ctx2)
        assert r2.artifact and r2.artifact["kb"]["name"] == "库二"
        assert r2.ask is None  # 关键:不再追问

    def test_session_memory_invalidated_when_kb_deleted(self, env):
        """记忆的库被删除:清掉记忆回到正常解析流(追问),不报错不断流。"""
        env.store.create_kb("库一")
        tmp_kb = env.store.create_kb("临时库")
        env.store.delete_kb(tmp_kb["id"])  # 绑定后库被删
        env.store.create_kb("库三")  # 保持多库(避免唯一库自动选中掩盖行为)
        tool = KbSearchTool(env.app_state)
        ctx = _ctx(env, with_memory=True)
        ctx.session_state.set("kb", "临时库")
        result = tool.execute({"user_input": "甲在哪?"}, ctx)
        assert result.ask is not None  # 记忆失效 → 重新追问
        assert ctx.session_state.get("kb") is None  # 记忆已清理

    def test_session_memory_explicit_switch_overrides(self, env):
        """用户显式换库:本轮指定优先于记忆,且记忆更新为新库。"""
        env.store.create_kb("库A"); env.store.create_kb("库B")
        tool = KbSearchTool(env.app_state)
        ctx = _ctx(env, with_memory=True)
        ctx.session_state.set("kb", "库A")
        result = tool.execute({"user_input": "甲在哪?", "kb": "库B"}, ctx)
        assert result.artifact and result.artifact["kb"]["name"] == "库B"
        assert ctx.session_state.get("kb") == "库B"  # 绑定已切换

    def test_session_state_handle_fail_open(self, env):
        """存储异常时句柄 fail-open:get 返回默认、set 静默,不阻断工具。"""
        from sdk.tool import SessionStateHandle
        broken = SimpleNamespace(
            get_pack_state=lambda *a: (_ for _ in ()).throw(RuntimeError("db down")),
            set_pack_state=lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
        h = SessionStateHandle(broken, "conv", "knowledge_graph")
        assert h.get("kb", "默认库") == "默认库"  # 不抛
        h.set("kb", "x")  # 不抛
        h.pop("kb")  # 不抛

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
