"""MCP 接入 key(订阅系统模式):GET 回显明文 + refresh-key 生成/轮换。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from api.admin import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_echoes_key():
    from services.platform_settings import PlatformSettingsStore, save_mcp_settings
    save_mcp_settings(PlatformSettingsStore(), {"api_key": "sk-echo"})
    r = _client().get("/api/admin/settings/mcp")
    assert r.status_code == 200
    assert r.json() == {"configured": True, "apiKey": "sk-echo"}


def test_get_unconfigured():
    from services.platform_settings import PlatformSettingsStore, save_mcp_settings
    save_mcp_settings(PlatformSettingsStore(), {"api_key": ""})
    r = _client().get("/api/admin/settings/mcp")
    assert r.json() == {"configured": False, "apiKey": ""}


def test_refresh_rotates():
    from services.platform_settings import (
        PlatformSettingsStore, get_mcp_settings, save_mcp_settings)
    save_mcp_settings(PlatformSettingsStore(), {"api_key": "sk-old"})
    r = _client().post("/api/admin/settings/mcp/refresh-key")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    new_key = body["apiKey"]
    assert new_key and new_key != "sk-old"
    assert get_mcp_settings(PlatformSettingsStore())["api_key"] == new_key
