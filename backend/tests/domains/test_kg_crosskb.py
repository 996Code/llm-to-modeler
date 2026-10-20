"""N3 跨库(多库)检索 —— answer_question_multi 合并策略 + kb_search 跨库默认 + /search kb 数组。

复用 m4 的 env / search_client fixture(见 test_knowledge_graph_m4);
多库数据在本文件内装配:两库各建图谱节点/文档/向量命中(分数错开,
跨库排序可断言)。

【合并策略断言】
  - 意图解析一次(所有库共享同一 query 意图——kg.query LLM 调用计数=1)
  - chunks 跨库按相似度排序取 top_k,带 kbName 库来源标注
  - subgraph 各库 concat(节点 id 带 kb 前缀不冲突)
  - LLM 综合回答一次,资料前缀〔库名/文档名〕
  - cited_chunks 编号权威源=合并后 chunks 顺序(P0#3 契约不变)
  - 单库列表委托 answer_question(行为不变回归)
"""
import pytest

from domains.knowledge_graph import retrieval
from domains.knowledge_graph.tools.kb_search import (
    KbSearchTool, _PIPELINE_STEPS, make_monotonic_stage,
)
from tests.domains.test_kg_auto_requery import _SufficiencyLLM
from tests.domains.test_knowledge_graph_m4 import (  # noqa: F401  fixture 复用
    _ctx, _mem_conv, env, search_client,
)


# ── 多库数据装配 ─────────────────────────────────────────────

class PerKbVector:
    """按 kb_id 返回各自命中(分数错开,跨库排序可断言)。"""

    def __init__(self, hits_by_kb):
        self.hits_by_kb = hits_by_kb

    def search(self, kb_id, query_vector, top_k=5, doc_id=None):
        return list(self.hits_by_kb.get(kb_id, []))[:top_k]


KB_A = {"id": "kbA", "name": "甲库", "description": "", "schema": {"relation_types": []},
        "vectorEnabled": True, "vectorDim": 3}
KB_B = {"id": "kbB", "name": "乙库", "description": "", "schema": {"relation_types": []},
        "vectorEnabled": True, "vectorDim": 3}


def _node(kb_id, name, normalized, ntype):
    return {"id": f"{kb_id}:{normalized}", "name": name, "normalized": normalized,
            "type": ntype, "description": "", "aliases": [], "sourceDocs": [],
            "typeStatus": "approved", "updatedAt": ""}


@pytest.fixture()
def two_kbs(env, monkeypatch):
    """在 m4 env 上装配两库数据:甲库(甲→A部门,chunk 0.88)/乙库(乙→B部门,chunk 0.95)。

    返回 (kb_a, kb_b)(fabricated kb dict,id 与内存图/向量/文档记录对齐)。
    """
    g = env.graph
    g.nodes[("kbA", "甲")] = _node("kbA", "甲", "甲", "person")
    g.nodes[("kbA", "a部门")] = _node("kbA", "A部门", "a部门", "department")
    g.edges.append({"id": "eA", "kb": "kbA", "doc_id": "dA",
                    "source": "kbA:甲", "target": "kbA:a部门",
                    "type": "任职于", "description": "", "evidence": "甲担任A部门经理"})
    g.nodes[("kbB", "乙")] = _node("kbB", "乙", "乙", "person")
    g.nodes[("kbB", "b部门")] = _node("kbB", "B部门", "b部门", "department")
    g.edges.append({"id": "eB", "kb": "kbB", "doc_id": "dB",
                    "source": "kbB:乙", "target": "kbB:b部门",
                    "type": "负责", "description": "", "evidence": "乙负责B部门"})
    # 意图同时给两库的种子词
    env.llm.query_result = {"entities": ["甲", "乙"], "keywords": [], "hop": 1}
    # 文档记录(docName 映射走 store.list_documents)
    doc_a = env.store.create_document("kbA", "handbook-a.md", "text/markdown", 10, "", "ha")
    doc_b = env.store.create_document("kbB", "handbook-b.md", "text/markdown", 10, "", "hb")
    vec = PerKbVector({
        "kbA": [{"chunkId": "cA", "docId": doc_a["id"], "seq": 0,
                 "text": "甲担任A部门经理。", "score": 0.88}],
        "kbB": [{"chunkId": "cB", "docId": doc_b["id"], "seq": 0,
                 "text": "乙负责B部门。", "score": 0.95}],
    })
    monkeypatch.setattr(retrieval.runtime, "get_vector", lambda state: vec)
    return KB_A, KB_B


def _count_intent_calls(env, monkeypatch):
    """包装 chat_json,统计 kg.query(意图解析)调用次数。"""
    counter = {"n": 0}
    orig = env.llm.chat_json

    def counting(messages, **kw):
        if kw.get("stage") == "kg.query":
            counter["n"] += 1
        return orig(messages, **kw)

    monkeypatch.setattr(env.llm, "chat_json", counting)
    return counter


# ── answer_question_multi 合并策略 ───────────────────────────

class TestAnswerQuestionMulti:

    def test_intent_parsed_once(self, two_kbs, env, monkeypatch):
        """【硬约束】多库共享同一 query 意图:kg.query LLM 调用只发一次。"""
        kb_a, kb_b = two_kbs
        counter = _count_intent_calls(env, monkeypatch)
        retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?")
        assert counter["n"] == 1, f"意图解析应只调 1 次,实际 {counter['n']}"

    def test_chunks_merged_cross_kb_sorted(self, two_kbs, env):
        """chunks 跨库按相似度排序(乙库 0.95 > 甲库 0.88)且带 kbName 标注。"""
        kb_a, kb_b = two_kbs
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?")
        assert len(out["chunks"]) == 2
        assert out["chunks"][0]["kbName"] == "乙库" and out["chunks"][0]["score"] == 0.95
        assert out["chunks"][1]["kbName"] == "甲库" and out["chunks"][1]["score"] == 0.88
        # sources.chunks 带 kbId(引用下载定位:kbId+docId)
        assert out["sources"]["chunks"][0]["kbId"] == "kbB"
        assert out["sources"]["chunks"][0]["kbName"] == "乙库"
        assert out["sources"]["chunks"][1]["kbId"] == "kbA"

    def test_chunks_cross_kb_top_k(self, two_kbs, env):
        """合并后取全局 top_k:top_k=1 只留最高分(跨库挤占单库名额)。"""
        kb_a, kb_b = two_kbs
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?",
                                              top_k=1)
        assert len(out["chunks"]) == 1 and out["chunks"][0]["kbName"] == "乙库"

    def test_cross_kb_dedup_keeps_higher_score(self, two_kbs, env, monkeypatch):
        """D13:同内容跨库重复只留高分者,被挤掉的库记进 dedupFrom。"""
        kb_a, kb_b = two_kbs
        vec = PerKbVector({
            "kbA": [{"chunkId": "cA", "docId": "dA", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.7}],
            "kbB": [{"chunkId": "cB", "docId": "dB", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.9}],
        })
        monkeypatch.setattr(retrieval.runtime, "get_vector", lambda state: vec)
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "资料在哪?")
        assert len(out["chunks"]) == 1            # 重复不占两个名额
        assert out["chunks"][0]["kbName"] == "乙库"  # 高分者保留
        assert out["chunks"][0]["dedupFrom"] == ["甲库"]

    def test_cross_kb_dedup_equal_score_keeps_first(self, two_kbs, env, monkeypatch):
        """D13:同分重复留先入列者(kbs 传入序),稳定可预期。"""
        kb_a, kb_b = two_kbs
        vec = PerKbVector({
            "kbA": [{"chunkId": "cA", "docId": "dA", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.8}],
            "kbB": [{"chunkId": "cB", "docId": "dB", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.8}],
        })
        monkeypatch.setattr(retrieval.runtime, "get_vector", lambda state: vec)
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "资料在哪?")
        assert len(out["chunks"]) == 1
        assert out["chunks"][0]["kbName"] == "甲库"
        assert out["chunks"][0]["dedupFrom"] == ["乙库"]

    def test_cross_kb_dedup_frees_top_k_slot(self, two_kbs, env, monkeypatch):
        """D13:去重腾出的名额给下一名候选(重复不再挤占 top_k)。"""
        kb_a, kb_b = two_kbs
        vec = PerKbVector({
            "kbA": [{"chunkId": "cA", "docId": "dA", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.95},
                    {"chunkId": "cA2", "docId": "dA", "seq": 1,
                     "text": "甲库独有候选。", "score": 0.6}],
            "kbB": [{"chunkId": "cB", "docId": "dB", "seq": 0,
                     "text": "同一段资料内容。", "score": 0.9}],
        })
        monkeypatch.setattr(retrieval.runtime, "get_vector", lambda state: vec)
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "资料在哪?",
                                              top_k=2)
        texts = [c["text"] for c in out["chunks"]]
        assert len(out["chunks"]) == 2
        assert "甲库独有候选。" in texts    # 腾出的名额给了它

    def test_subgraph_merged(self, two_kbs, env):
        """subgraph 各库 concat:两库节点/边都在,节点 id 带 kb 前缀不冲突。"""
        kb_a, kb_b = two_kbs
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?")
        names = {n["name"] for n in out["subgraph"]["nodes"]}
        assert {"甲", "A部门", "乙", "B部门"} <= names
        ids = [n["id"] for n in out["subgraph"]["nodes"]]
        assert len(ids) == len(set(ids)), "跨库节点 id 不应冲突"
        edge_types = {e["type"] for e in out["subgraph"]["edges"]}
        assert edge_types == {"任职于", "负责"}
        # 来源实体汇总跨库合并
        assert {"甲", "乙"} <= set(out["sources"]["entities"])

    def test_prompt_chunks_carry_kb_source(self, two_kbs, env):
        """LLM 资料前缀〔库名/文档名〕(单库路径的〔文档名〕追加库名)。"""
        kb_a, kb_b = two_kbs
        retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?")
        prompt = env.llm.chat_calls[-1][1]
        assert "〔乙库/handbook-b.md〕" in prompt and "〔甲库/handbook-a.md〕" in prompt

    def test_cited_chunks_numbering_authority(self, two_kbs, env, monkeypatch):
        """cited_chunks 编号权威源=合并后 chunks 顺序(P0#3 契约不变):
        [片段:1] 指向跨库排序后的第一名(乙库)。"""
        kb_a, kb_b = two_kbs
        monkeypatch.setattr(
            env.llm, "chat",
            lambda messages, **kw: "乙负责B部门[片段:1]。")
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "乙负责什么?")
        assert out["cited_chunks"] == [1]
        assert out["sources"]["chunks"][0]["kbName"] == "乙库"
        assert out["sources"]["chunks"][0]["docName"] == "handbook-b.md"

    def test_result_shape(self, two_kbs, env):
        """返回体:kbs 列表(逐库 id/name)+ kb 展示名(拼接,前端卡片兼容)。"""
        kb_a, kb_b = two_kbs
        out = retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?")
        assert out["answer"].startswith("模拟回答")
        assert out["kbs"] == [{"id": "kbA", "name": "甲库"}, {"id": "kbB", "name": "乙库"}]
        assert out["kb"]["name"] == "甲库、乙库"

    def test_single_kb_list_delegates(self, two_kbs, env, monkeypatch):
        """单库列表委托 answer_question:行为与单库路径一致(kb 字段单库),
        仅补 kbs 键。"""
        kb_a, _ = two_kbs
        counter = _count_intent_calls(env, monkeypatch)
        out = retrieval.answer_question_multi(env.app_state, [kb_a], "甲在哪?")
        assert out["kb"] == {"id": "kbA", "name": "甲库"}
        assert out["kbs"] == [{"id": "kbA", "name": "甲库"}]
        assert counter["n"] == 1
        # 单库 sources.chunks 也带 kbId(引用下载定位)
        assert out["sources"]["chunks"]
        assert all(c["kbId"] == "kbA" for c in out["sources"]["chunks"])
        # 单库路径(answer_question)行为不变回归:同库同问,回答/来源一致
        single = retrieval.answer_question(env.app_state, kb_a, "甲在哪?")
        assert single["answer"] == out["answer"]
        assert single["kb"] == out["kb"]
        assert "kbs" not in single

    def test_empty_kb_list_raises(self, env):
        """空库列表:显式报错(不静默返回假空答案)。"""
        with pytest.raises(ValueError, match="知识库"):
            retrieval.answer_question_multi(env.app_state, [], "任意问题?")


# ── kb_search 工具跨库默认 ───────────────────────────────────

class TestKbSearchCrossKb:

    def test_multi_kbs_default_cross_no_ask(self, env):
        """N3:多库且未指定 → 不再追问选库,直接跨全部库检索。"""
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?"}, _ctx(env))
        assert result.ask is None, "跨库默认下不应再追问"
        assert result.artifact_type == "data"
        assert result.artifact["type"] == "kg_search_result"
        assert {k["name"] for k in result.artifact["kbs"]} == {"库一", "库二"}   # 顺序无关
        assert set(result.artifact["kb"]["name"].split("、")) == {"库一", "库二"}   # 顺序无关
        assert result.summary.startswith("模拟回答")

    def test_explicit_kb_still_single(self, env):
        """显式指定库:仍走单库路径(artifact 无 kbs 键)。"""
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?", "kb": "库二"}, _ctx(env))
        assert result.artifact["kb"]["name"] == "库二"
        assert "kbs" not in result.artifact

    def test_memory_bound_kb_stays_single(self, env):
        """会话记忆绑定单库(用户显式指定过):沿用单库,不跨库。"""
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        tool = KbSearchTool(env.app_state)
        ctx = _ctx(env, with_memory=True)
        ctx.session_state.set("kb", "库二")
        result = tool.execute({"user_input": "甲在哪?"}, ctx)
        assert result.artifact["kb"]["name"] == "库二"
        assert "kbs" not in result.artifact
        # 跨库默认路径不写记忆绑定(记忆只绑单库)
        ctx2 = _ctx(env, with_memory=True)
        tool.execute({"user_input": "甲在哪?"}, ctx2)
        assert ctx2.session_state.get("kb") is None

    def test_cross_kb_across_turns_with_memory(self, env):
        """多轮:首轮显式选库写入记忆;库被删后记忆清理回到跨库。"""
        env.store.create_kb("库一")
        doomed = env.store.create_kb("临时库")
        env.store.create_kb("库三")
        tool = KbSearchTool(env.app_state)
        mc = _mem_conv(env)
        ctx1 = _ctx(env, with_memory=True, mem_conv=mc)
        r1 = tool.execute({"user_input": "甲在哪?", "kb": "临时库"}, ctx1)
        assert r1.artifact["kb"]["name"] == "临时库"
        assert ctx1.session_state.get("kb") == "临时库"
        env.store.delete_kb(doomed["id"])
        ctx2 = _ctx(env, with_memory=True, mem_conv=mc)
        r2 = tool.execute({"user_input": "甲在哪?"}, ctx2)
        assert r2.artifact and "kbs" in r2.artifact, "记忆失效后应回到跨库检索"
        assert ctx2.session_state.get("kb") is None


# ── /search 端点:kb 数组 + 跨库默认 ─────────────────────────

class TestSearchEndpointCrossKb:

    def test_search_no_kb_defaults_cross(self, search_client):
        """不传 kb = 全部库(跨库默认):多库不再 422 要求指定。"""
        client, env = search_client
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "甲在哪个部门?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["answer"].startswith("模拟回答")
        # 顺序无关断言:list_kbs 按 created_at DESC,建库顺序不构成契约
        assert {k["name"] for k in body["kbs"]} == {"库一", "库二"}

    def test_search_kb_array(self, search_client):
        """kb 传数组:跨指定库检索。"""
        client, env = search_client
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        env.store.create_kb("库三")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "q", "kb": ["库一", "库三"]})
        assert r.status_code == 200, r.text
        assert {k["name"] for k in r.json()["kbs"]} == {"库一", "库三"}   # 顺序无关

    def test_search_kb_array_skips_unknown_silently(self, search_client):
        """数组里不存在的名字静默跳过(不 404 泄露探测结果)。"""
        client, env = search_client
        env.store.create_kb("库一")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "q", "kb": ["库一", "不存在的库"]})
        assert r.status_code == 200, r.text
        assert [k["name"] for k in r.json()["kbs"]] == ["库一"]

    def test_search_kb_array_all_unknown_404(self, search_client):
        """数组全部不存在:404,不回显任何库名(防枚举)。"""
        client, env = search_client
        env.store.create_kb("库一")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "q", "kb": ["探测A", "探测B"]})
        assert r.status_code == 404
        assert "库一" not in r.text

    def test_search_kb_array_empty_422(self, search_client):
        client, _ = search_client
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "q", "kb": []})
        assert r.status_code == 422

    def test_search_kb_string_still_single(self, search_client):
        """kb 传字符串(兼容单库):走单库路径。"""
        client, env = search_client
        env.store.create_kb("库一")
        env.store.create_kb("库二")
        r = client.post("/api/packs/knowledge_graph/search",
                        json={"query": "q", "kb": "库二"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kb"]["name"] == "库二"
        assert [k["name"] for k in body["kbs"]] == ["库二"]   # 单库也带 kbs(响应形状统一)

    def test_search_no_kb_at_all_404(self, search_client):
        client, _ = search_client
        assert client.post("/api/packs/knowledge_graph/search",
                           json={"query": "x"}).status_code == 404


# ── 进度条单行道(10-20 线上"执行链路不断跳"回归) ─────────────

class TestPipelineMonotonicStage:
    """步骤表覆盖检索层全部 stage 键;上屏序列按步骤表单调,回退/未知键不上屏。"""

    def test_steps_cover_all_stage_keys(self):
        """retrieval 层实际会发的 10 个键全在步骤表——缺一个前端匹配不到就整条清零。"""
        assert {s["key"] for s in _PIPELINE_STEPS} == {
            "kb_search.resolve", "kb_search.intent", "kb_search.graph",
            "kb_search.subgraph", "kb_search.fulltext", "kb_search.vector",
            "kb_search.requery", "kb_search.rerank", "kb_search.assemble",
            "kb_search.answer",
        }
        order = [s["key"] for s in _PIPELINE_STEPS]
        # 时间顺序:补检在向量检索后、重排前
        assert (order.index("kb_search.vector") < order.index("kb_search.requery")
                < order.index("kb_search.rerank"))

    def test_regressed_stage_dropped(self):
        """跨库第二/三库与补检轮重发的回退 stage 不上屏(进度条不倒跳)。"""
        out = []
        emit = make_monotonic_stage(lambda k, m: out.append(k))
        emit("kb_search.graph", "库1")
        emit("kb_search.subgraph", "库1")
        emit("kb_search.vector", "库1")
        emit("kb_search.graph", "库2")           # 逐库回退,不上屏
        emit("kb_search.subgraph", "库2")
        emit("kb_search.vector", "库2")           # 同步骤换文案,上屏(进度不跳)
        emit("kb_search.requery", "核查/补检")    # 前进,上屏
        emit("kb_search.intent", "补检轮重发")    # 回退,不上屏
        emit("kb_search.assemble", "组装")
        assert out == ["kb_search.graph", "kb_search.subgraph", "kb_search.vector",
                       "kb_search.vector", "kb_search.requery", "kb_search.assemble"]

    def test_unknown_stage_dropped(self):
        """不在步骤表的键不上屏(此前会让前端进度条整条清零)。"""
        out = []
        emit = make_monotonic_stage(lambda k, m: out.append(k))
        emit("kb_search.whatever", "未知键")
        emit("kb_search.intent", "已知键")
        assert out == ["kb_search.intent"]

    def test_fulltext_zero_hit_flow(self):
        """图谱零命中 → 全文兜底 → 向量:全程单调(万用表零命中场景)。"""
        out = []
        emit = make_monotonic_stage(lambda k, m: out.append(k))
        emit("kb_search.graph", "匹配")
        emit("kb_search.fulltext", "兜底")
        emit("kb_search.vector", "向量")
        assert out == ["kb_search.graph", "kb_search.fulltext", "kb_search.vector"]

    def test_cross_kb_requery_e2e_monotonic(self, two_kbs, env):
        """端到端:跨两库 + 证据不足补检,上屏 stage 按步骤表严格单调。"""
        llm = _SufficiencyLLM(sufficient=False, better="甲和乙的职务是什么")
        llm.query_result = {"entities": ["甲", "乙"], "keywords": [], "hop": 1}
        env.app_state.llm_client = llm
        out = []
        on_stage = make_monotonic_stage(lambda k, m: out.append(k))
        kb_a, kb_b = two_kbs
        retrieval.answer_question_multi(env.app_state, [kb_a, kb_b], "甲和乙在哪?",
                                        on_stage=on_stage)
        order = [s["key"] for s in _PIPELINE_STEPS]
        idxs = [order.index(k) for k in out]
        assert idxs == sorted(idxs), f"上屏 stage 必须单调,实际 {out}"
        assert out.count("kb_search.intent") == 1   # 补检轮共享意图,不再重发
        assert "kb_search.requery" in out           # 补检步骤真实点亮
        assert out[-1] == "kb_search.answer"
