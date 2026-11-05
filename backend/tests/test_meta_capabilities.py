"""Reusable capabilities do not widen a window routing scope."""
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.meta import list_packs


def request_for(configs, names, tools):
    state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda name: tools.get(name)),
        pack_configs=configs,
        pack_state=SimpleNamespace(enabled_names=lambda: list(configs)),
    )
    return Request({"type": "http", "method": "GET", "path": "/api/meta/packs",
                    "query_string": f"packs={names}".encode(), "headers": [],
                    "app": SimpleNamespace(state=state)})


@pytest.mark.asyncio
async def test_consumed_capability_visible_without_exposing_provider_route():
    configs = {
        "search_provider": {"capabilities": {"live_search": {"tool": "search"}}},
        "assistant": {"uses_capabilities": ["live_search"]},
    }
    result = await list_packs(request_for(configs, "assistant", {"search": object()}))
    assert [item["name"] for item in result] == ["assistant"]
    assert result[0]["capabilities"] == ["live_search"]


@pytest.mark.asyncio
async def test_unloaded_capability_is_not_advertised():
    configs = {"assistant": {"uses_capabilities": ["live_search"]}}
    result = await list_packs(request_for(configs, "assistant", {}))
    assert result[0]["capabilities"] == []


@pytest.mark.asyncio
async def test_provider_requires_loaded_tool():
    configs = {
        "search_provider": {"capabilities": {"live_search": {"tool": "search"}}},
        "assistant": {"uses_capabilities": ["live_search"]},
    }
    result = await list_packs(request_for(configs, "assistant", {}))
    assert result[0]["capabilities"] == []
