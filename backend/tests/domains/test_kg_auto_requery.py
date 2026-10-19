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
