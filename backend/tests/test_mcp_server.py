"""对外 MCP 端点:无状态 streamable HTTP,key 鉴权,Tool→MCP 自动转换。

外部 server 用本进程第二 FastAPI app 模拟(官方 client 的
httpx_client_factory 注入 ASGITransport,零网络)。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
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


def _make_app() -> FastAPI:
    """装配最小网关 app(不启动 manager——401 类测试用不到它)。"""
    from services.platform_settings import PlatformSettingsStore
    save_mcp_settings(PlatformSettingsStore(), {"api_key": "sk-test"})
    app = FastAPI()
    app.state.platform_settings_store = PlatformSettingsStore()
    app.state.registry = ToolRegistry()
    app.state.registry.register(EchoTool())
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


def _post(url, headers=None):
    """401 类测试的直发工具(不经 session manager,无事件循环纪律约束;
    ASGITransport 是 async-only 传输,同步 httpx.post 挂不了它)。"""
    async def _do():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_make_app()),
                base_url="http://t") as c:
            return await c.post(url, headers=headers or {}, json=_init_body())
    return asyncio.run(_do())


def test_wrong_key_401():
    r = _post(MCP_URL, headers={"X-Api-Key": "bad"})
    assert r.status_code == 401


def test_no_key_401():
    r = _post(MCP_URL)
    assert r.status_code == 401
