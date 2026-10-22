# backend/tests/test_mcp_registry.py
"""mcp_servers 注册表:CRUD + 注册即探测(成功 connected/失败 pending 不阻塞)。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sdk.tool import Tool, ToolResult
from sdk.registry import ToolRegistry
from services.platform_settings import PlatformSettingsStore, save_mcp_settings


class EchoTool(Tool):
    name = "echo"
    description = "回声"
    when = "测试"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"]}

    def execute(self, state: dict, ctx) -> ToolResult:
        return ToolResult(reply=f"echo:{state['text']}", summary="ok")


@pytest.fixture()
def env():
    """(TestClient, 外部 server URL):假外部 server 走真 uvicorn 线程
    (Task 4 同款理由——探测的 asyncio.run 与网关 manager 不共 loop)。"""
    import threading
    import time
    from contextlib import asynccontextmanager
    import uvicorn
    save_mcp_settings(PlatformSettingsStore(), {"api_key": "sk-ext"})
    from services import mcp_server

    @asynccontextmanager
    async def _lsp(app):
        await mcp_server.startup_mcp(app)
        yield
        await mcp_server.shutdown_mcp(app)

    ext = FastAPI(lifespan=_lsp)
    ext.state.platform_settings_store = PlatformSettingsStore()
    ext.state.registry = ToolRegistry()
    ext.state.registry.register(EchoTool())
    ext.state.pack_configs = {"demo": {"mcp_tools": ["echo"]}}
    ext.state.llm_client = SimpleNamespace()
    ext.state.asset_client = SimpleNamespace()
    # 挂法用生产同款 mount_mcp(单 Mount 匹配不了无尾斜杠 /api/mcp,
    # 见 services/mcp_server.mount_mcp docstring;与 Task 4 fixture 同款)
    mcp_server.mount_mcp(ext, ext.state)

    server = uvicorn.Server(uvicorn.Config(
        ext, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/api/mcp"

    app = FastAPI()
    from api.admin import router
    from services.mcp_registry import McpServerStore
    from sdk import mcp_client
    app.include_router(router)
    app.state.mcp_server_store = McpServerStore()
    mcp_client.register_server_loader(
        lambda code: app.state.mcp_server_store.get_by_code(code))
    with TestClient(app) as c:
        yield c, url
    mcp_client.register_server_loader(lambda code: None)
    server.should_exit = True
    t.join(timeout=10)  # 等 lifespan shutdown(shutdown_mcp)跑完再放行下一测试


def test_create_probe_and_list(env):
    client, url = env
    r = client.post("/api/admin/mcp/servers", json={
        "name": "外部数据", "code": "ext_data",
        "url": url, "headers": {"X-Api-Key": "sk-ext"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "connected"
    assert [t["name"] for t in body["tools_snapshot"]] == ["echo"]
    assert [s["code"] for s in client.get("/api/admin/mcp/servers").json()] == ["ext_data"]


def test_bad_url_pending(env):
    client, _ = env
    r = client.post("/api/admin/mcp/servers", json={
        "name": "坏的", "code": "bad", "url": "http://127.0.0.1:1/mcp", "headers": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_code_unique(env):
    client, _ = env
    client.post("/api/admin/mcp/servers",
                json={"name": "a", "code": "dup", "url": "http://127.0.0.1:1/mcp", "headers": {}})
    r = client.post("/api/admin/mcp/servers",
                    json={"name": "b", "code": "dup", "url": "http://127.0.0.1:2/mcp", "headers": {}})
    assert r.status_code == 409


def test_refresh_and_delete(env):
    client, url = env
    sid = client.post("/api/admin/mcp/servers", json={
        "name": "外部数据", "code": "ext_data",
        "url": url, "headers": {"X-Api-Key": "sk-ext"}}).json()["id"]
    assert client.post(f"/api/admin/mcp/servers/{sid}/refresh").status_code == 200
    assert client.delete(f"/api/admin/mcp/servers/{sid}").status_code == 200
    assert client.get("/api/admin/mcp/servers").json() == []


# ── 启用/停用:enabled 列(默认 True)+启停端点+调用层 fail-fast ──────


def test_enable_disable_roundtrip(env):
    client, url = env
    sid = client.post("/api/admin/mcp/servers", json={
        "name": "启停测试", "code": "toggle_me", "url": url,
        "headers": {"X-Api-Key": "sk-ext"}}).json()["id"]
    rows = {s["code"]: s for s in client.get("/api/admin/mcp/servers").json()}
    assert rows["toggle_me"]["enabled"] is True        # 新建默认启用

    r = client.post(f"/api/admin/mcp/servers/{sid}/disable")
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.post(f"/api/admin/mcp/servers/{sid}/enable")
    assert r.status_code == 200 and r.json()["enabled"] is True


def test_disabled_server_client_rejects(env):
    """停用的 server:client 调用直接报 permanent(已停用),不发起连接。"""
    from sdk import mcp_client
    from sdk.tool import PackInvokeError
    client, url = env
    sid = client.post("/api/admin/mcp/servers", json={
        "name": "启停调用", "code": "off_srv", "url": url,
        "headers": {"X-Api-Key": "sk-ext"}}).json()["id"]
    client.post(f"/api/admin/mcp/servers/{sid}/disable")
    with pytest.raises(PackInvokeError) as ei:
        mcp_client.call_tool("off_srv", "echo", {})
    assert ei.value.error_type == "permanent"
    assert "停用" in str(ei.value)
