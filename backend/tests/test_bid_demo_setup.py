"""Demo provider bootstrap uses direct saves without contacting a service."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("configured", [True, False])
def test_provider_bootstrap_reaches_mcp_setup(monkeypatch, configured):
    path = Path(__file__).resolve().parents[2] / "tools/bid-demo/setup_demo.py"
    spec = importlib.util.spec_from_file_location("bid_demo_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ReachedMcpSetup(Exception):
        pass

    saved = []

    class Session:
        def get(self, url):
            if url.endswith("/llm-providers"):
                return SimpleNamespace(json=lambda: {
                    "configured": configured,
                    "primary": {"base_url": "http://provider.test/v1"}})
            assert url.endswith("/mcp/servers")
            raise ReachedMcpSetup

        def put(self, url, json):
            assert url.endswith("/llm-providers")
            saved.append(json)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(module.requests, "Session", Session)
    monkeypatch.setattr(module.sys, "argv", [str(path)])
    monkeypatch.setenv("LLM_BASE_URL", "http://provider.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    with pytest.raises(ReachedMcpSetup):
        module.main()
    assert saved == ([] if configured else [{"primary": {
        "base_url": "http://provider.test/v1", "api_key": "test-key",
        "model": "test-model", "enabled": True}}])
