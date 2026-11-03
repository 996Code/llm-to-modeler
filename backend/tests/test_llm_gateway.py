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
        # 保留 query/document 独立入口,固定 bge-m3 查询直接编码原文。
        _StubEmbedder.last_texts = list(texts)
        return [[0.9, 0.9] for _ in texts]


def _local_client(monkeypatch):
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

    def test_query_mode_never_adds_prefix(self, monkeypatch):
        """mode=query 保留独立编码入口,旧前缀开关不影响 bge-m3。"""
        client = _local_client(monkeypatch)
        out = client.embeddings(["员工表"], stage="x", mode="query")
        assert out == [[0.9, 0.9]]
        assert _StubEmbedder.last_texts == ["员工表"]   # 默认无前缀
        # 旧开关存在时仍由真实 encode_queries 直传(裸实例绕 __init__)
        from llm.local_embeddings import _LocalEmbedder
        emb = object.__new__(_LocalEmbedder)
        seen = {}
        monkeypatch.setattr(_LocalEmbedder, "encode",
                            lambda self, ts: seen.update(texts=ts))
        monkeypatch.setenv("EMBED_QUERY_PREFIX", "1")
        emb.encode_queries(["员工表"])
        assert seen["texts"] == ["员工表"]
        # 开关关(默认):直传
        monkeypatch.delenv("EMBED_QUERY_PREFIX")
        emb.encode_queries(["员工表"])
        assert seen["texts"] == ["员工表"]

    def test_legacy_api_env_still_uses_local_embedding(self, monkeypatch):
        client = _local_client(monkeypatch)
        monkeypatch.setenv("EMBEDDING_BACKEND", "api")
        monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-v3")
        monkeypatch.setattr(client, "_embeddings_api",
                            lambda *a, **kw: pytest.fail("API embedding used"),
                            raising=False)
        assert client.embeddings(["q"], mode="query", stage="x") == [[0.9, 0.9]]
        assert client.embed_model == "bge-m3"
        log = client._conversation_store.calls[-1]
        assert log["endpoint"] == "embeddings:local/bge-m3"
        assert log["request_data"]["mode"] == "query"

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


class TestChatCache:

    @pytest.fixture(autouse=True)
    def _seed_providers(self):
        """端点链只认 DB 供应商:种入与构造器默认同参的一行(链解析
        命中测试 patch 的构造器客户端),进出清不泄漏。"""
        from services import llm_providers as _providers
        from llm.client import LLMConfig
        _providers.reset_runtime_cache_for_tests()
        d = LLMConfig(base_url="http://test/v1", model="test-chat")
        _providers.save_providers({
            "primary": {"base_url": d.base_url, "api_key": d.api_key,
                        "model": d.model}}, updated_by="test")
        yield
        _providers.reset_runtime_cache_for_tests()

    def test_disabled_by_default(self, monkeypatch):
        """LLM_CACHE_ENABLED=0 整体禁用:两次同参 chat 都出站(逃生门)。
        (2026-11 起白名单 stage 默认开,本用例显式关掉验证逃生门。)"""
        monkeypatch.setenv("LLM_CACHE_ENABLED", "0")
        from llm.client import LLMConfig
        client = LLMClient(config=LLMConfig(base_url="http://test/v1", model="test-chat"),
                           conversation_store=_FakeStore())
        n = []

        class FakeChat:
            def create(self, **kw):
                n.append(1)
                # 注:计划原 fixture 缺 choice.finish_reason(chat 成功路径
                # 观测会读),补上让红落在"缓存未实现"而非替身报错
                m = SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content="答", reasoning_content=None))],
                    usage=None)
                return m
        monkeypatch.setattr(type(client.client.chat.completions),
                            "create", lambda self, **kw: FakeChat().create(**kw))
        r1 = client.chat([{"role": "user", "content": "q"}])
        r2 = client.chat([{"role": "user", "content": "q"}])
        assert r1 == r2 == "答" and len(n) == 2

    def test_enabled_hits_cache(self, monkeypatch):
        """白名单 stage:全同 messages+temperature 二次零出站;temperature
        不同不命中。(2026-11 起白名单 stage 默认开,不再需要 env=1。)"""
        from llm.client import LLMConfig
        client = LLMClient(config=LLMConfig(base_url="http://test/v1", model="test-chat"),
                           conversation_store=_FakeStore())
        n = []

        class FakeChat:
            def create(self, **kw):
                n.append(1)
                m = SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content="答", reasoning_content=None))],
                    usage=None)
                return m
        fake = FakeChat()
        monkeypatch.setattr(type(client.client.chat.completions),
                            "create", lambda self, **kw: fake.create(**kw))
        client.chat([{"role": "user", "content": "q"}], temperature=0.1,
                    stage="route_pack")
        client.chat([{"role": "user", "content": "q"}], temperature=0.1,
                    stage="route_pack")
        assert len(n) == 1, "全同参数二次应命中"
        client.chat([{"role": "user", "content": "q"}], temperature=0.9,
                    stage="route_pack")
        assert len(n) == 2, "temperature 不同不得命中"

    def test_non_whitelisted_stage_never_cached(self, monkeypatch):
        """非白名单 stage(大生成)永不缓存——同参两次都出站。"""
        from llm.client import LLMConfig
        client = LLMClient(config=LLMConfig(base_url="http://test/v1", model="test-chat"),
                           conversation_store=_FakeStore())
        n = []

        class FakeChat:
            def create(self, **kw):
                n.append(1)
                m = SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content="答", reasoning_content=None))],
                    usage=None)
                return m
        fake = FakeChat()
        monkeypatch.setattr(type(client.client.chat.completions),
                            "create", lambda self, **kw: fake.create(**kw))
        client.chat([{"role": "user", "content": "q"}], stage="assistant.conclude")
        client.chat([{"role": "user", "content": "q"}], stage="assistant.conclude")
        assert len(n) == 2, "大生成 stage 不得命中缓存"

    def test_chat_json_whitelisted_stage_hits_cache(self, monkeypatch):
        """chat_json 白名单 stage:同参二次零出站(json_object 直连路径)。"""
        from llm.client import LLMConfig
        client = LLMClient(config=LLMConfig(base_url="http://test/v1", model="test-chat"),
                           conversation_store=_FakeStore())
        n = []

        class FakeChat:
            def create(self, **kw):
                n.append(1)
                m = SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"pack": "x"}', reasoning_content=None))],
                    usage=None)
                return m
        fake = FakeChat()
        monkeypatch.setattr(type(client.client.chat.completions),
                            "create", lambda self, **kw: fake.create(**kw))
        r1 = client.chat_json([{"role": "user", "content": "q"}],
                              stage="assistant.select")
        r2 = client.chat_json([{"role": "user", "content": "q"}],
                              stage="assistant.select")
        assert r1 == r2 == {"pack": "x"}
        assert len(n) == 1, "chat_json 白名单 stage 二次应命中"
