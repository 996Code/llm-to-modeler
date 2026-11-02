"""The embedding capability is fixed, independent of legacy environment values."""
import pytest

from sdk.embedding import (LOCAL_EMBED_DIM, LOCAL_EMBED_MODEL,
                           embed_cache_label, embed_model)


@pytest.mark.parametrize("backend", ["local", "api", "unknown", ""])
def test_embedding_is_fixed_regardless_of_legacy_env(monkeypatch, backend):
    monkeypatch.setenv("EMBEDDING_BACKEND", backend)
    monkeypatch.setenv("LLM_EMBED_MODEL", "someone-tries-to-override")
    assert embed_model() == LOCAL_EMBED_MODEL == "bge-m3"
    assert LOCAL_EMBED_DIM == 1024
    assert embed_cache_label() == "local/bge-m3"


def test_embedding_defaults_need_no_configuration(monkeypatch):
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
    assert embed_model() == "bge-m3"
    assert embed_cache_label() == "local/bge-m3"
