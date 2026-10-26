"""异构外部 MCP fake server(供 tools/mcp-e2e/run_e2e.py 使用)。

搭法复用 backend/tests/test_mcp_integration.py 的实测经验:
- low-level Server + StreamableHTTPSessionManager / SseServerTransport,
  不经本平台网关代码,模拟真实异构接入方;
- **Route endpoint 必须用类实例(原生 ASGI)**——函数会被 Starlette 包成
  request handler,manager.handle_request 收到的 receive/send 变 None,
  直接 NoneType 报错(一期实测教训,勿改回函数);
- uvicorn 线程 + 随机端口(start_server)。

三个 server(与注册 code 一一对应):
- report  "报表数据" server: query_orders / query_gmv 返回 tabular 数据
  ``{"columns": [...], "rows": [[...], ...]}``(execute_mcp_tool 的首选
  形状),数据支撑"各城市订单量对比"类问题(city + 数值两列、6 行中文);
- sse: SSE 传输探针(sse_ping),验证 sdk client 的 /sse 传输协商分支;
- auth: X-Api-Key 鉴权(streamable),验证注册表 headers 传递。
"""
import json
import threading
import time
from contextlib import asynccontextmanager

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

# ── 报表数据(支撑"各城市订单量对比"类问题) ────────────────────

ORDERS = [
    ("北京", 3215), ("上海", 4872), ("广州", 2964),
    ("深圳", 3690), ("杭州", 2158), ("成都", 1903),
]
GMV_WAN = [
    ("北京", 982.4), ("上海", 1537.8), ("广州", 761.2),
    ("深圳", 1043.6), ("杭州", 588.9), ("成都", 502.3),
]

REPORT_AUTH_KEY = "sk-e2e-auth"   # auth server 的 X-Api-Key


def _tabular(columns: list, rows: list) -> tuple:
    """构造 (content, structuredContent) 双通道返回——text JSON 兜底 +
    structuredContent 优先(execute_mcp_tool 的首选形状)。"""
    payload = {"columns": columns, "rows": [list(r) for r in rows]}
    return ([types.TextContent(type="text",
                              text=json.dumps(payload, ensure_ascii=False))],
            payload)


def build_report_server() -> Starlette:
    """"报表数据" server: streamable HTTP, 2 个 tabular 工具。"""
    server = Server("e2e-report")

    @server.list_tools()
    async def _list():
        return [
            types.Tool(
                name="query_orders",
                description="查询各城市订单量(可按城市筛选,可限制行数)",
                inputSchema={"type": "object", "properties": {
                    "city": {"type": "string",
                             "description": "城市名(可选,不传=全部城市)"},
                    "limit": {"type": "integer",
                              "description": "返回行数上限(可选,默认全部)"},
                }}),
            types.Tool(
                name="query_gmv",
                description="查询各城市 GMV 成交额(万元,可按城市筛选)",
                inputSchema={"type": "object", "properties": {
                    "city": {"type": "string",
                             "description": "城市名(可选,不传=全部城市)"},
                }}),
        ]

    @server.call_tool()
    async def _call(name: str, args: dict):
        args = args or {}
        if name == "query_orders":
            rows = ORDERS
            if args.get("city"):
                rows = [r for r in rows if r[0] == args["city"]]
            if isinstance(args.get("limit"), int) and args["limit"] > 0:
                rows = rows[:args["limit"]]
            return _tabular(["city", "order_count"], rows)
        if name == "query_gmv":
            rows = GMV_WAN
            if args.get("city"):
                rows = [r for r in rows if r[0] == args["city"]]
            return _tabular(["city", "gmv_wan"], rows)
        raise ValueError(f"tool not found: {name}")

    return _streamable_app(server)


# ── SSE 传输 server ───────────────────────────────────────────

def build_sse_server() -> Starlette:
    server = Server("e2e-sse")
    sse = SseServerTransport("/messages/")

    @server.list_tools()
    async def _list():
        return [types.Tool(name="sse_ping", description="SSE 传输探针",
                           inputSchema={"type": "object", "properties": {}})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        return [types.TextContent(type="text", text="pong-over-sse")]

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive,
                                   request._send) as streams:
            await server.run(streams[0], streams[1],
                             server.create_initialization_options())
        return Response()

    return Starlette(routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=sse.handle_post_message),
    ])


# ── 计量业务平台(南京院客服 Demo: 检测进度查询) ────────────────

# 演示主体: 模拟业务平台侧的"当前登录主体"(归属校验用,演示话术要
# 如实说明这是模拟,不代表真实用户体系)
METROLOGY_CURRENT_OWNER = "南京精密电子有限公司"

METROLOGY_ORDERS = {
    "BJ2026": {
        "owner": METROLOGY_CURRENT_OWNER,
        "instrument": "数字压力计(0~2.5MPa)",
        "status": "检测中",
        "progress": "3/5",
        "current_step": "压力示值误差检定",
        "eta": "2026-10-30",
    },
    "BJ2027": {
        "owner": METROLOGY_CURRENT_OWNER,
        "instrument": "精密压力表(0~1.6MPa)",
        "status": "已完成",
        "report_no": "RPT-2026-0888",
        "finished_at": "2026-10-22",
        "valid_until": "2027-10-21",
    },
    # 拒权样例: 单号存在但属于其他主体 → forbidden(与 not_found 区分)
    "BJ8888": {
        "owner": "南京宏远机械制造有限公司",
        "instrument": "扭矩扳手",
    },
}
# 故障演示样例: 该单号触发 60s 睡眠,超过客户端 30s 超时 → 如实报错
METROLOGY_TIMEOUT_ORDER = "BJ0000"
METROLOGY_TIMEOUT_SECONDS = 60


def _envelope(payload: dict) -> tuple:
    """业务信封双通道返回(结构化信封是 Demo 受控执行的前置约定)。"""
    return ([types.TextContent(type="text",
                              text=json.dumps(payload, ensure_ascii=False))],
            payload)


def build_metrology_server() -> Starlette:
    """"计量业务平台" server: streamable HTTP, query_progress 工具。

    返回业务信封 {status, data|message}: ok / forbidden(单存在但不属于
    当前主体) / not_found(单不存在)——受控执行方先判信封再归一,
    forbidden 不再被当成成功数据行。
    """
    server = Server("e2e-metrology")

    @server.list_tools()
    async def _list():
        return [types.Tool(
            name="query_progress",
            description="查询委托单检测进度(归属校验: 非本人单据返回 forbidden)",
            inputSchema={"type": "object",
                         "properties": {"order_no": {
                             "type": "string",
                             "description": "委托单号(BJ 开头,如 BJ2026)"}},
                         "required": ["order_no"]})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        args = args or {}
        if name != "query_progress":
            raise ValueError(f"tool not found: {name}")
        order_no = str(args.get("order_no") or "").strip().upper()
        if order_no == METROLOGY_TIMEOUT_ORDER:
            # 故障演示: 睡过客户端 read_timeout → 调用方超时如实报错;
            # 万一客户端 read_timeout 更长真等到了, 也只回 unavailable
            # 业务信封,绝不回 ok+空 data 冒充成功
            time.sleep(METROLOGY_TIMEOUT_SECONDS)
            return _envelope({"status": "unavailable",
                              "message": "业务平台处理超时(模拟)"})
        rec = METROLOGY_ORDERS.get(order_no)
        if rec is None:
            return _envelope({"status": "not_found",
                              "message": f"未找到委托单 {order_no}"})
        if rec["owner"] != METROLOGY_CURRENT_OWNER:
            return _envelope({"status": "forbidden",
                              "message": "该委托单不属于当前主体,无权查询"})
        return _envelope({
            "status": "ok",
            "data": {"order_no": order_no, **rec,
                     "queried_at": time.strftime("%Y-%m-%d %H:%M")},
        })

    return _streamable_app(server)


# ── X-Api-Key 鉴权 server(streamable) ────────────────────────

def build_auth_server() -> Starlette:
    server = Server("e2e-auth")

    @server.list_tools()
    async def _list():
        return [types.Tool(
            name="secret_echo", description="需鉴权的回声工具",
            inputSchema={"type": "object",
                         "properties": {"msg": {"type": "string"}},
                         "required": ["msg"]})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        return [types.TextContent(type="text", text=json.dumps(
            {"authorized": True, "msg": (args or {}).get("msg", "")},
            ensure_ascii=False))]

    inner = _streamable_app(server)

    class KeyGate:
        """ASGI 鉴权包装:错/缺 X-Api-Key → 401(验证注册表 headers 传递)。"""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode(): v.decode()
                           for k, v in scope.get("headers") or []}
                if headers.get("x-api-key") != REPORT_AUTH_KEY:
                    resp = JSONResponse({"detail": "unauthorized"},
                                        status_code=401)
                    await resp(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    class Combined:
        def __init__(self):
            self.inner = inner
            self.gate = KeyGate(inner)

        async def __call__(self, scope, receive, send):
            await self.gate(scope, receive, send)

    return Combined()


# ── 公共设施 ──────────────────────────────────────────────────

def _streamable_app(server: Server) -> Starlette:
    """low-level Server → 无状态 streamable HTTP Starlette 应用。

    Route endpoint 用 _Entry 类实例(原生 ASGI)——见模块 docstring 的
    一期实测教训;Mount 兜底带尾斜杠/子路径形式。
    """
    manager = StreamableHTTPSessionManager(
        app=server, event_store=None, json_response=True, stateless=True)

    class _Entry:
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


def start_server(app, host: str = "127.0.0.1") -> tuple:
    """uvicorn 线程启动(随机端口),返回 (port, uvicorn.Server)。

    停止:server.should_exit = True(错误路径步骤用它模拟 server 宕机)。
    """
    srv = uvicorn.Server(uvicorn.Config(
        app, host=host, port=0, log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    if not srv.started:
        raise RuntimeError("fake server uvicorn 未就绪")
    port = srv.servers[0].sockets[0].getsockname()[1]
    return port, srv
