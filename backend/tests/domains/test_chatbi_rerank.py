"""P0#4 ChatBI rerank 接线测试 —— 候选表重排/降级/默认关不变。

对齐 tests/domains/test_chatbi_retrieval.py 的替身形态:
  - 候选用真实 SearchResult/VectorRecord(stores 数据类),id 带 "model:"
    前缀(经 decode_chunk_id 反解出 name/type,同真实索引行);
  - store 为最小 search_records 替身(签名对齐适配层);embedder 用
    ChatBIEmbedder 包最小 llm(同 FakeLLM.embeddings 模式)。
"""
from types import SimpleNamespace

from domains.chatbi import retrieval as rt
from domains.chatbi.stores import ChatBIEmbedder, SearchResult, VectorRecord

# 显式 scope 直连(绕过关系库;同 test_chatbi_retrieval 的直连模式)
_SCOPE = "44444444-4444-4444-4444-444444444444"


class _Hit:
    """构造真实形态的检索命中(SearchResult + VectorRecord)。"""

    def __init__(self, rid, score, text):
        self.record = VectorRecord(
            id=f"model:{rid}", vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            metadata={"name": rid, "type": "model"}, text=text)
        self.score = score


def _fake_store(hits):
    """最小向量库替身:search_records 原样返回预置命中(签名对齐适配层)。"""
    return SimpleNamespace(
        search_records=lambda sc, vec, top_k, score_threshold, doc_id:
            list(hits))


def _fake_embedder():
    """embedder 替身:ChatBIEmbedder 包最小 llm(固定 8 维向量)。"""
    llm = SimpleNamespace(
        embeddings=lambda texts, conv_id=None, stage=None:
            [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts])
    return ChatBIEmbedder(llm)


class TestChatbiRerank:

    def test_rerank_reorders_before_topk(self, monkeypatch):
        """开关开:rerank 序替代向量分序,top_k 截断吃重排后的顺序。"""
        hits = [_Hit("t-low-vec", 0.30, "订单表"), _Hit("t-high-vec", 0.90, "考勤表")]
        order_calls = []
        monkeypatch.setattr(rt, "_llm_refine",
                            lambda q, cands, llm=None, conv_id=None:
                            rt.RetrievalResult(models=rt._candidates_to_models(cands)))
        fake_llm = SimpleNamespace(
            # 降序索引语义(同平台契约): [0,1] = 订单表(索引0)最相关
            rerank=lambda q, texts, conv_id=None, stage=None:
                order_calls.append(texts) or [0, 1])
        out = rt.retrieve("查订单", _fake_store(hits), embedder=_fake_embedder(),
                          scope=_SCOPE, llm=fake_llm, rerank_enabled=True, top_k=1)
        assert [m["name"] for m in out.models] == ["t-low-vec"]  # 重排后截断保留订单表
        assert order_calls and order_calls[0][0] == "订单表"     # documents=候选 text
        assert order_calls[0][1] == "考勤表"

    def test_rerank_failure_degrades_to_score_order(self, monkeypatch):
        """失败降级:回退向量分序(现状行为),检索不阻断。"""
        hits = [_Hit("a", 0.30, "x"), _Hit("b", 0.90, "y")]
        monkeypatch.setattr(rt, "_llm_refine",
                            lambda q, cands, llm=None, conv_id=None:
                            rt.RetrievalResult(models=rt._candidates_to_models(cands)))
        fake_llm = SimpleNamespace(
            rerank=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
        out = rt.retrieve("q", _fake_store(hits), embedder=_fake_embedder(),
                          scope=_SCOPE, llm=fake_llm, rerank_enabled=True, top_k=1)
        assert [m["name"] for m in out.models] == ["b"]  # 向量分序兜底

    def test_disabled_default_unchanged(self, monkeypatch):
        """默认关:不调 rerank,行为与现状一致(评测集护栏根基)。"""
        hits = [_Hit("a", 0.30, "x"), _Hit("b", 0.90, "y")]
        called = []
        monkeypatch.setattr(rt, "_llm_refine",
                            lambda q, cands, llm=None, conv_id=None:
                            rt.RetrievalResult(models=rt._candidates_to_models(cands)))
        fake_llm = SimpleNamespace(
            rerank=lambda *a, **kw: called.append(1) or [0, 1])
        rt.retrieve("q", _fake_store(hits), embedder=_fake_embedder(),
                    scope=_SCOPE, llm=fake_llm, top_k=1)
        assert called == []
