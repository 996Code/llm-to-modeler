"""对外 MCP 端点:无状态 streamable HTTP,key 鉴权,Tool→MCP 自动转换。

外部 server 用本进程第二 FastAPI app 模拟(官方 client 的
httpx_client_factory 注入 ASGITransport,零网络)。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sdk.tool import Tool, ToolResult
from sdk.registry import ToolRegistry
from services.platform_settings import save_mcp_settings

MCP_URL = "http://t/api/mcp"


class EchoTool(Tool):
    name = "echo"
    description = "回声测试工具"
    when = "测试"

    def input_schema(self) -> dict:
        return {"type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"]}

    def execute(self, state: dict, ctx) -> ToolResult:
        return ToolResult(reply=f"echo:{state['text']}", summary="ok")


class HiddenTool(Tool):
    """注册表里存在但 pack_configs 未声明暴露的工具(终审 #1 用)。"""

    name = "hidden"
    description = "内部工具,不应被 MCP 调用"
    when = "测试"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, state: dict, ctx) -> ToolResult:
        return ToolResult(reply="leak", summary="ok")


def _make_app(api_key: str = "sk-test") -> FastAPI:
    """装配最小网关 app(不启动 manager——401/503 类测试用不到它)。

    api_key 参数控制落库的 MCP key:空串=未启用(503 分支,终审 #4)。
    """
    from services.platform_settings import PlatformSettingsStore
    save_mcp_settings(PlatformSettingsStore(), {"api_key": api_key})
    app = FastAPI()
    app.state.platform_settings_store = PlatformSettingsStore()
    app.state.registry = ToolRegistry()
    app.state.registry.register(EchoTool())
    app.state.registry.register(HiddenTool())  # 注册但 pack 不声明
    app.state.pack_configs = {"demo": {"mcp_tools": ["echo"]}}
    app.state.llm_client = SimpleNamespace()
    app.state.asset_client = SimpleNamespace()
    from services import mcp_server
    # 生产同款挂法(mount_mcp):Mount("/api/mcp") 接尾斜杠/子路径,
    # 精确 Route("/api/mcp") 接官方 client 默认的无尾斜杠端点。
    mcp_server.mount_mcp(app, app.state)
    return app


def _http_client(app, headers=None):
    """官方 client 的注入点:ASGITransport 零网络直连本进程 app。

    mcp 1.28 起 streamable_http_client 不再收 headers/httpx_client_factory,
    改收预构建 AsyncClient(头在这里给;调用方负责关闭)。
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers=headers or {}, timeout=5)


def test_full_roundtrip():
    """事件循环纪律:session manager 的 run() 与请求必须同 loop——
    startup→交互→shutdown 全部包进同一个 asyncio.run。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from services import mcp_server
    app = _make_app()

    async def run():
        await mcp_server.startup_mcp(app)
        hc = _http_client(app, headers={"X-Api-Key": "sk-test"})
        try:
            async with streamable_http_client(MCP_URL, http_client=hc) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    assert [t.name for t in tools.tools] == ["echo"]
                    assert "回声" in tools.tools[0].description
                    res = await s.call_tool("echo", {"text": "hi"})
                    assert not res.isError
        finally:
            await hc.aclose()
            await mcp_server.shutdown_mcp()

    asyncio.run(run())


def _init_body():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}


def _post(url, headers=None, api_key: str = "sk-test"):
    """401/503 类测试的直发工具(不经 session manager,无事件循环纪律约束;
    ASGITransport 是 async-only 传输,同步 httpx.post 挂不了它)。"""
    async def _do():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_make_app(api_key=api_key)),
                base_url="http://t") as c:
            return await c.post(url, headers=headers or {}, json=_init_body())
    return asyncio.run(_do())


def test_wrong_key_401():
    r = _post(MCP_URL, headers={"X-Api-Key": "bad"})
    assert r.status_code == 401


def test_no_key_401():
    r = _post(MCP_URL)
    assert r.status_code == 401


# ── 终审 #4:未配置 key → 一律 503(spec fail-closed,此前无落盘测试)──


def test_empty_key_503():
    """管理端清空 key 后:任何请求(含 initialize)→ 503,不是 401/500。"""
    r = _post(MCP_URL, api_key="")            # 无 key 头
    assert r.status_code == 503
    assert "未启用" in r.json()["detail"]
    r2 = _post(MCP_URL, headers={"X-Api-Key": "whatever"}, api_key="")
    assert r2.status_code == 503              # 带 key 头也 503:未配置一律拒绝


# ── 终审 #2:非 ASCII key → 401,不是 500(TypeError)──────────────


def test_non_ascii_key_401_not_500():
    """key 头含非 ASCII(latin-1 可解字节):compare_digest 对 str 抛
    TypeError → 500;应按字节比较 → 401。"""
    async def _do():
        # httpx 原始字节头:UTF-8 "密钥" → Starlette 按 latin-1 解出非 ASCII str
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_make_app()),
                base_url="http://t") as c:
            return await c.post(
                MCP_URL, headers=[(b"x-api-key", "密钥".encode())],
                json=_init_body())
    r = asyncio.run(_do())
    assert r.status_code == 401


# ── 终审 #1:未声明暴露的工具不可调用(fail-closed 暴露契约)──────


def test_hidden_tool_not_callable():
    """注册表里存在但 pack_configs 未声明(hidden)→ 调用必须失败;
    声明了的 echo 仍可调(证明闸门只挡未声明,不误伤正常链路)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from services import mcp_server
    app = _make_app()

    async def run():
        await mcp_server.startup_mcp(app)
        hc = _http_client(app, headers={"X-Api-Key": "sk-test"})
        try:
            async with streamable_http_client(MCP_URL, http_client=hc) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    assert [t.name for t in tools.tools] == ["echo"]  # hidden 不在声明
                    res = await s.call_tool("hidden", {})
                    assert res.isError                     # 未声明 → isError
                    assert "tool not found: hidden" in res.content[0].text
                    res2 = await s.call_tool("echo", {"text": "ok"})
                    assert not res2.isError                # 声明了的照常可调
        finally:
            await hc.aclose()
            await mcp_server.shutdown_mcp()

    asyncio.run(run())


# ── 终审 #3:auth 白名单 /api/mcp 前缀收紧(不再误吞 /api/mcpfoo)──


class TestAuthWhitelistMcpPrefix:
    def _app(self, monkeypatch, token="t"):
        monkeypatch.setenv("ADMIN_TOKEN", token)
        from api.auth import AuthMiddleware
        app = FastAPI()

        @app.get("/api/mcpfoo")
        @app.get("/api/mcp")
        @app.get("/api/mcp/x")
        async def _ok():
            return {"ok": True}

        app.add_middleware(AuthMiddleware)
        return app

    def test_mcpfoo_not_whitelisted(self, monkeypatch):
        """/api/mcpfoo 不再被前缀误吞:无 token → 中间件 401。"""
        with TestClient(self._app(monkeypatch)) as c:
            r = c.get("/api/mcpfoo")
            assert r.status_code == 401
            assert r.json()["error"] == "invalid_token"

    def test_mcp_paths_still_whitelisted(self, monkeypatch):
        """真 MCP 端点(无尾斜杠/带尾斜杠/子路径)仍放行。"""
        with TestClient(self._app(monkeypatch)) as c:
            assert c.get("/api/mcp").status_code == 200
            assert c.get("/api/mcp/").status_code == 200
            assert c.get("/api/mcp/x").status_code == 200
