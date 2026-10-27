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
from pathlib import Path

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

# 预约夹具(按日期查当日预约送检单;date 参数走 date 槽位归一链路)
METROLOGY_APPOINTMENTS = {
    "2026-10-26": [
        {"order_no": "BJ2026", "instrument": "数字压力计(0~2.5MPa)",
         "slot": "09:30", "window": "上午", "counter": "3号窗口"},
        {"order_no": "BJ2027", "instrument": "精密压力表(0~1.6MPa)",
         "slot": "14:00", "window": "下午", "counter": "5号窗口"},
        {"order_no": "BJ2028", "instrument": "电子台秤(30kg)",
         "slot": "10:15", "window": "上午", "counter": "3号窗口"},
    ],
    "2026-10-27": [
        {"order_no": "BJ2029", "instrument": "角膜曲率计",
         "slot": "09:00", "window": "上午", "counter": "2号窗口"},
    ],
}

# 证书夹具(按状态查证书;state 参数带 enum 白名单,走 enum 槽位链路)
METROLOGY_CERTIFICATES = [
    {"report_no": "RPT-2026-0888", "order_no": "BJ2027",
     "instrument": "精密压力表(0~1.6MPa)", "state": "有效",
     "issued_at": "2026-10-22", "valid_until": "2027-10-21"},
    {"report_no": "RPT-2026-0901", "order_no": "BJ2028",
     "instrument": "电子台秤(30kg)", "state": "有效",
     "issued_at": "2026-10-25", "valid_until": "2027-10-24"},
    {"report_no": "RPT-2025-1204", "order_no": "BJ2025",
     "instrument": "扭矩扳手", "state": "已过期",
     "issued_at": "2025-10-15", "valid_until": "2026-10-14"},
    {"report_no": "RPT-2026-0712", "order_no": "BJ2024",
     "instrument": "无线温度记录器", "state": "待领取",
     "issued_at": "2026-10-18", "valid_until": "2027-10-17"},
]

# 报告进度夹具(按报告号查;多行返回 + 拒权样例,走通用表格卡链路)
METROLOGY_REPORTS = {
    "RPT-2026-0888": {"owner": METROLOGY_CURRENT_OWNER, "rows": [
        {"report_no": "RPT-2026-0888", "order_no": "BJ2027",
         "instrument": "精密压力表(0~1.6MPa)", "state": "已归档",
         "archived_at": "2026-10-23", "keeper": "档案室B区"},
    ]},
    "RPT-2026-0901": {"owner": METROLOGY_CURRENT_OWNER, "rows": [
        {"report_no": "RPT-2026-0901", "order_no": "BJ2028",
         "instrument": "电子台秤(30kg)", "state": "在检定中",
         "archived_at": "", "keeper": "检测二科"},
    ]},
    # 拒权样例: 报告存在但属于其他主体 → forbidden(通用卡链路的拒权分态)
    "RPT-2025-1204": {"owner": "南京宏远机械制造有限公司", "rows": []},
}

# 工作量统计夹具(自动路由演示用:该工具默认不编排,验证"上新接口
# 零配置自动接入"——注册快照刷新后对话即可用)
METROLOGY_WORKLOAD = [
    {"category": "压力仪表", "orders": 46, "units": 128},
    {"category": "电学仪器", "orders": 38, "units": 96},
    {"category": "长度量具", "orders": 52, "units": 143},
    {"category": "温度仪表", "orders": 21, "units": 47},
]

# 器具价目夹具(AI.xlsx 转换,已清洗: 去导出 HTML 标签/尾逗号,数值转 int):
# 检测费/强检一二级目录(null=不在强检目录)/检校周期/技术规程
METROLOGY_INSTRUMENTS = json.loads(
    (Path(__file__).resolve().parent / "metrology_instruments.json")
    .read_text(encoding="utf-8"))


def _envelope(payload: dict) -> tuple:
    """业务信封双通道返回(结构化信封是 Demo 受控执行的前置约定)。"""
    return ([types.TextContent(type="text",
                              text=json.dumps(payload, ensure_ascii=False))],
            payload)


def build_metrology_server() -> Starlette:
    """"计量业务平台" server: streamable HTTP。

    - query_progress: 委托单检测进度(归属校验),业务信封 ok / forbidden
      (单存在但不属于当前主体) / not_found(单不存在)——受控执行方先判
      信封再归一,forbidden 不再被当成成功数据行;
    - query_instrument: 器具价目(检测费/强检目录/检校周期/技术规程),
      名称精确→包含两级匹配,可叠加型号过滤;多行命中全返回
      (如同名器具不同量程价目不同);
    - query_appointment: 按日期查当日预约送检单(date 槽位链路);
    - query_certificate: 按状态查证书清单(state 带 enum 白名单);
    - query_report: 按报告号查报告进度(拒权分态 + 通用表格卡)。
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
                         "required": ["order_no"]}),
                types.Tool(
                    name="query_instrument",
                    description="查询器具价目信息: 检测费/强检目录归类/"
                                "检校周期/技术规程(名称支持模糊匹配)",
                    inputSchema={"type": "object",
                                 "properties": {
                                     "name": {"type": "string",
                                              "description": "器具名称(必填,"
                                              "先精确后包含匹配,如 压力/电子秤)"},
                                     "model": {"type": "string",
                                               "description": "型号规格过滤"
                                               "(可选,子串匹配)"}},
                                 "required": ["name"]}),
                types.Tool(
                    name="query_appointment",
                    description="查询某日期的预约送检单(当天各时段预约的"
                                "器具与受理窗口)",
                    inputSchema={"type": "object",
                                 "properties": {"date": {
                                     "type": "string",
                                     "description": "查询日期(格式 "
                                     "2026-10-26)"}},
                                 "required": ["date"]}),
                types.Tool(
                    name="query_certificate",
                    description="按状态查询证书清单(有效/已过期/待领取)",
                    inputSchema={"type": "object",
                                 "properties": {"state": {
                                     "type": "string",
                                     "enum": ["有效", "已过期", "待领取"],
                                     "description": "证书状态(必选)"}},
                                 "required": ["state"]}),
                types.Tool(
                    name="query_report",
                    description="按报告号查询报告进度与归档去向(归属校验)",
                    inputSchema={"type": "object",
                                 "properties": {"report_no": {
                                     "type": "string",
                                     "description": "报告号(RPT 开头,如 "
                                     "RPT-2026-0888)"}},
                                 "required": ["report_no"]}),
                types.Tool(
                    name="query_workload",
                    description="统计各器具类别的检测工作量(本月委托单数"
                                "与检测台件数;类别可模糊过滤)",
                    inputSchema={"type": "object",
                                 "properties": {"category": {
                                     "type": "string",
                                     "description": "器具类别(如 压力/电学/"
                                     "长度;答\"全部\"统计所有类别)"}},
                                 "required": []})]

    @server.call_tool()
    async def _call(name: str, args: dict):
        args = args or {}
        if name == "query_instrument":
            qname = str(args.get("name") or "").strip()
            qmodel = str(args.get("model") or "").strip()
            if not qname:
                return _envelope({"status": "invalid_input",
                                  "message": "缺少器具名称(name)"})
            hits = [r for r in METROLOGY_INSTRUMENTS
                    if r["name"] == qname]
            if not hits:  # 精确未中 → 包含匹配(演示口语"查一下压力")
                hits = [r for r in METROLOGY_INSTRUMENTS
                        if qname in r["name"]]
            if qmodel:
                hits = [r for r in hits if qmodel in (r.get("model") or "")]
            if not hits:
                # 型号过滤清空与名称未命中是两态,文案如实区分
                msg = (f"未找到器具「{qname}」型号含「{qmodel}」的价目信息"
                       if qmodel else f"未找到器具「{qname}」的价目信息")
                return _envelope({"status": "not_found", "message": msg})
            return _envelope({"status": "ok", "data": hits})
        if name == "query_appointment":
            date = str(args.get("date") or "").strip()
            if not date:
                return _envelope({"status": "invalid_input",
                                  "message": "缺少查询日期(date)"})
            rows = METROLOGY_APPOINTMENTS.get(date)
            if not rows:
                return _envelope({
                    "status": "not_found",
                    "message": f"{date} 当日没有预约送检单"})
            return _envelope({"status": "ok", "data": rows})
        if name == "query_certificate":
            state = str(args.get("state") or "").strip()
            if not state:
                return _envelope({"status": "invalid_input",
                                  "message": "缺少证书状态(state)"})
            rows = [r for r in METROLOGY_CERTIFICATES if r["state"] == state]
            if not rows:
                return _envelope({
                    "status": "not_found",
                    "message": f"没有状态为「{state}」的证书"})
            return _envelope({"status": "ok", "data": rows})
        if name == "query_report":
            report_no = str(args.get("report_no") or "").strip().upper()
            if not report_no:
                return _envelope({"status": "invalid_input",
                                  "message": "缺少报告号(report_no)"})
            rec = METROLOGY_REPORTS.get(report_no)
            if rec is None:
                return _envelope({"status": "not_found",
                                  "message": f"未找到报告 {report_no}"})
            if rec["owner"] != METROLOGY_CURRENT_OWNER:
                return _envelope({"status": "forbidden",
                                  "message": "该报告不属于当前主体,无权查询"})
            return _envelope({"status": "ok", "data": rec["rows"]})
        if name == "query_workload":
            category = str(args.get("category") or "").strip()
            rows = [r for r in METROLOGY_WORKLOAD
                    if not category or category in r["category"]
                    or category == "全部"]
            if not rows:
                return _envelope({
                    "status": "not_found",
                    "message": f"没有类别含「{category}」的工作量数据"})
            return _envelope({"status": "ok", "data": rows})
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
