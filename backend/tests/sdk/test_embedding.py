"""sdk/embedding —— 嵌入口径单一事实源。

local 恒 bge-m3(dim 1024 定死,不受 LLM_EMBED_MODEL 影响——用户裁决
"定死的东西不走 env");api 读 LLM_EMBED_MODEL,未配置强校验抛错。
"""
import pytest

from sdk.embedding import (LOCAL_EMBED_DIM, LOCAL_EMBED_MODEL,
                           embed_backend, embed_cache_label, embed_model,
                           embed_model_or_raise)


class TestEmbeddingCanon:

    def test_local_is_pinned_regardless_of_env(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "local")
        monkeypatch.setenv("LLM_EMBED_MODEL", "someone-tries-to-override")
        assert embed_model() == LOCAL_EMBED_MODEL          # env 改不动 local
        assert embed_backend() == "local"
        assert embed_cache_label() == "local/bge-m3"
        assert embed_model_or_raise() == LOCAL_EMBED_MODEL

    def test_default_backend_is_local(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
        monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
        assert embed_backend() == "local"
        assert embed_model() == LOCAL_EMBED_MODEL
        assert LOCAL_EMBED_DIM == 1024                       # 建库预填口径

    def test_api_backend_reads_env(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "api")
        monkeypatch.setenv("LLM_EMBED_MODEL", "text-embedding-v3")
        assert embed_backend() == "api"
        assert embed_model() == "text-embedding-v3"
        assert embed_cache_label() == "text-embedding-v3"   # 切模型键即变

    def test_api_unconfigured_raises(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "api")
        monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
        with pytest.raises(RuntimeError, match="LLM_EMBED_MODEL"):
            embed_model_or_raise()
        # 弱口径版本回退 bge-m3(展示/指纹用途;出站路径用强口径版)
        assert embed_model() == LOCAL_EMBED_MODEL
