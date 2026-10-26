"""Read-only metrology diagnostics: real store contracts and safe failures."""
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.chatbi import datasources, runtime as bi_runtime
from domains.knowledge_graph import runtime as kg_runtime
from domains.knowledge_graph.store import KGStore
from domains.metrology_assistant import api
from sdk.relational_store import PackRelationalDB
from services.mcp_registry import McpServerStore


URL = "/api/packs/metrology_assistant/test/env"
SECRET = "private-dependency-credential"
DEPENDENCY_ERROR = (
    f"url=https://user:{SECRET}@internal/mcp "
    f"headers={{'Authorization': '{SECRET}'}} password={SECRET}"
)


def _unavailable(*args, **kwargs):
    raise RuntimeError(DEPENDENCY_ERROR)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv(api.cfg.PROGRESS_DATASOURCE_ENV, raising=False)
    kg = KGStore()
    db = PackRelationalDB("chatbi")
    datasources.init_store(db)
    mcp = McpServerStore()
    monkeypatch.setattr(kg_runtime, "get_kg_store", lambda state: kg)
    monkeypatch.setattr(bi_runtime, "get_pack_db", lambda: db)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/packs/metrology_assistant")
    app.state.mcp_server_store = mcp
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, kg=kg, db=db, mcp=mcp)


def _seed_kbs(env):
    kbs = []
    for name in (api.cfg.MANDATORY_CHECK_KB, api.cfg.REGULATION_KB):
        kb = env.kg.create_kb(name)
        for index, status in enumerate(("succeeded", "failed", "importing", "uploaded")):
            doc = env.kg.create_document(
                kb["id"], f"{index}.pdf", "application/pdf", 1,
                "/not-read.pdf", f"hash-{index}")
            env.kg.update_document(doc["id"], import_status=status,
                                   error=DEPENDENCY_ERROR if status == "failed" else "")
        kbs.append(kb)
    return kbs


def _seed_progress(env, monkeypatch):
    ds = datasources.create_datasource(
        env.db, "progress", "mcp", "metrology", 0,
        "query_progress", "private-user", "")
    with env.db.connect() as conn:
        conn.execute(
            "UPDATE chatbi_data_sources SET encrypted_password = ? WHERE id = ?",
            (SECRET, ds.id))
    monkeypatch.setenv(api.cfg.PROGRESS_DATASOURCE_ENV, f"  {ds.id}  ")
    monkeypatch.setattr(env.mcp, "apply_probe", lambda sid: None)
    env.mcp.create_server(
        "metrology", "metrology", f"https://user:{SECRET}@internal/mcp",
        {"Authorization": SECRET})
    with env.mcp.db.connect() as conn:
        conn.execute(
            "UPDATE mcp_servers SET status = 'connected', tools_json = ? "
            "WHERE code = 'metrology'",
            ('[{"name":"query_progress","description":"private-dependency-credential",'
             '"inputSchema":{"type":"object"}}]',))
    return ds


def _body(env):
    response = env.client.get(URL)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    for field in ('"headers"', '"url"', '"password"', '"encrypted_password"'):
        assert field not in response.text
    return response.json()


def test_env_real_store_fields_and_allowlisted_response(env, monkeypatch):
    _seed_kbs(env)
    _seed_progress(env, monkeypatch)
    body = _body(env)
    assert body["errors"] == []
    assert body["kbMapping"] == {
        "purposes": list(api.cfg.STRONG_INSPECTION_PURPOSES),
        "mandatoryCheckKb": api.cfg.MANDATORY_CHECK_KB,
        "regulationKb": api.cfg.REGULATION_KB,
    }
    assert body["kbs"] == [
        {"name": name, "exists": True, "docs": 4, "imported": 1, "status": "ok"}
        for name in (api.cfg.MANDATORY_CHECK_KB, api.cfg.REGULATION_KB)
    ]
    assert body["progress"] == {
        "env_configured": True,
        "datasource": {"name": "progress", "active": True, "dbType": "mcp",
                       "host": "metrology", "tool": "query_progress"},
        "datasourceStatus": "ok",
        "mcp": {"code": "metrology", "status": "connected", "enabled": True,
                "tools": ["query_progress"]},
        "mcpStatus": "ok",
    }
    fixtures = {item["order"]: item["desc"] for item in body["orderFixtures"]}
    assert set(fixtures) == {o.label for o in api.cfg.DEMO_ORDER_OPTIONS} | {
        "BJ8888", "BJ9000", "BJ0000"}
    assert all(fixtures.values())


def test_env_missing_resources_are_not_dependency_errors(env):
    body = _body(env)
    assert body["errors"] == []
    assert all(kb["status"] == "missing" and kb["exists"] is False
               and kb["docs"] == kb["imported"] == 0 for kb in body["kbs"])
    assert body["progress"] == {
        "env_configured": False, "datasource": None,
        "datasourceStatus": "not_configured", "mcp": None, "mcpStatus": "missing"}


def test_env_configured_datasource_missing(env, monkeypatch):
    monkeypatch.setenv(api.cfg.PROGRESS_DATASOURCE_ENV, "unknown-id")
    body = _body(env)
    assert body["progress"]["env_configured"] is True
    assert body["progress"]["datasource"] is None
    assert body["progress"]["datasourceStatus"] == "missing"
    assert body["errors"] == []


@pytest.mark.parametrize("status,enabled", [("pending", True), ("connected", False)])
def test_env_preserves_mcp_probe_and_enabled_states(env, monkeypatch, status, enabled):
    _seed_progress(env, monkeypatch)
    with env.mcp.db.connect() as conn:
        conn.execute("UPDATE mcp_servers SET status = ?, enabled = ?, tools_json = '[]'",
                     (status, int(enabled)))
    body = _body(env)
    assert body["progress"]["mcp"] == {
        "code": "metrology", "status": status, "enabled": enabled, "tools": []}


def test_env_inactive_datasource_is_reported(env, monkeypatch):
    ds = _seed_progress(env, monkeypatch)
    datasources.update_datasource(env.db, ds.id, is_active=False)
    assert _body(env)["progress"]["datasource"]["active"] is False


def test_env_kg_initialization_failure_keeps_both_names(env, monkeypatch, caplog):
    _seed_progress(env, monkeypatch)
    monkeypatch.setattr(kg_runtime, "get_kg_store", _unavailable)
    with caplog.at_level(logging.ERROR, logger=api.__name__):
        body = _body(env)
    assert body["kbs"] == [
        {"name": name, "exists": False, "docs": 0, "imported": 0, "status": "error"}
        for name in (api.cfg.MANDATORY_CHECK_KB, api.cfg.REGULATION_KB)
    ]
    assert body["errors"] == ["知识库服务不可用，请查看服务日志"]
    assert body["progress"]["datasource"] is not None
    assert body["progress"]["mcp"] is not None
    assert any(r.exc_info for r in caplog.records if r.name == api.__name__)


@pytest.mark.parametrize("method", ["get_kb_by_name", "list_documents"])
@pytest.mark.parametrize("failed_index", [0, 1])
def test_env_one_kb_failure_does_not_hide_other_kb(
        env, monkeypatch, caplog, method, failed_index):
    kbs = _seed_kbs(env)
    original = getattr(env.kg, method)
    key = "name" if method == "get_kb_by_name" else "id"

    def maybe_fail(value):
        if value == kbs[failed_index][key]:
            _unavailable()
        return original(value)

    monkeypatch.setattr(env.kg, method, maybe_fail)
    with caplog.at_level(logging.ERROR, logger=api.__name__):
        body = _body(env)
    failed, healthy = body["kbs"][failed_index], body["kbs"][1 - failed_index]
    assert failed["name"] == kbs[failed_index]["name"]
    assert failed["status"] == "error"
    assert failed["exists"] is (method == "list_documents")
    assert healthy == {"name": kbs[1 - failed_index]["name"], "exists": True,
                       "docs": 4, "imported": 1, "status": "ok"}
    assert body["errors"] == [f"知识库「{failed['name']}」状态读取失败，请查看服务日志"]
    assert any(r.exc_info for r in caplog.records if r.name == api.__name__)


@pytest.mark.parametrize("dependency", ["datasource", "pack_db", "mcp", "mcp_store"])
def test_env_dependency_errors_are_safe_and_logged(env, monkeypatch, caplog, dependency):
    _seed_kbs(env)
    _seed_progress(env, monkeypatch)
    if dependency == "datasource":
        monkeypatch.setattr(datasources, "get_datasource", _unavailable)
    elif dependency == "pack_db":
        monkeypatch.setattr(bi_runtime, "get_pack_db", _unavailable)
    elif dependency == "mcp":
        monkeypatch.setattr(env.mcp, "get_by_code", _unavailable)
    else:
        del env.app.state.mcp_server_store
    failed = "datasource" if dependency in ("datasource", "pack_db") else "mcp"
    healthy = "mcp" if failed == "datasource" else "datasource"
    with caplog.at_level(logging.ERROR, logger=api.__name__):
        body = _body(env)
    assert body["progress"][failed] is None
    assert body["progress"][failed + "Status"] == "error"
    assert body["progress"][healthy] is not None
    message = "进度数据源状态读取失败" if failed == "datasource" else "MCP 注册状态读取失败"
    assert body["errors"] == [message + "，请查看服务日志"]
    assert all(kb["status"] == "ok" for kb in body["kbs"])
    assert any(r.exc_info for r in caplog.records if r.name == api.__name__)


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"},
                                    {"X-User-Id": "admin"}])
def test_env_rejects_unauthorized_before_reading_dependencies(env, monkeypatch, headers):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")

    def must_not_read(*args):
        pytest.fail("unauthorized request reached diagnostics dependencies")

    monkeypatch.setattr(kg_runtime, "get_kg_store", must_not_read)
    assert env.client.get(URL, headers=headers).status_code == 401


def test_env_accepts_valid_admin_token(env, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    response = env.client.get(URL, headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert "kbMapping" in response.json()
