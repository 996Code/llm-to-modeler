"""sdk.mcp_client:按 code 取配置→短连接调用→结果转 dict;失败抛 ToolError(retryable)。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
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
def fake_external():
    """假外部 MCP server:真 uvicorn 线程(随机端口)。

    走真网络而非 ASGITransport——sdk client 每次调用 asyncio.run 新建
    loop,进程内 ASGI 会造成 manager 与请求跨 loop 断链;uvicorn 自带
    独立 loop + lifespan(startup_mcp 在其内启动),天然隔离。

    挂法用生产同款 mount_mcp(Task 2 实测:单 Mount 不匹配无尾斜杠的
    /api/mcp,官方 client 不跟随 307,必须 Mount+精确 Route 双路径)。
    """
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
        await mcp_server.shutdown_mcp()

    ext = FastAPI(lifespan=_lsp)
    ext.state.platform_settings_store = PlatformSettingsStore()
    ext.state.registry = ToolRegistry()
    ext.state.registry.register(EchoTool())
    ext.state.pack_configs = {"demo": {"mcp_tools": ["echo"]}}
    ext.state.llm_client = SimpleNamespace()
    ext.state.asset_client = SimpleNamespace()
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
    yield {"url": f"http://127.0.0.1:{port}/api/mcp",
           "headers": {"X-Api-Key": "sk-ext"},
           "timeout": 5.0, "read_timeout": 10.0}
    server.should_exit = True
    t.join(timeout=10)  # 等 lifespan shutdown(shutdown_mcp)跑完再放行下一测试


def test_list_and_call(fake_external):
    from sdk import mcp_client
    tools = mcp_client.list_remote_tools(fake_external)
    assert [t["name"] for t in tools] == ["echo"]
    mcp_client.register_server_loader(
        lambda code: fake_external if code == "demo" else None)
    out = mcp_client.call_tool("demo", "echo", {"text": "hi"})
    assert out["reply"] == "echo:hi"


def test_unknown_code_fails():
    from sdk import mcp_client
    mcp_client.register_server_loader(lambda code: None)
    with pytest.raises(Exception) as ei:
        mcp_client.call_tool("nope", "echo", {})
    assert ei.value.error_type == "permanent"


def test_unreachable_retryable():
    from sdk import mcp_client
    cfg = {"url": "http://127.0.0.1:1/mcp", "headers": {},
           "timeout": 0.5, "read_timeout": 0.5}
    with pytest.raises(Exception) as ei:
        mcp_client.call_remote_tool(cfg, "echo", {})
    assert ei.value.error_type == "retryable"
