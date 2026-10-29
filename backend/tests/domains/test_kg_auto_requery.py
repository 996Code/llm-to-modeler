"""D17 检索自主循环——证据不足时判定→改写补检一轮→父块去重融合。

覆盖:判定 fail-open(异常/格式坏/充分=跳过)、零命中不判、
融合去重保留高分、块数上限、补检失败降级首轮。
"""
import pytest

from domains.knowledge_graph import retrieval
from domains.knowledge_graph.retrieval import _judge_evidence, _merge_retrieved

from tests.domains.test_knowledge_graph_m4 import MockLLM, env  # noqa: F401


class _SufficiencyLLM(MockLLM):
    """kg.sufficiency 阶段返回可脚本化判定。"""

    def __init__(self, sufficient=True, better=""):
        super().__init__()
        self._sufficient, self._better = sufficient, better
        self.calls = 0

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
        if stage == "kg.sufficiency":
            self.calls += 1
            return {"sufficient": self._sufficient, "better_query": self._better}
        return super().chat_json(messages, temperature=temperature,
                                 conv_id=conv_id, stage=stage, model=model)


def _retrieved(*scores):
    return {"chunks": [{"chunkId": f"c{i}#0", "score": s, "text": f"块{i}"}
                       for i, s in enumerate(scores)],
            "subgraph": {"nodes": [], "edges": []}}


class TestJudgeEvidence:

    def test_不足时返回改写词(self, env):
        llm = _SufficiencyLLM(sufficient=False, better="劳动合同解除条件")
        env.app_state.llm_client = llm
        bq = _judge_evidence(env.app_state, {"name": "库"}, "q", _retrieved(0.8), "c")
        assert bq == "劳动合同解除条件"

    def test_充分时返回None(self, env):
        llm = _SufficiencyLLM(sufficient=True)
        env.app_state.llm_client = llm
        assert _judge_evidence(env.app_state, {"name": "库"}, "q",
                               _retrieved(0.8), "c") is None

    def test_异常fail_open(self, env):
        class _Boom(MockLLM):
            def chat_json(self, *a, **k):
                if k.get("stage") == "kg.sufficiency":
                    raise RuntimeError("炸")
                return super().chat_json(*a, **k)

        env.app_state.llm_client = _Boom()
        assert _judge_evidence(env.app_state, {"name": "库"}, "q",
                               _retrieved(0.8), "c") is None

    def test_零命中不判定(self, env):
        llm = _SufficiencyLLM(sufficient=False, better="x")
        env.app_state.llm_client = llm
        assert _judge_evidence(env.app_state, {"name": "库"}, "q",
                               {"chunks": [], "subgraph": {}}, "c") is None
        assert llm.calls == 0  # 零命中直接短路,判定调用都不发

    def test_改写词超长截断(self, env):
        llm = _SufficiencyLLM(sufficient=False, better="长" * 300)
        env.app_state.llm_client = llm
        assert len(_judge_evidence(env.app_state, {"name": "库"}, "q",
                                   _retrieved(0.8), "c")) <= 120


class TestRequerySharedIntent:
    """补检意图一次解析、全库共享(此前逐库重烧 kg.query,线上 3 库白耗 ~6.5s)。"""

    def test_多库意图只解析一次且共享(self, env, monkeypatch):
        llm = _SufficiencyLLM(sufficient=False, better="换个角度的检索词")
        env.app_state.llm_client = llm
        counter = {"n": 0}
        orig = llm.chat_json

        def counting(messages, **kw):
            if kw.get("stage") == "kg.query":
                counter["n"] += 1
            return orig(messages, **kw)

        monkeypatch.setattr(llm, "chat_json", counting)
        captured = []
        monkeypatch.setattr(
            retrieval, "hybrid_retrieve",
            lambda state, kb, q, **kw: captured.append(kw) or _retrieved(0.5))
        kbs = [{"id": "k1", "name": "库一", "schema": {"relation_types": []}},
               {"id": "k2", "name": "库二", "schema": {"relation_types": []}}]
        stages = []
        retrieval._requery_once(env.app_state, kbs, "原问题", _retrieved(0.8),
                                "c", None, lambda k, m: stages.append(k), None)
        assert counter["n"] == 1, f"补检意图应只解析 1 次,实际 {counter['n']}"
        assert len(captured) == 2
        assert captured[0]["intent"] is captured[1]["intent"]  # 同一意图对象
        # 补检轮 hybrid 不再各自触发意图解析(传了共享 intent)
        assert all(kw.get("intent") is not None for kw in captured)

    def test_核查阶段先于判定上报(self, env, monkeypatch):
        """kb_search.requery 先报"核查证据"再报"换角度补检"(判定期也有反馈)。"""
        env.app_state.llm_client = _SufficiencyLLM(sufficient=False, better="改写词")
        monkeypatch.setattr(
            retrieval, "hybrid_retrieve",
            lambda state, kb, q, **kw: _retrieved(0.5))
        stages = []
        retrieval._requery_once(env.app_state, [{"id": "k", "name": "库",
                                                 "schema": {}}], "q",
                                _retrieved(0.8), "c", None,
                                lambda k, m: stages.append((k, m)), None)
        requery_msgs = [m for k, m in stages if k == "kb_search.requery"]
        assert requery_msgs[0].startswith("核查")
        assert requery_msgs[1].startswith("证据不足")


class TestMergeRetrieved:

    def test_父块去重保留高分(self):
        base = _retrieved(0.9, 0.3)          # c0(0.9) c1(0.3)
        extra = {"chunks": [
            {"chunkId": "c0#1", "score": 0.5, "text": "c0 低分子块"},   # 同父块低分
            {"chunkId": "c9#0", "score": 0.7, "text": "新块"}],
            "subgraph": {"nodes": [], "edges": []}}
        out = _merge_retrieved(base, extra)
        ids = {c["chunkId"] for c in out["chunks"]}
        assert ids == {"c0#0", "c1#0", "c9#0"}   # c0 保高分版
        by = {c["chunkId"]: c for c in out["chunks"]}
        assert by["c0#0"]["score"] == 0.9

    def test_块数上限(self):
        base = _retrieved(*[0.9 - i * 0.01 for i in range(10)])
        extra = _retrieved(*[0.5 - i * 0.01 for i in range(10)])
        out = _merge_retrieved(base, extra, limit=8)
        assert len(out["chunks"]) <= 8

    def test_子图并集(self):
        n1 = {"id": "kb1:甲", "name": "甲"}
        n2 = {"id": "kb1:乙", "name": "乙"}
        e1 = {"source": "kb1:甲", "type": "任职于", "target": "kb1:乙"}
        base = {"chunks": [], "subgraph": {"nodes": [n1], "edges": [e1]}}
        extra = {"chunks": [], "subgraph": {
            "nodes": [n1, n2], "edges": [
                e1, {"source": "kb1:乙", "type": "位于", "target": "kb1:甲"}]}}
        out = _merge_retrieved(base, extra)
        assert len(out["subgraph"]["nodes"]) == 2
        assert len(out["subgraph"]["edges"]) == 2


class TestMultiRequeryAdoption:
    """多库全链路:补检成果必须进入最终 chunks/sources(回归 10-29 线上事故)。

    事故机制:answer_question_multi 在 _requery_once 之后,rerank/reorder/
    sources/返回值四处消费的是 requery 前的旧 merged_chunks 变量——
    补检捞回的高分块被首轮旧清单覆盖丢弃,多库查询答"查不到"。
    此前用例只单测 _requery_once/_merge_retrieved(各自正确),从未断言
    多库端到端的采纳,故漏网。
    """

    def test_补检块进入最终chunks与sources(self, env, monkeypatch):
        from types import SimpleNamespace

        thin = {"chunks": [{"chunkId": "a", "score": 0.5, "text": "封面元数据"}],
                "seeds": [], "intent": {},
                "subgraph": {"nodes": [], "edges": []}}
        rich = {"chunks": [{"chunkId": "b", "score": 0.9, "text": "范围:0.01Ω~99.99Ω"}],
                "seeds": [], "intent": {},
                "subgraph": {"nodes": [], "edges": []}}
        # 2 库 × (首轮 + 补检轮):前两次瘦召回,后两次补检捞回
        seq_results = [thin, thin, rich, rich]
        monkeypatch.setattr(retrieval, "hybrid_retrieve",
                            lambda app_state, kb, q, **kw: seq_results.pop(0))
        monkeypatch.setattr(retrieval, "parse_query_intent",
                            lambda *a, **kw: {"terms": [], "keywords": []})
        monkeypatch.setattr(retrieval, "_judge_evidence",
                            lambda *a, **kw: "换个角度的检索词")
        monkeypatch.setattr(retrieval, "linearize_context",
                            lambda r: {"triples": [], "node_details": [],
                                       "chunk_texts": [c["text"] for c in r["chunks"]]})
        monkeypatch.setattr(retrieval, "_prompt_loader",
                            lambda: SimpleNamespace(render=lambda *a, **kw: "ctx"))
        kbs = [{"id": "kb1", "name": "库一"}, {"id": "kb2", "name": "库二"}]
        result = retrieval.answer_question_multi(
            env.app_state, kbs, "某标准 适用于什么范围？", conv_id="c-multi")
        final_ids = {c["chunkId"] for c in result["chunks"]}
        source_texts = " ".join(c.get("text") or "" for c in
                                (result.get("sources") or {}).get("chunks") or [])
        assert "b" in final_ids, "补检捞回的高分块未进入最终 chunks"
        assert "0.01Ω" in source_texts, "补检块未进入 sources(前端引用卡数据源)"
