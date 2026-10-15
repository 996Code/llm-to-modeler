"""P1#8 网关增强 —— encode/encode_queries 拆分 + 嵌入缓存 + chat 缓存开关。"""
import json
from types import SimpleNamespace

import pytest

from llm.client import LLMClient


class _FakeStore:
    def __init__(self):
        self.calls = []

    def save_call_log(self, **kw):
        self.calls.append(kw)
        return "id"


class _StubEmbedder:
    """替身 local embedder:记录收到的文本(镜像真实默认行为=不加前缀)。"""
    last_texts = None

    def encode(self, texts):
        _StubEmbedder.last_texts = list(texts)
        return [[0.1, 0.2] for _ in texts]

    def encode_queries(self, texts):
        # 镜像真实 LocalEmbedder.encode_queries 默认口径:直传(bge-m3
        # 官方明确查询侧无需 instruction;EMBED_QUERY_PREFIX 开关另测)
        _StubEmbedder.last_texts = list(texts)
        return [[0.9, 0.9] for _ in texts]


def _local_client(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    monkeypatch.setattr("llm.local_embeddings.get_local_embedder",
                        lambda: _StubEmbedder())
    return LLMClient(config=None, conversation_store=_FakeStore())


class TestEmbedModes:

    def test_document_mode_default_unchanged(self, monkeypatch):
        """缺省 mode=document:直传文本(现行为零变化)。"""
        client = _local_client(monkeypatch)
        out = client.embeddings(["员工表"], stage="x")
        assert out == [[0.1, 0.2]]
        assert _StubEmbedder.last_texts == ["员工表"]

    def test_query_mode_prefixes_locally(self, monkeypatch):
        """mode=query 走 encode_queries:默认(bge-m3)不加前缀直传;
        EMBED_QUERY_PREFIX=1 时注入检索前缀(审查修正:m3 官方明确
        查询侧无需 instruction,前缀属 v1.5 系列)。"""
        client = _local_client(monkeypatch)
        out = client.embeddings(["员工表"], stage="x", mode="query")
        assert out == [[0.9, 0.9]]
        assert _StubEmbedder.last_texts == ["员工表"]   # 默认无前缀
        # 开关开:真实 encode_queries 注入前缀(裸实例绕 __init__)
        from llm.local_embeddings import _LocalEmbedder
        emb = object.__new__(_LocalEmbedder)
        seen = {}
        monkeypatch.setattr(_LocalEmbedder, "encode",
                            lambda self, ts: seen.update(texts=ts))
        monkeypatch.setenv("EMBED_QUERY_PREFIX", "1")
        emb.encode_queries(["员工表"])
        assert seen["texts"] == ["为这个句子生成表示以用于检索相关文章：员工表"]
        # 开关关(默认):直传
        monkeypatch.delenv("EMBED_QUERY_PREFIX")
        emb.encode_queries(["员工表"])
        assert seen["texts"] == ["员工表"]

    def test_api_backend_ignores_mode(self, monkeypatch):
        """API 后端无官方查询/文档区分:mode 忽略不报错。"""
        monkeypatch.setenv("EMBEDDING_BACKEND", "api")
        monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-v3")
        client = LLMClient(config=None, conversation_store=_FakeStore())
        captured = {}

        class FakeResp:
            data = [SimpleNamespace(index=0, embedding=[0.5, 0.5])]

        def fake_create(model, input):
            captured["input"] = input
            return FakeResp()

        monkeypatch.setattr(type(client.client.embeddings), "create",
                            staticmethod(fake_create))
        out = client.embeddings(["q"], mode="query", stage="x")
        assert out == [[0.5, 0.5]]
        assert captured["input"] == ["q"]   # 无前缀

    def test_invalid_mode_raises(self, monkeypatch):
        client = _local_client(monkeypatch)
        with pytest.raises(ValueError):
            client.embeddings(["q"], mode="other")


class TestEmbedCache:

    def test_second_call_zero_outbound(self, monkeypatch):
        """同参二次 embeddings:零出站(替身不再被调)——蓝图验收
        '重复 query 检索链路 LLM 调用数下降可观测'的直接锚点。"""
        client = _local_client(monkeypatch)
        calls = []

        def counting_encode(texts):
            calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]
        monkeypatch.setattr("llm.local_embeddings.get_local_embedder",
                            lambda: SimpleNamespace(
                                encode=counting_encode,
                                encode_queries=lambda ts: counting_encode(ts)))
        v1 = client.embeddings(["员工表"], stage="s1")
        v2 = client.embeddings(["员工表"], stage="s2")
        assert v1 == v2 == [[0.1, 0.2]]
        assert len(calls) == 1, f"第二次应命中缓存零出站,实际出站 {len(calls)}"
        # 命中可观测:第二次日志带 cache_hit
        log2 = [c for c in client._conversation_store.calls
                if c["request_data"].get("stage") == "s2"][-1]
        assert log2["request_data"].get("cache_hit") is True

    def test_different_mode_no_cross_hit(self, monkeypatch):
        """mode 不同不串味:query 与 document 各自出站。"""
        client = _local_client(monkeypatch)
        seen = []

        def rec(texts):
            seen.append(list(texts))
            return [[0.3] for _ in texts]
        monkeypatch.setattr("llm.local_embeddings.get_local_embedder",
                            lambda: SimpleNamespace(
                                encode=rec, encode_queries=lambda ts: rec(ts)))
        client.embeddings(["q"], mode="document", stage="a")
        client.embeddings(["q"], mode="query", stage="b")
        assert len(seen) == 2

    def test_ttl_expiry(self, monkeypatch):
        """TTL 过期后重新出站(monkeypatch time)。"""
        import llm.embed_cache as ec
        client = _local_client(monkeypatch)
        monkeypatch.setattr("llm.local_embeddings.get_local_embedder",
                            lambda: SimpleNamespace(
                                encode=lambda ts: [[0.4] for _ in ts],
                                encode_queries=lambda ts: [[0.4] for _ in ts]))
        client.embeddings(["x"], stage="a")
        # 注:计划原文 lambda: ec.time.monotonic() + 90000 自引用递归
        # (ec.time 即全局 time 模块,setattr 后 lambda 调到自己)——改为
        # patch 前绑定原函数,语义不变(原值+90000 > TTL 86400)
        _orig = ec.time.monotonic
        monkeypatch.setattr(ec.time, "monotonic", lambda: _orig() + 90000)
        client.embeddings(["x"], stage="b")
        # 过期后出站——第二次日志 cache_hit 非 True
        log2 = [c for c in client._conversation_store.calls
                if c["request_data"].get("stage") == "b"][-1]
        assert log2["request_data"].get("cache_hit") is not True

    def test_lru_eviction(self):
        """超上限淘汰最旧(容量 2,放 3 条,第 1 条被逐)。"""
        from llm.embed_cache import EmbedCache
        c = EmbedCache(max_entries=2, ttl_seconds=60)
        c.put("a", [[1]])
        c.put("b", [[2]])
        c.put("c", [[3]])
        assert c.get("a") is None and c.get("c") == [[3]]
