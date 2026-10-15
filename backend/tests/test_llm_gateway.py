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
    """替身 local embedder:记录收到的文本。"""
    last_texts = None

    def encode(self, texts):
        _StubEmbedder.last_texts = list(texts)
        return [[0.1, 0.2] for _ in texts]

    _PREFIX = "为这个句子生成表示以用于检索相关文章："

    def encode_queries(self, texts):
        # 镜像真实 LocalEmbedder.encode_queries:前缀注入在其内部
        _StubEmbedder.last_texts = [self._PREFIX + t for t in texts]
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
        """mode=query 走 encode_queries:本地后端加 bge-m3 检索前缀。"""
        client = _local_client(monkeypatch)
        out = client.embeddings(["员工表"], stage="x", mode="query")
        assert out == [[0.9, 0.9]]
        assert _StubEmbedder.last_texts == [
            "为这个句子生成表示以用于检索相关文章：员工表"]

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
