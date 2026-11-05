"""The shared search contract is used without cross-pack tool routing."""
from types import SimpleNamespace

import pytest

from domains.web_search import service
from domains.web_search.tools.web_search import WebSearchTool
from sdk.tool import PackInvokeError, ToolContext


def context():
    return ToolContext(llm_client=None, asset_client=None, conversation=None,
                       emit=lambda *args: None, conv_id="web-contract")


@pytest.mark.parametrize("value", [None, False, 0, 1, "true", {}])
def test_only_explicit_true_authorizes_conversation_search(monkeypatch, value):
    from domains.web_search import websearch
    monkeypatch.setattr(websearch, "settings_url", lambda state: "http://search.test")
    result = WebSearchTool(SimpleNamespace()).preflight(
        {"query": "question", "pack_params": {"web_search": {"enabled": value}}},
        context())
    assert result is not None and result.error_for_llm


def test_running_request_stops_when_provider_is_unloaded():
    provider = object()
    tools = {"web_search": provider}
    app = SimpleNamespace(registry=SimpleNamespace(get=tools.get))
    check = service.request_guard(app)
    check()
    tools.clear()
    with pytest.raises(PackInvokeError, match="停用"):
        check()


def test_reenable_does_not_revive_old_request():
    tools = {"web_search": object()}
    app = SimpleNamespace(registry=SimpleNamespace(get=tools.get))
    check = service.request_guard(app)
    tools["web_search"] = object()
    with pytest.raises(PackInvokeError):
        check()


def test_kg_delegates_explicit_web_request_without_kg_access(monkeypatch):
    from domains.knowledge_graph import runtime
    from domains.knowledge_graph.tools.kb_search import KbSearchTool
    from sdk.tool import ToolResult
    expected = ToolResult(summary="web answer", artifact_type="data")
    monkeypatch.setattr(service, "available", lambda app: True)
    monkeypatch.setattr(service, "answer", lambda *args, **kwargs: expected)
    monkeypatch.setattr(runtime, "get_kg_store", lambda app: pytest.fail("must not retrieve KB"))
    state = {"user_input": "帮我联网搜一下新闻", "pack_params": {"web_search": {"enabled": True}}}
    tool = KbSearchTool(SimpleNamespace())
    assert tool.preflight(state, context()) is None
    assert tool.execute(state, context()) is expected
