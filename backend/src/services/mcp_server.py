"""对外 MCP server——内部 Tool 自动转 MCP 工具,无状态 Streamable HTTP。

【链路】pack config.yaml 的 mcp_tools 声明(fail-closed 默认不暴露)
→ iter_exposed_tools 过滤 registry → tool_to_mcp(name/description/
input_schema 三件套直转)→ call_tool 里构造最小 state+ToolContext 执行
(工具是同步的,asyncio.to_thread 跑;超时 60s)。

工具可选实现 mcp_description(app_state) 钩子,动态丰富对外描述
(如 kb_search 拼知识库清单)——缺省用 tool.description。
"""
import asyncio
import hmac
import json
import logging
from typing import Any, List

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from sdk.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_S = 60.0
_session_manager: StreamableHTTPSessionManager | None = None
# run() 返回的 async context manager 必须持有同一个实例:manager 的 run()
# 有单次守卫(第二次调用直接 RuntimeError),shutdown 复用 startup 时
# 进过的那个 cm 才能正确退出任务组——brief 原稿 shutdown 里再次 run()
# 会触发 "can only be called once per instance"。
_session_cm = None


def iter_exposed_tools(app_state: Any) -> List[Tool]:
    """按各 pack manifest 的 mcp_tools 声明收集要暴露的工具。"""
    registry = getattr(app_state, "registry", None)
    configs = getattr(app_state, "pack_configs", None) or {}
    out: List[Tool] = []
    for cfg in configs.values():
        for name in (cfg or {}).get("mcp_tools") or []:
            tool = registry.get(name) if registry else None
            if tool is not None and tool not in out:
                out.append(tool)
    return out


def tool_to_mcp(tool: Tool, app_state: Any) -> types.Tool:
    desc = tool.description
    enrich = getattr(tool, "mcp_description", None)
    if callable(enrich):
        try:
            desc = enrich(app_state) or desc
        except Exception:
            logger.warning(f"mcp_description 失败,用默认描述: {tool.name}",
                           exc_info=True)
    return types.Tool(name=tool.name, description=desc,
                      inputSchema=tool.input_schema())


def _make_ctx(app_state: Any) -> ToolContext:
    """无会话上下文:conversation/conv_id 为空 → trace 静默跳过(sdk/tool.py:131)。"""
    return ToolContext(
        llm_client=getattr(app_state, "llm_client", None),
        asset_client=getattr(app_state, "asset_client", None),
        conversation=None, emit=lambda *a, **k: None,
        conv_id=None, registry=getattr(app_state, "registry", None),
        session_state=None, user_id="mcp",
    )


def run_tool(app_state: Any, name: str, args: dict) -> dict:
    """同步执行一个内部工具并转 MCP 结果 dict(error_for_llm 如实抛出)。

    fail-closed 暴露契约:只有 pack manifest 声明暴露的工具可调——
    每次调用现算声明集合(不缓存,禁用 pack 后其工具立即不可调),
    未声明的注册表工具(如 chatbi ask_data)即使猜到名字也拒绝。
    """
    if name not in {t.name for t in iter_exposed_tools(app_state)}:
        raise RuntimeError(f"tool not found: {name}")
    registry = getattr(app_state, "registry", None)
    tool = registry.get(name) if registry else None
    if tool is None:
        raise RuntimeError(f"tool not found: {name}")
    state = dict(args or {})
    # SEC: pack_params 是宿主/嵌入链路的身份透传通道,外部 MCP 客户端
    # 不可伪造(kb_search 会从中读 user_roles 做 ACL 判定)——无条件覆盖
    state["pack_params"] = {}
    err = tool.validate_input(state)
    if err:
        raise RuntimeError(f"输入校验失败: {err}")
    result = tool.execute(state, _make_ctx(app_state))  # to_thread 外层包裹
    if isinstance(result, ToolResult) and result.error_for_llm:
        raise RuntimeError(result.error_for_llm)
    return {"reply": result.reply, "summary": result.summary,
            "artifact": result.artifact}


def build_mcp_server(app_state: Any) -> Server:
    server = Server("llm-modeler-mcp")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [tool_to_mcp(t, app_state) for t in iter_exposed_tools(app_state)]

    @server.call_tool()
    async def _call(name: str, args: dict) -> list[types.TextContent]:
        coro = asyncio.to_thread(run_tool, app_state, name, args)
        result = await asyncio.wait_for(coro, timeout=TOOL_TIMEOUT_S)
        return [types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, default=str))]

    return server


def build_mcp_asgi_app(app_state: Any) -> Starlette:
    """无状态 entry:key 校验(fail-closed:未配置 503/错 key 401)后交 session manager。

    挂法(挂到网关统一走 mount_mcp,见其 docstring):子应用内
    Route("/")+Mount("/") 与 ragflow 的 Route("/mcp")+Mount("/mcp") 同构。
    """
    manager_holder: dict = {}

    class Entry:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            from services.platform_settings import (
                get_platform_settings_store, get_mcp_settings)
            key = (request.headers.get("X-Api-Key") or "").strip()
            expected = get_mcp_settings(
                get_platform_settings_store(request))["api_key"]
            if not expected:
                resp = JSONResponse({"detail": "MCP 未启用:管理端未配置 MCP API Key"},
                                    status_code=503)
            elif not hmac.compare_digest(key.encode(), expected.encode()):
                resp = JSONResponse({"detail": "MCP API Key 无效"}, status_code=401)
            else:
                resp = None
            if resp is not None:
                await resp(scope, receive, send)
                return
            await manager_holder["manager"].handle_request(scope, receive, send)

    entry = Entry()
    manager = StreamableHTTPSessionManager(
        app=build_mcp_server(app_state), event_store=None,
        json_response=True, stateless=True)
    manager_holder["manager"] = manager
    setattr(app_state, "_mcp_manager", manager)  # startup_mcp 从这里取
    sub = Starlette(routes=[
        Route("/", endpoint=entry, methods=["GET", "POST", "DELETE"]),
        Mount("/", app=entry),
    ])
    # 子 Starlette 会在 scope 里覆盖 app=self,Entry 内 get_platform_settings_store
    # 走 request.app.state——把网关的 app.state 透传给子应用,读到的才是
    # lifespan 装配好的 platform_settings_store(而不是在子应用上再懒建一个)。
    sub.state = app_state
    sub._mcp_entry = entry  # mount_mcp 给网关挂无尾斜杠精确 Route 用
    return sub


def mount_mcp(app: Any, app_state: Any) -> None:
    """把 MCP 子应用挂上网关(main lifespan / 测试 fixture 共用同一挂法)。

    两条 /api/mcp 专属路径,缺一不可:
    - Mount("/api/mcp"):带尾斜杠/子路径形式(/api/mcp/、/api/mcp/x)。
      Starlette Mount 的匹配正则带尾斜杠(^/api/mcp/.*$),无尾斜杠的
      POST /api/mcp 根本进不了子应用(外层路由器 307 到带斜杠形式,
      官方 client 不跟随重定向),所以还需要——
    - Route("/api/mcp") 精确命中无尾斜杠形式(官方 client 的默认端点拼法)。
      子侧补 Route("") 不可行(Route 构造断言 path 必须以 / 开头)。

    不用 mount("/api")+子 Route("/mcp") 的原因:pack 热切换
    (pack_api_mount.commit_routes)会把 pack 路由整体重排到 routes 列表尾,
    通用 /api 前缀 Mount 会排到 pack 路由之前,遮蔽 /api/packs/*;
    本挂法的两条路径都是 /api/mcp 专属,与任何排序都不冲突。
    """
    sub = build_mcp_asgi_app(app_state)
    app.mount("/api/mcp", sub)
    app.router.routes.append(Route(
        "/api/mcp", endpoint=sub._mcp_entry,
        methods=["GET", "POST", "DELETE"]))


async def startup_mcp(app: Any) -> None:
    """启动 session manager(main lifespan / 测试 fixture 各调一次;幂等)。"""
    global _session_manager, _session_cm
    if _session_manager is not None:
        return
    manager = getattr(app.state, "_mcp_manager", None)
    if manager is None:
        return
    cm = manager.run()
    await cm.__aenter__()
    _session_manager = manager
    _session_cm = cm


async def shutdown_mcp() -> None:
    global _session_manager, _session_cm
    if _session_manager is not None:
        await _session_cm.__aexit__(None, None, None)
        _session_manager = None
        _session_cm = None
