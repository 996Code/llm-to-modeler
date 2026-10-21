"""MCP 全局单 key:平台设置读写 + 管理端端点(GET 不回显明文)。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.platform_settings import (
    PlatformSettingsStore, get_mcp_settings, save_mcp_settings,
)


def test_roundtrip_and_defaults():
    store = PlatformSettingsStore()
    assert get_mcp_settings(store) == {"api_key": ""}   # 缺省空
    merged, errors = save_mcp_settings(store, {"api_key": "sk-mcp-1"})
    assert errors == [] and merged == {"api_key": "sk-mcp-1"}
    assert get_mcp_settings(store) == {"api_key": "sk-mcp-1"}


def test_reject_non_string():
    _, errors = save_mcp_settings(PlatformSettingsStore(), {"api_key": 123})
    assert errors and "字符串" in errors[0]["message"]


def test_admin_endpoint_no_plaintext_echo():
    from api.admin import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        c.put("/api/admin/settings/mcp", json={"api_key": "sk-secret"})
        r = c.get("/api/admin/settings/mcp")
    assert r.status_code == 200
    assert r.json() == {"configured": True}             # 明文不回显
