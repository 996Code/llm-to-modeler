"""MCP 联动集成测试:多形态外部 server × SDK 客户端 × 注册表。

外部 server 全部用官方 mcp SDK low-level 手搭(不经本平台网关代码),
模拟真实异构接入方:

- Server A(streamable HTTP,无鉴权,3 工具多参数形态):
    echo_text   纯文本非 JSON 返回(结果转换 text 兜底)
    calc        int 参数(结果走 structuredContent 优先)
    table_query array/object 参数(text JSON 返回)
    always_fail 工具侧 isError(→ PackInvokeError permanent)
- Server B(SSE 传输):验证 sdk client 的 /sse 传输协商分支
- Server C(X-Api-Key 鉴权):验证注册表 headers 传递
"""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import mcp.types as types
import pytest
import uvicorn
from fastapi import FastAPI
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from sdk.tool import PackInvokeError
from services.platform_settings import PlatformSettingsStore, save_mcp_settings

# mcp SDK 在连接失败路径(如 401/拒连)会留下未关闭的 anyio 内存流,GC 时以
# unraisable 形式冒出——SDK 内部行为,与本平台代码无关,仅本文件内忽略。
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning")


# ── Server A:streamable HTTP,多工具多参数形态 ─────────────────

def _build_server_a() -> Starlette:
    server = Server("srv-a")

    @server.list_tools()
    async def _list(request: types.ListToolsRequest):
        tools = [
            types.Tool(name="echo_text", description="纯文本返回",
                       inputSchema={"type": "object",
                                   "properties": {"who": {"type": "string"}},
                                   "required": ["who"]}),
            types.Tool(name="calc", description="int 参数加法", title="Calculator",
                       outputSchema={"type": "object", "properties": {"result": {"type": "integer"}},
                                     "required": ["result"]},
                       annotations=types.ToolAnnotations(readOnlyHint=True),
                       _meta={"vendor": {"revision": 1}},
                       inputSchema={"type": "object",
                                   "properties": {"a": {"type": "integer"},
                                                 "b": {"type": "integer"}},
                                   "required": ["a", "b"]}),
            types.Tool(name="table_query", description="array/object 参数",
                       inputSchema={"type": "object",
                                   "properties": {
                                       "columns": {"type": "array",
                                                   "items": {"type": "string"}},
                                       "filter": {"type": "object"}},
                                   "required": ["columns"]}),
            types.Tool(name="always_fail", description="永远失败",
                       inputSchema={"type": "object", "properties": {}}),
        ]
        cursor = request.params.cursor if request and request.params else None
        return types.ListToolsResult(tools=tools[:2] if cursor is None else tools[2:],
                                     nextCursor="second-page" if cursor is None else None)

    @server.call_tool()
    async def _call(name: str, args: dict):
        if name == "echo_text":
            return [types.TextContent(type="text", text=f"hello {args['who']}")]
        if name == "calc":
            result = int(args["a"]) + int(args["b"])
            return ([types.TextContent(type="text", text=str(result))],
                    {"result": result})
        if name == "table_query":
            payload = {"columns": args["columns"],
                       "filter": args.get("filter") or {}, "rows": []}
            return [types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False))]
        if name == "always_fail":
            raise ValueError("boom: 业务失败")
        raise ValueError(f"tool not found: {name}")

    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True)

    class _Entry:  # 类实例=原生 ASGI(函数会被 Starlette 包成 request handler)
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def _lsp(app):
        async with manager.run():
            yield

    return Starlette(lifespan=_lsp, routes=[
        Route("/mcp", endpoint=_Entry(), methods=["GET", "POST", "DELETE"]),
        Mount("/mcp", app=manager.handle_request),
    ])


# ── Server B:SSE 传输 ─────────────────────────────────────────

def _build_server_b() -> Starlette:
    server = Server("srv-b")
    sse = SseServerTransport("/messages/")

    @server.list_tools()
    async def _list():
        return [types.Tool(name="sse_ping", description="SSE 传输探针",
                           inputSchema={"type": "object", "properties": {}})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        return [types.TextContent(type="text", text="pong-over-sse")]

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1],
                             server.create_initialization_options())
        return Response()

    return Starlette(routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ])


# ── Server C:X-Api-Key 鉴权(streamable) ───────────────────────

def _build_server_c() -> Starlette:
    server = Server("srv-c")

    @server.list_tools()
    async def _list():
        return [types.Tool(name="secret_echo", description="需鉴权",
                           inputSchema={"type": "object",
                                   "properties": {"msg": {"type": "string"}},
                                   "required": ["msg"]})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        return [types.TextContent(
            type="text", text=json.dumps({"authorized": True,
                                          "msg": args["msg"]}))]

    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True)

    class KeyGate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode(): v.decode()
                           for k, v in scope.get("headers") or []}
                if headers.get("x-api-key") != "sk-srv-c":
                    resp = JSONResponse({"detail": "unauthorized"},
                                        status_code=401)
                    await resp(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    async def _entry(request):
        await manager.handle_request(request.scope, request.receive, request._send)

    class _Entry:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def _lsp(app):
        async with manager.run():
            yield

    inner = Starlette(lifespan=_lsp, routes=[
        Route("/mcp", endpoint=_Entry(), methods=["GET", "POST", "DELETE"]),
        Mount("/mcp", app=manager.handle_request),
    ])
    return _wrap(inner, KeyGate)


def _wrap(inner, gate_cls):
    """把鉴权 ASGI 类包在 inner Starlette 外(Starlette middleware 需要类)。"""
    from starlette.middleware import Middleware
    # 简化:直接返回一个组合 ASGI app
    class Combined:
        def __init__(self):
            self.inner = inner
            self.gate = gate_cls(inner)

        async def __call__(self, scope, receive, send):
            await self.gate(scope, receive, send)

    return Combined()


# ── uvicorn 线程启动器 ─────────────────────────────────────────

def _start(app) -> tuple[int, object]:
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 未就绪"
    port = server.servers[0].sockets[0].getsockname()[1]
    return port, server


@pytest.fixture()
def servers():
    """起三个异构外部 MCP server,产出 (注册表, {code: cfg})。"""
    from services.mcp_registry import McpServerStore
    from sdk import mcp_client

    port_a, srv_a = _start(_build_server_a())
    port_b, srv_b = _start(_build_server_b())
    port_c, srv_c = _start(_build_server_c())
    cfgs = {
        "srv_a": {"url": f"http://127.0.0.1:{port_a}/mcp", "headers": {},
                  "timeout": 10.0, "read_timeout": 20.0},
        "srv_b": {"url": f"http://127.0.0.1:{port_b}/sse", "headers": {},
                  "timeout": 10.0, "read_timeout": 20.0},
        "srv_c": {"url": f"http://127.0.0.1:{port_c}/mcp",
                  "headers": {"X-Api-Key": "sk-srv-c"},
                  "timeout": 10.0, "read_timeout": 20.0},
    }
    mcp_client.register_server_loader(
        lambda code: cfgs.get(code))
    yield cfgs
    for s in (srv_a, srv_b, srv_c):
        s.should_exit = True
    mcp_client.register_server_loader(lambda code: None)


def test_multi_server_registry_and_calls(servers):
    """三 server 注册→快照→按 code 调用全通(传输协商/鉴权/参数形态)。"""
    from services.mcp_registry import McpServerStore
    from sdk import mcp_client

    store = McpServerStore()
    for code, cfg in servers.items():
        row = store.create_server(name=code, code=code, url=cfg["url"],
                                  headers=cfg["headers"],
                                  timeout=cfg["timeout"],
                                  read_timeout=cfg["read_timeout"])
        assert row["status"] == "connected", f"{code} 探测失败"
    assert len(store.get_by_code("srv_a")["tools_snapshot"]) == 4
    calc = next(t for t in store.get_by_code("srv_a")["tools_snapshot"] if t["name"] == "calc")
    assert calc["outputSchema"]["required"] == ["result"]
    assert calc["annotations"]["readOnlyHint"] is True
    assert calc["_meta"] == {"vendor": {"revision": 1}} and calc["title"] == "Calculator"
    assert len(store.get_by_code("srv_b")["tools_snapshot"]) == 1

    # 纯文本非 JSON → content 兜底
    out = mcp_client.call_tool("srv_a", "echo_text", {"who": "mcp"})
    assert out == {"content": ["hello mcp"]}
    # structuredContent 优先
    out = mcp_client.call_tool("srv_a", "calc", {"a": 2, "b": 3})
    assert out == {"result": 5}
    # array/object 参数 + text JSON
    out = mcp_client.call_tool("srv_a", "table_query",
                               {"columns": ["id", "name"],
                                "filter": {"city": "上海"}})
    assert out["columns"] == ["id", "name"] and out["filter"]["city"] == "上海"
    # SSE 传输协商
    out = mcp_client.call_tool("srv_b", "sse_ping", {})
    assert out == {"content": ["pong-over-sse"]}
    # 鉴权 headers 由注册表配置传递
    out = mcp_client.call_tool("srv_c", "secret_echo", {"msg": "hi"})
    assert out["authorized"] is True and out["msg"] == "hi"


def test_tool_error_is_permanent(servers):
    """工具侧业务失败(isError)→ PackInvokeError permanent,不静默。"""
    from sdk import mcp_client
    with pytest.raises(PackInvokeError) as ei:
        mcp_client.call_tool("srv_a", "always_fail", {})
    assert ei.value.error_type == "permanent"
    assert "boom" in str(ei.value)


def test_wrong_header_unauthorized(servers):
    """Server C 错 key → 连接层 401 → credential,不得自动重试。"""
    from sdk import mcp_client
    mcp_client.register_server_loader(lambda code: {
        "url": servers["srv_c"]["url"], "headers": {"X-Api-Key": "wrong"},
        "timeout": 5.0, "read_timeout": 10.0} if code == "bad_key" else None)
    with pytest.raises(PackInvokeError) as ei:
        mcp_client.call_tool("bad_key", "secret_echo", {"msg": "x"})
    assert ei.value.error_type == "credential"
