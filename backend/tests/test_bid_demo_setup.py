"""Explicit demo setup preserves administrator-owned registrations and config."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_setup():
    path = Path(__file__).resolve().parents[2] / "tools/bid-demo/setup_demo.py"
    spec = importlib.util.spec_from_file_location("bid_demo_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(data, status=200):
    def check():
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
    return SimpleNamespace(json=lambda: copy.deepcopy(data), status_code=status,
                           raise_for_status=check, text=str(data))


@pytest.mark.parametrize("configured", [True, False])
def test_provider_bootstrap_never_imports_environment_credentials(monkeypatch, configured, capsys):
    module = load_setup()

    class ReachedMcpSetup(Exception):
        pass

    class Session:
        def get(self, url):
            if url.endswith("/llm-providers"):
                return response({"configured": configured,
                                 "primary": {"base_url": "http://provider.test/v1"}})
            assert url.endswith("/mcp/servers")
            raise ReachedMcpSetup

        def put(self, *args, **kwargs):
            pytest.fail("Demo must not write model credentials")

    monkeypatch.setattr(module.requests, "Session", Session)
    monkeypatch.setattr(module.sys, "argv", ["setup_demo.py"])
    monkeypatch.setenv("LLM_BASE_URL", "http://private-provider.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret-never-print")
    monkeypatch.setenv("LLM_MODEL", "private-model")
    if configured:
        with pytest.raises(ReachedMcpSetup):
            module.main()
    else:
        assert module.main() == 2
    output = capsys.readouterr().out
    assert "secret-never-print" not in output
    assert "private-provider" not in output


class MemoryApi:
    def __init__(self):
        self.servers = []
        self.datasources = []
        self.content = {"scenarios": {}, "future": {"keep": True}}
        self.writes = []

    def get(self, url):
        if url.endswith("/mcp/servers"):
            return response(self.servers)
        if url.endswith("/datasources"):
            return response({"items": self.datasources})
        if url.endswith("/config"):
            return response({"content": self.content, "factory": False, "revision": 3})
        raise AssertionError(url)

    def post(self, url, json):
        self.writes.append((url, copy.deepcopy(json)))
        if url.endswith("/mcp/servers"):
            row = {**json, "id": "srv", "enabled": True, "status": "connected",
                   "tools_snapshot": [{"name": tool} for tool in load_setup().REQUIRED_TOOLS]}
            self.servers.append(row)
            return response(row)
        if url.endswith("/datasources"):
            row = {**json, "dbType": json["db_type"], "id": "generic-ds"}
            self.datasources.append(row)
            return response(row)
        raise AssertionError(url)

    def put(self, url, json):
        self.writes.append((url, copy.deepcopy(json)))
        assert url.endswith("/config")
        self.content = copy.deepcopy(json["content"])
        return response({"revision": 4})


def test_server_registration_has_description_and_is_idempotent():
    module, api = load_setup(), MemoryApi()
    server = module.ensure_demo_server(api, "http://backend", "http://mcp/mcp")
    assert server["description"].strip()
    assert module.ensure_demo_server(api, "http://backend", "http://mcp/mcp") == server
    assert len(api.writes) == 1


@pytest.mark.parametrize("changes", [
    {"url": "http://other/mcp"}, {"enabled": False}, {"status": "error"},
    {"tools_snapshot": []},
])
def test_server_reuse_rejects_wrong_url_disabled_or_missing_tools(changes):
    module, api = load_setup(), MemoryApi()
    module.ensure_demo_server(api, "http://backend", "http://mcp/mcp")
    api.servers[0].update(changes)
    before = copy.deepcopy(api.servers)
    with pytest.raises(RuntimeError):
        module.ensure_demo_server(api, "http://backend", "http://mcp/mcp")
    assert api.servers == before
    assert len(api.writes) == 1


def test_generic_datasource_allows_future_tools_without_changing_legacy_binding():
    module, api = load_setup(), MemoryApi()
    legacy = {"id": "manual", "name": "计量业务平台", "dbType": "mcp",
              "host": "metrology", "database": "query_progress"}
    api.datasources.append(copy.deepcopy(legacy))
    ds = module.ensure_demo_datasource(api, "http://backend")
    assert ds["host"] == "metrology"
    assert ds["database"] == ""
    assert api.datasources[0] == legacy
    assert module.ensure_demo_datasource(api, "http://backend")["id"] == ds["id"]
    assert len(api.writes) == 1


@pytest.mark.parametrize("changes", [
    {"host": "equipment"}, {"database": "query_progress"},
    {"dbType": "postgresql"}, {"enabled": False},
])
def test_same_name_datasource_is_not_identity(changes):
    module, api = load_setup(), MemoryApi()
    module.ensure_demo_datasource(api, "http://backend")
    api.datasources[0].update(changes)
    with pytest.raises(RuntimeError):
        module.ensure_demo_datasource(api, "http://backend")
    assert len(api.writes) == 1


def test_seed_adds_only_missing_demo_entries_and_explicit_bindings():
    module, api = load_setup(), MemoryApi()
    manual = {"label": "人工", "action": {"kind": "chatbi", "server": "metrology", "tool": "query_progress"}}
    api.content["scenarios"] = {"progress": copy.deepcopy(manual), "custom": {"future": 3}}
    demo = {"progress": {"action": {"kind": "chatbi", "tool": "query_progress"}},
            "instrument": {"action": {"kind": "chatbi", "tool": "query_instrument", "env": "OLD"}},
            "knowledge": {"action": {"kind": "kg", "kb": "强检目录"}}}
    original = copy.deepcopy(demo)
    module.seed_demo_config(api, "http://backend", "generic-ds", demo)
    assert api.content["scenarios"]["progress"] == manual
    assert api.content["scenarios"]["custom"] == {"future": 3}
    assert api.content["future"] == {"keep": True}
    action = api.content["scenarios"]["instrument"]["action"]
    assert action == {"kind": "chatbi", "tool": "query_instrument", "server": "metrology", "datasource_id": "generic-ds"}
    assert demo == original
    module.seed_demo_config(api, "http://backend", "generic-ds", demo)
    assert len(api.writes) == 1


def test_existing_server_tool_override_under_different_key_is_not_duplicated():
    module, api = load_setup(), MemoryApi()
    api.content["scenarios"]["custom"] = {"action": {"kind": "chatbi", "server": "metrology", "tool": "query_progress"}}
    module.seed_demo_config(api, "http://backend", "generic-ds", {
        "progress": {"action": {"kind": "chatbi", "tool": "query_progress"}}})
    assert list(api.content["scenarios"]) == ["custom"]
    assert api.writes == []


def test_explicit_main_loads_real_legacy_seed_and_is_additive_on_rerun(monkeypatch):
    module = load_setup()

    class DemoApi(MemoryApi):
        def get(self, url):
            if url.endswith('/llm-providers'):
                return response({'configured': True})
            if url.endswith('/kbs'):
                return response([{'id': 'catalog', 'name': module.CATALOG_KB},
                                 {'id': 'regulations', 'name': module.REGULATION_KB}])
            if url.endswith('/documents'):
                return response({'items': [{'id': 'doc', 'importStatus': 'succeeded'}]})
            return super().get(url)

        def post(self, url, json=None, files=None):
            if url.endswith('/documents'):
                assert files['files'][1]
                return response({'items': [{'ok': True, 'document': {'id': 'doc'}}]})
            return super().post(url, json)

    api = DemoApi()
    api.content['scenarios']['custom'] = {'future': 'manual'}
    monkeypatch.setattr(module.requests, 'Session', lambda: api)
    monkeypatch.setattr(module.sys, 'argv', ['setup_demo.py', '--base', 'http://backend',
                                           '--server-url', 'http://mcp/mcp'])
    assert module.main() == 0
    assert {'mandatory_check', 'progress', 'instrument_info', 'turnaround', 'qualification'} <= set(api.content['scenarios'])
    assert api.content['scenarios']['custom'] == {'future': 'manual'}
    for cfg in api.content['scenarios'].values():
        if cfg.get('action', {}).get('kind') == 'chatbi':
            assert cfg['action']['server'] == 'metrology'
            assert cfg['action']['datasource_id'] == 'generic-ds'
            assert 'env' not in cfg['action']
    before = copy.deepcopy(api.content)
    assert module.main() == 0
    assert api.content == before
    assert len(api.writes) == 3
