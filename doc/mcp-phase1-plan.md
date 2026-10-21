# MCP 双向接入一期 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对外把内部工具经 `/api/mcp`（无状态 Streamable HTTP + 全局单 key）暴露给外部 AI；对内建 `mcp_servers` 注册表 + `sdk/mcp_client.py` 客户端设施（插件零 MCP 感知）。

**Architecture:** 三块新代码全在平台层：`services/mcp_server.py`（low-level Server + Tool→MCP 转换器 + StreamableHTTPSessionManager 无状态挂载）、`services/mcp_registry.py`（mcp_servers 表 CRUD + 注册即探测）、`sdk/mcp_client.py`（每次调用短连接，经依赖倒置注册 server 装载器）。引擎、pack 路由零改动。

**Tech Stack:** FastAPI + 官方 `mcp` SDK（已在 requirements: `mcp>=1.28,<1.29`，backend/.venv 已装）+ PG（psycopg，Store 每方法新连接）+ Vue3/antd（前端管理页）。

**Spec:** `doc/mcp-integration-design.md`

## Global Constraints

- 不新增任何依赖；只用已装 `mcp` SDK（`mcp.server.lowlevel.Server`、`mcp.server.streamable_http_manager.StreamableHTTPSessionManager(app=, json_response=True, stateless=True)`、`mcp.client.streamable_http.streamablehttp_client(url, headers, timeout, sse_read_timeout, httpx_client_factory=)`）。
- 不做加密/OAuth/SSRF/限流/多租户/key 体系——全局单 key 存 platform_settings。
- fail-closed：未配置 MCP key → `/api/mcp` 一律 503；错误如实返回，不静默降级。
- Store 类照抄 `PlatformSettingsStore` 模式（services/platform_settings.py:87-138：`get_pg_engine` + 每方法新连接 + 写锁 + `CREATE TABLE IF NOT EXISTS`）。
- 管理端路由挂 `api/admin.py`，依赖 `require_admin`（main.py:313 已导出）。
- 测试跑 backend/.venv：`cd backend && .venv/bin/python -m pytest tests/<file> -v`（需 TEST_DATABASE_URL，conftest 自动建库清表）。

---

### Task 1: MCP 全局单 key（平台设置 + 管理端读写端点）

**Files:**
- Modify: `backend/src/services/platform_settings.py`（DISPLAY 块后追加 MCP 块）
- Modify: `backend/src/api/admin.py`（settings/display 端点后追加）
- Test: `backend/tests/test_mcp_settings.py`（新建）

**Interfaces:**
- Produces: `get_mcp_settings(store) -> {"api_key": str}`（缺省 `{"api_key": ""}`）；`save_mcp_settings(store, {"api_key": str}) -> (merged, errors)`；端点 `GET/PUT /api/admin/settings/mcp`（GET 不回显 key 明文，只回 `{"configured": bool}`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mcp_settings.py
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_settings.py -v`
Expected: FAIL（ImportError: get_mcp_settings 不存在）

- [ ] **Step 3: 实现 platform_settings.py MCP 块**

在 `save_display_settings` 函数后追加（照 display 块的口径）：

```python
# ── MCP 接入设置(mcp 键;对外 MCP 端点的全局单 key) ──

MCP_KEY = "mcp"
MCP_DEFAULTS: Dict[str, str] = {"api_key": ""}


def get_mcp_settings(store: Optional["PlatformSettingsStore"]) -> Dict[str, str]:
    """读 MCP 设置:损坏/缺失回默认(key 为空串=未启用,fail-closed)。"""
    saved = store.get(MCP_KEY) if store is not None else None
    saved = saved if isinstance(saved, dict) else {}
    key = saved.get("api_key")
    return {"api_key": key if isinstance(key, str) else MCP_DEFAULTS["api_key"]}


def save_mcp_settings(
    store: "PlatformSettingsStore", payload: Dict[str, Any]
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """校验并保存 MCP 设置(只认 api_key 一个键,必须非空字符串可清空)。"""
    errors: List[Dict[str, str]] = []
    clean: Dict[str, Any] = {}
    for key, raw in (payload or {}).items():
        if key not in MCP_DEFAULTS:
            errors.append({"field": key, "message": "未知配置项(只支持 api_key)"})
            continue
        if not isinstance(raw, str):
            errors.append({"field": key, "message": "api_key 必须是字符串"})
            continue
        clean[key] = raw
    if errors:
        return get_mcp_settings(store), errors
    merged = get_mcp_settings(store)
    merged.update(clean)
    store.set(MCP_KEY, merged)
    return merged, []
```

- [ ] **Step 4: 实现 admin 端点**

在 `api/admin.py` 的 display 设置端点之后追加（同款 `require_admin` 依赖与响应结构）：

```python
from services.platform_settings import (
    get_platform_settings_store, get_mcp_settings, save_mcp_settings)


@router.get("/settings/mcp")
async def get_mcp(request: Request, _: None = Depends(require_admin)):
    """MCP 接入设置状态(不回显 key 明文,只报是否已配置)。"""
    store = get_platform_settings_store(request)  # 懒建,最小测试 app 也可用
    return {"configured": bool(get_mcp_settings(store)["api_key"])}


@router.put("/settings/mcp")
async def put_mcp(request: Request, _: None = Depends(require_admin)):
    """保存 MCP API Key(全局单 key;清空即停用对外 MCP 端点)。"""
    store = get_platform_settings_store(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    merged, errors = save_mcp_settings(store, payload or {})
    if errors:
        raise HTTPException(422, errors)
    return {"configured": bool(merged["api_key"])}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_settings.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/services/platform_settings.py backend/src/api/admin.py backend/tests/test_mcp_settings.py
git commit -m "feat(mcp): MCP 全局单 key 平台设置 + 管理端读写端点"
```

---

### Task 2: 对外 MCP 端点（转换器 + 无状态挂载 + key 鉴权）

**Files:**
- Create: `backend/src/services/mcp_server.py`
- Create: `backend/src/api/mcp.py`（挂载入口，main.py import）
- Modify: `backend/src/main.py`（lifespan 启停 + mount）
- Modify: `backend/src/api/auth.py:169-170`（白名单加 `/api/mcp` 前缀）
- Test: `backend/tests/test_mcp_server.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `get_mcp_settings`；`app.state.registry`（ToolRegistry）、`app.state.pack_configs`（dict[name, config.yaml dict]）、`app.state.llm_client / asset_client`（装配后必在，main.py:128/153）。
- Produces: `services.mcp_server.startup_mcp(app)` / `shutdown_mcp()`（main lifespan 调用）；`iter_exposed_tools(app_state) -> list[Tool]`；`tool_to_mcp(tool, app_state) -> mcp.types.Tool`；`run_tool(app_state, name, args) -> dict`。

- [ ] **Step 1: 写失败测试（官方 client 进程内全链路 + 鉴权）**

```python
# backend/tests/test_mcp_server.py
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
    app.mount("/api/mcp", mcp_server.build_mcp_asgi_app(app.state))
    return app


def _client_factory(app):
    def _f():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=5)
    return _f


def test_full_roundtrip():
    """事件循环纪律:session manager 的 run() 与请求必须同 loop——
    startup→交互→shutdown 全部包进同一个 asyncio.run。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from services import mcp_server
    app = _make_app()

    async def run():
        await mcp_server.startup_mcp(app)
        try:
            async with streamablehttp_client(
                    MCP_URL, headers={"X-Api-Key": "sk-test"},
                    httpx_client_factory=_client_factory(app)) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    assert [t.name for t in tools.tools] == ["echo"]
                    assert "回声" in tools.tools[0].description
                    res = await s.call_tool("echo", {"text": "hi"})
                    assert not res.isError
        finally:
            await mcp_server.shutdown_mcp()

    asyncio.run(run())


def _init_body():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}


def test_wrong_key_401():
    r = httpx.post(MCP_URL, headers={"X-Api-Key": "bad"}, json=_init_body(),
                   transport=httpx.ASGITransport(app=_make_app()))
    assert r.status_code == 401


def test_no_key_401():
    r = httpx.post(MCP_URL, json=_init_body(),
                   transport=httpx.ASGITransport(app=_make_app()))
    assert r.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: FAIL（ModuleNotFoundError: services.mcp_server）

- [ ] **Step 3: 实现 services/mcp_server.py**

```python
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
    """同步执行一个内部工具并转 MCP 结果 dict(error_for_llm 如实抛出)。"""
    registry = getattr(app_state, "registry", None)
    tool = registry.get(name) if registry else None
    if tool is None:
        raise RuntimeError(f"tool not found: {name}")
    state = dict(args or {})
    state.setdefault("pack_params", {})
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
    """无状态 entry:key 校验(fail-closed:未配置 503/错 key 401)后交 session manager。"""
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
            elif not hmac.compare_digest(key, expected):
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
    return Starlette(routes=[
        Route("/", endpoint=entry, methods=["GET", "POST", "DELETE"]),
        Mount("/", app=entry),
    ])


async def startup_mcp(app: Any) -> None:
    """启动 session manager(main lifespan / 测试 fixture 各调一次;幂等)。"""
    global _session_manager
    if _session_manager is not None:
        return
    manager = getattr(app.state, "_mcp_manager", None)
    if manager is None:
        return
    _session_manager = manager
    await manager.run().__aenter__()


async def shutdown_mcp() -> None:
    global _session_manager
    if _session_manager is not None:
        await _session_manager.run().__aexit__(None, None, None)
        _session_manager = None
```

注：fixture 里 `app.mount("/api/mcp", ...)` 与生产 main.py 挂法一致（子应用内 Route("/") 即端点）。`app.state` 就是 build 时传入的 app_state 对象，`setattr` 对外可见。测试串行跑，全局 `_session_manager` 幂等启动/关闭够用（多 app 场景每 fixture 关旧再启新）。

- [ ] **Step 4: 跑 Task 2 测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: 3 PASS

- [ ] **Step 5: 接线 main.py + auth 白名单**

`main.py` lifespan 的 `assemble_packs` 之后（graph 构建前）：

```python
    # 对外 MCP server(无状态;key 由管理端 platform_settings 配)
    from services.mcp_server import build_mcp_asgi_app, startup_mcp, shutdown_mcp
    app.mount("/api/mcp", build_mcp_asgi_app(app.state))
    await startup_mcp(app)
```

shutdown 段（task_manager.close() 之后）追加 `await shutdown_mcp()`。

`api/auth.py` dispatch 中 `/api/open/` 放行块（:169-170）后追加：

```python
        # 对外 MCP 端点(/api/mcp/*):模块内 X-Api-Key 独自设门(同 /api/open 模式)
        if path.startswith("/api/mcp"):
            return await call_next(request)
```

- [ ] **Step 6: 跑本文件 + 相邻回归**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_server.py tests/test_admin_api.py -v`
Expected: 全 PASS

- [ ] **Step 7: Commit**

```bash
git add backend/src/services/mcp_server.py backend/src/main.py backend/src/api/auth.py backend/tests/test_mcp_server.py
git commit -m "feat(mcp): 对外 MCP 端点——Tool 自动转换+无状态挂载+单 key 鉴权"
```

---

### Task 3: KG 声明 + kb_search 动态描述

**Files:**
- Modify: `backend/src/domains/knowledge_graph/config.yaml`（runtime_contract 后加 `mcp_tools: [kb_search]`）
- Modify: `backend/src/domains/knowledge_graph/tools/kb_search.py`（加 `mcp_description` 方法）
- Test: `backend/tests/domains/test_kg_mcp_declare.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `tool_to_mcp` 会调 `tool.mcp_description(app_state)` 钩子（可选）。
- Produces: kb_search 对外描述 = 原 description + "\n\n可选知识库: A、B、C"（fail-open：store 故障回默认描述）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/domains/test_kg_mcp_declare.py
"""KG 对外暴露声明:config.yaml mcp_tools + kb_search 动态描述(知识库清单)。"""
import yaml
from types import SimpleNamespace
from sdk.tool import ToolResult


def test_config_declares_kb_search():
    from pathlib import Path
    cfg = yaml.safe_load(Path(
        "src/domains/knowledge_graph/config.yaml").read_text())
    assert cfg.get("mcp_tools") == ["kb_search"]


def test_mcp_description_lists_kbs(monkeypatch):
    from domains.knowledge_graph.tools.kb_search import KbSearchTool
    tool = KbSearchTool(SimpleNamespace())   # app_state 占位
    monkeypatch.setattr(
        "domains.knowledge_graph.runtime.get_kg_store",
        lambda st: SimpleNamespace(
            list_kbs=lambda: [{"name": "产品手册"}, {"name": "规章制度"}]))
    desc = tool.mcp_description(SimpleNamespace())
    assert "kb_search" in desc or "知识库" in desc
    assert "产品手册" in desc and "规章制度" in desc
    assert "回声" not in desc


def test_mcp_description_fail_open(monkeypatch):
    from domains.knowledge_graph.tools.kb_search import KbSearchTool
    tool = KbSearchTool(None)
    def boom(st):
        raise RuntimeError("store down")
    monkeypatch.setattr(
        "domains.knowledge_graph.runtime.get_kg_store", boom)
    assert tool.mcp_description(None) is None   # 回退默认描述
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/domains/test_kg_mcp_declare.py -v`
Expected: FAIL（mcp_tools 声明不存在）

- [ ] **Step 3: 实现**

config.yaml `runtime_contract:` 块之后插入：

```yaml
# 对外 MCP 暴露声明(服务层 services/mcp_server.py 消费;默认不暴露)
mcp_tools: [kb_search]
```

kb_search.py KbSearchTool 类内（format_result 前后均可）追加：

```python
    def mcp_description(self, app_state) -> str:
        """对外 MCP 描述:拼上当前知识库清单(外部 AI 一次 list_tools 即知
        有哪些库可选)。store 不可用时返回 None → 调用方回退默认描述(fail-open)。"""
        from domains.knowledge_graph import runtime
        try:
            names = [k["name"] for k in runtime.get_kg_store(app_state).list_kbs()
                     if k.get("kind") != "folder"]
        except Exception:
            return None
        libs = "、".join(names) if names else "(暂无知识库)"
        return f"{self.description}\n\n可选知识库: {libs}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/domains/test_kg_mcp_declare.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/domains/knowledge_graph/config.yaml backend/src/domains/knowledge_graph/tools/kb_search.py backend/tests/domains/test_kg_mcp_declare.py
git commit -m "feat(kg): kb_search 对外 MCP 声明+动态知识库清单描述"
```

---

### Task 4: sdk/mcp_client.py（客户端设施，插件唯二入口）

**Files:**
- Create: `backend/src/sdk/mcp_client.py`
- Test: `backend/tests/sdk/test_mcp_client.py`（新建；tests/sdk/ 目录已存在）

**Interfaces:**
- Consumes: `mcp.ClientSession`、`mcp.client.streamable_http.streamablehttp_client`、`mcp.client.sse.sse_client`、`sdk.tool.ToolError`（sdk/tool.py:236，error_type 三类）。
- Produces: `register_server_loader(fn)`（fn: code -> dict|None，返回 `{"url","headers","timeout","read_timeout"}`；依赖倒置，services 层装配期注册，同 sdk/relational_store.py 的 register_engine_factory 模式）；`call_tool(code: str, tool: str, args: dict) -> dict`（同步）；`list_tools(code: str) -> list[dict]`（同步，实时探测，注册表探测用）；`list_remote_tools(server_cfg: dict) -> list[dict]` / `call_remote_tool(server_cfg: dict, tool, args) -> dict`（不查注册表，直接按 cfg 连）；模块级 `_http_factory`（测试注入 ASGITransport 用，生产 None=官方 create_mcp_http_client）。

- [ ] **Step 1: 写失败测试（外部 server 用进程内第二 app 模拟）**

```python
# backend/tests/sdk/test_mcp_client.py
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
    独立 loop + lifespan(startup_mcp 在其内启动),天然隔离。"""
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
    ext.mount("/api/mcp", mcp_server.build_mcp_asgi_app(ext.state))

    server = uvicorn.Server(uvicorn.Config(
        ext, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {"url": f"http://127.0.0.1:{port}/api/mcp",
           "headers": {"X-Api-Key": "sk-ext"},
           "timeout": 5.0, "read_timeout": 10.0}
    server.should_exit = True


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/sdk/test_mcp_client.py -v`
Expected: FAIL（ModuleNotFoundError: sdk.mcp_client）

- [ ] **Step 3: 实现 sdk/mcp_client.py**

```python
"""SDK MCP 客户端设施——插件消费外部 MCP 的唯二入口。

插件只调 list_tools(code)/call_tool(code, tool, args),协议细节(传输
协商/短连接/超时/结果转换)全在本层;server 配置经依赖倒置注入
(register_server_loader,装配期由 services/mcp_registry.py 注册,
模式同 sdk/relational_store.py 的 register_engine_factory)。

连接策略(Dify 验证):每次调用临时建连接用完即毁;URL 以 /sse 结尾
走 SSE,否则 streamable HTTP;失败抛 ToolError(retryable)如实上报。
"""
import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from sdk.tool import ToolError

logger = logging.getLogger(__name__)

_server_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
_http_factory: Optional[Callable[..., Any]] = None  # 测试注入 ASGITransport


def register_server_loader(fn: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    global _server_loader
    _server_loader = fn


def _load_server(code: str) -> Dict[str, Any]:
    cfg = _server_loader(code) if _server_loader else None
    if not cfg:
        raise ToolError(f"未注册的 MCP server: {code}", ToolError.PERMANENT)
    return cfg


async def _run_session(cfg, tool: Optional[str], args: Optional[dict]):
    """建立一次连接,执行 list_tools 或 call_tool,返回原始结果。"""
    from mcp import ClientSession
    url = cfg["url"]
    headers = cfg.get("headers") or {}
    timeout = float(cfg.get("timeout") or 30)
    read_timeout = float(cfg.get("read_timeout") or 300)
    factory = _http_factory
    if url.rstrip("/").endswith("/sse"):
        from mcp.client.sse import sse_client
        cm = sse_client(url, headers=headers, timeout=timeout)
    else:
        from mcp.client.streamable_http import streamablehttp_client
        kwargs = {"headers": headers, "timeout": timeout,
                  "sse_read_timeout": read_timeout}
        if factory is not None:
            kwargs["httpx_client_factory"] = factory
        cm = streamablehttp_client(url, **kwargs)
    async with cm as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool is None:
                return await session.list_tools()
            return await session.call_tool(tool, args or {})


def _convert(result) -> dict:
    """CallToolResult → dict:structuredContent 优先;text 先试 json.loads。"""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for c in getattr(result, "content", None) or []:
        text = getattr(c, "text", None)
        if text is not None:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, (dict, list)) else {"text": parsed}
            except (ValueError, TypeError):
                continue
    return {"content": [getattr(c, "text", "") for c in result.content or []]}


def _sync(coro):
    return asyncio.run(coro)  # 短连接无跨调用状态,每调用新事件循环安全


def list_remote_tools(server_cfg: Dict[str, Any]) -> List[dict]:
    """直连探测(注册表刷新用),返回 [{name,description,inputSchema,...}]。"""
    try:
        result = _sync(_run_session(server_cfg, None, None))
        return [{"name": t.name, "description": t.description or "",
                 "inputSchema": t.inputSchema} for t in result.tools]
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"MCP server 连接失败({server_cfg['url']}): {e}",
                        ToolError.RETRYABLE)


def call_remote_tool(server_cfg: Dict[str, Any], tool: str, args: dict) -> dict:
    try:
        result = _sync(_run_session(server_cfg, tool, args))
        if getattr(result, "isError", False):
            msgs = [getattr(c, "text", "") for c in result.content or []]
            raise ToolError(f"MCP 工具 {tool} 执行失败: {'; '.join(msgs)}",
                            ToolError.PERMANENT)
        return _convert(result)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"MCP 调用失败({server_cfg['url']}/{tool}): {e}",
                        ToolError.RETRYABLE)


def list_tools(code: str) -> List[dict]:
    return list_remote_tools(_load_server(code))


def call_tool(code: str, tool: str, args: dict) -> dict:
    return call_remote_tool(_load_server(code), tool, args)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/sdk/test_mcp_client.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/sdk/mcp_client.py backend/tests/sdk/test_mcp_client.py
git commit -m "feat(mcp): SDK 客户端设施——短连接+传输协商+ToolError 分流"
```

---

### Task 5: mcp_servers 注册表 + 管理端 CRUD API

**Files:**
- Create: `backend/src/services/mcp_registry.py`
- Modify: `backend/src/api/admin.py`（追加 /mcp/servers 路由组）
- Modify: `backend/src/main.py`（lifespan 装配 McpServerStore + register_server_loader）
- Test: `backend/tests/test_mcp_registry.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 `register_server_loader` / `list_remote_tools`。
- Produces: `McpServerStore(database_url=None)`：`list_servers() -> list[dict]`、`get_by_code(code) -> dict|None`、`create_server(name, code, url, headers, timeout, read_timeout) -> dict`、`update_server(id, **fields) -> dict`、`delete_server(id) -> None`、`apply_probe(id) -> dict`（网络探测回写 status/tools_snapshot，**探测在 DB 连接外**）。行 dict 字段：`id/name/code/url/headers/timeout/read_timeout/status/tools_snapshot/created_at/updated_at`。管理端路由 `GET/POST /api/admin/mcp/servers`、`PUT/DELETE /api/admin/mcp/servers/{id}`、`POST /api/admin/mcp/servers/{id}/refresh`。

- [ ] **Step 1: 写失败测试**

```python
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
        await mcp_server.shutdown_mcp()

    ext = FastAPI(lifespan=_lsp)
    ext.state.platform_settings_store = PlatformSettingsStore()
    ext.state.registry = ToolRegistry()
    ext.state.registry.register(EchoTool())
    ext.state.pack_configs = {"demo": {"mcp_tools": ["echo"]}}
    ext.state.llm_client = SimpleNamespace()
    ext.state.asset_client = SimpleNamespace()
    ext.mount("/api/mcp", mcp_server.build_mcp_asgi_app(ext.state))

    server = uvicorn.Server(uvicorn.Config(
        ext, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/api/mcp"

    app = FastAPI()
    from api.admin import router
    from services.mcp_registry import McpServerStore
    app.include_router(router)
    app.state.mcp_server_store = McpServerStore()
    with TestClient(app) as c:
        yield c, url
    server.should_exit = True


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_registry.py -v`
Expected: FAIL（ModuleNotFoundError: services.mcp_registry）

- [ ] **Step 3: 实现 services/mcp_registry.py**

```python
"""mcp_servers 注册表——外部 MCP server 的平台级登记处。

表结构=设计文档 §3.3 最小集;明文存储(内网+admin 门控,不加密)。
探测(网络 IO)在 DB 连接之外:save 先落库(pending)→ 事务外 list_remote_tools
→ 短事务回写快照+status(Dify 三段式,防慢连接占池)。
"""
import json
import logging
import re
import threading
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
_DDL = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    code          TEXT NOT NULL UNIQUE,
    url           TEXT NOT NULL,
    headers_json  TEXT NOT NULL DEFAULT '{}',
    timeout       REAL NOT NULL DEFAULT 30,
    read_timeout  REAL NOT NULL DEFAULT 300,
    status        TEXT NOT NULL DEFAULT 'pending',
    tools_json    TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
"""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class McpServerStore:
    """mcp_servers 表 DAO(照抄 PlatformSettingsStore:每方法新连接+写锁)。"""

    def __init__(self, database_url: Optional[str] = None):
        from services.db import get_pg_engine
        self.db = get_pg_engine(database_url)
        self._lock = threading.Lock()
        with self.db.connect() as conn:
            conn.execute(_DDL)

    def _row(self, r) -> Dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "code": r["code"], "url": r["url"],
                "headers": json.loads(r["headers_json"] or "{}"),
                "timeout": r["timeout"], "read_timeout": r["read_timeout"],
                "status": r["status"],
                "tools_snapshot": json.loads(r["tools_json"] or "[]"),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list_servers(self) -> List[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_servers ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def get_by_code(self, code: str) -> Optional[dict]:
        with self.db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM mcp_servers WHERE code = ?", (code,)).fetchone()
        return self._row(r) if r else None

    def create_server(self, name: str, code: str, url: str, headers: dict,
                      timeout: float = 30.0, read_timeout: float = 300.0) -> dict:
        if not _CODE_RE.fullmatch(code or ""):
            raise ValueError("code 只允许小写字母/数字/-/_，1-24 位")
        sid, now = uuid.uuid4().hex, _now()
        try:
            with self._lock, self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO mcp_servers (id,name,code,url,headers_json,"
                    "timeout,read_timeout,status,tools_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,'pending','[]',?,?)",
                    (sid, name, code, url, json.dumps(headers or {}), timeout,
                     read_timeout, now, now))
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(f"code 已存在: {code}")
            raise
        return self.apply_probe(sid)

    def update_server(self, sid: str, **fields: Any) -> dict:
        with self._lock, self.db.connect() as conn:
            cur = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
            if not cur:
                raise ValueError("server 不存在")
            f = {"name": fields.get("name", cur["name"]),
                 "url": fields.get("url", cur["url"]),
                 "headers_json": json.dumps(
                     fields.get("headers", json.loads(cur["headers_json"] or "{}"))),
                 "timeout": fields.get("timeout", cur["timeout"]),
                 "read_timeout": fields.get("read_timeout", cur["read_timeout"])}
            conn.execute(
                "UPDATE mcp_servers SET name=?,url=?,headers_json=?,timeout=?,"
                "read_timeout=?,updated_at=? WHERE id=?",
                (f["name"], f["url"], f["headers_json"], f["timeout"],
                 f["read_timeout"], _now(), sid))
        return self.apply_probe(sid)

    def delete_server(self, sid: str) -> None:
        with self._lock, self.db.connect() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = ?", (sid,))

    def apply_probe(self, sid: str) -> dict:
        """事务外探测回写:成功 connected+快照;失败保持 pending(不抛,如实返回)。"""
        with self.db.connect() as conn:
            cur = conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        if not cur:
            raise ValueError("server 不存在")
        cfg = {"url": cur["url"], "headers": json.loads(cur["headers_json"] or "{}"),
               "timeout": cur["timeout"], "read_timeout": cur["read_timeout"]}
        status, snapshot = "pending", []
        try:
            from sdk.mcp_client import list_remote_tools
            snapshot = list_remote_tools(cfg)
            status = "connected"
        except Exception as e:
            logger.warning(f"mcp probe failed({cur['code']}): {e}")
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE mcp_servers SET status=?,tools_json=?,updated_at=? WHERE id=?",
                (status, json.dumps(snapshot, ensure_ascii=False), _now(), sid))
            cur = conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        return self._row(cur)
```

- [ ] **Step 4: admin 路由 + main 装配**

`api/admin.py` 追加（复用文件内既有 `require_admin`、`HTTPException`）：

```python
def _mcp_store(request: Request):
    from services.mcp_registry import McpServerStore
    store = getattr(request.app.state, "mcp_server_store", None)
    if store is None:
        store = McpServerStore()
        request.app.state.mcp_server_store = store
    return store


@router.get("/mcp/servers")
async def list_mcp_servers(request: Request, _: None = Depends(require_admin)):
    return _mcp_store(request).list_servers()


@router.post("/mcp/servers")
async def create_mcp_server(request: Request, _: None = Depends(require_admin)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    try:
        return _mcp_store(request).create_server(
            name=str(payload.get("name") or "").strip(),
            code=str(payload.get("code") or "").strip(),
            url=str(payload.get("url") or "").strip(),
            headers=payload.get("headers") or {},
            timeout=float(payload.get("timeout") or 30),
            read_timeout=float(payload.get("read_timeout") or 300))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.put("/mcp/servers/{sid}")
async def update_mcp_server(sid: str, request: Request,
                            _: None = Depends(require_admin)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    try:
        return _mcp_store(request).update_server(sid, **payload)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/mcp/servers/{sid}")
async def delete_mcp_server(sid: str, request: Request,
                            _: None = Depends(require_admin)):
    _mcp_store(request).delete_server(sid)
    return {"ok": True}


@router.post("/mcp/servers/{sid}/refresh")
async def refresh_mcp_server(sid: str, request: Request,
                             _: None = Depends(require_admin)):
    try:
        return _mcp_store(request).apply_probe(sid)
    except ValueError as e:
        raise HTTPException(404, str(e))
```

`main.py` lifespan（platform_settings_store 之后）：

```python
    # 外部 MCP server 注册表 + SDK server 装载器接线(依赖倒置)
    from services.mcp_registry import McpServerStore
    from sdk.mcp_client import register_server_loader
    mcp_store = McpServerStore(database_url=database_url)
    app.state.mcp_server_store = mcp_store
    register_server_loader(lambda code: mcp_store.get_by_code(code))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/test_mcp_registry.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/services/mcp_registry.py backend/src/api/admin.py backend/src/main.py backend/tests/test_mcp_registry.py
git commit -m "feat(mcp): mcp_servers 注册表+管理端 CRUD+注册即探测"
```

---

### Task 6: 管理端「MCP 服务」面板（挂插件页，不新开顶级 tab）

**Files:**
- Create: `frontend/src/admin/components/McpServersPanel.vue`
- Modify: `frontend/src/admin/components/PacksTab.vue`（页底引入面板分区）
- Modify: `frontend/src/admin/api.ts`（追加 MCP 函数）

**Interfaces:**
- Consumes: Task 1 `GET/PUT /api/admin/settings/mcp`、Task 5 全部 `/api/admin/mcp/servers*` 端点。
- Produces: PacksTab 页底"外部 MCP 服务"分区：server 列表 + 新增/编辑弹窗 + 删除 + 刷新快照 + 工具快照展开 + 对外接入 key 设置（spec §5"不新开顶级页"，挂插件页同容器）。

- [ ] **Step 1: api.ts 追加函数**

```typescript
// ── MCP 服务管理(注册外部 MCP server + 对外接入 key) ──
export interface McpServer {
  id: string; name: string; code: string; url: string
  headers: Record<string, string>; timeout: number; read_timeout: number
  status: 'connected' | 'pending'
  tools_snapshot: { name: string; description: string }[]
  created_at: string
}
export const listMcpServers = () => adminGet<McpServer[]>('/api/admin/mcp/servers')
export const createMcpServer = (body: Partial<McpServer>) =>
  adminPost<Partial<McpServer>, McpServer>('/api/admin/mcp/servers', body)
export const updateMcpServer = (id: string, body: Partial<McpServer>) =>
  adminPut<Partial<McpServer>, McpServer>(`/api/admin/mcp/servers/${id}`, body)
export const deleteMcpServer = (id: string) => adminDelete(`/api/admin/mcp/servers/${id}`)
export const refreshMcpServer = (id: string) =>
  adminPost<Record<string, never>, McpServer>(`/api/admin/mcp/servers/${id}/refresh`, {})
export const getMcpKeyStatus = () =>
  adminGet<{ configured: boolean }>('/api/admin/settings/mcp')
export const putMcpKey = (apiKey: string) =>
  adminPut<{ api_key: string }, { configured: boolean }>(
    '/api/admin/settings/mcp', { api_key: apiKey })
```

（对照 api.ts 现有 adminGet/adminPost/adminPut/adminDelete 的实际签名，名字不同则适配。）

- [ ] **Step 2: McpServersPanel.vue**

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listMcpServers, createMcpServer, updateMcpServer,
  deleteMcpServer, refreshMcpServer, getMcpKeyStatus, putMcpKey,
  type McpServer,
} from '../api'

const servers = ref<McpServer[]>([])
const keyConfigured = ref(false)
const apiKeyInput = ref('')
const open = ref(false)
const editing = ref<McpServer | null>(null)
const form = ref({ name: '', code: '', url: '', headersText: '{}',
                   timeout: 30, read_timeout: 300 })
const columns = [
  { title: '名称', dataIndex: 'name' },
  { title: '标识', dataIndex: 'code' },
  { title: 'URL', dataIndex: 'url', ellipsis: true },
  { title: '状态', dataIndex: 'status' },
  { title: '工具数', key: 'tools',
    customRender: ({ record }: any) => record.tools_snapshot.length },
  { title: '操作', key: 'ops', width: 140 },
]

async function load() {
  servers.value = await listMcpServers()
  keyConfigured.value = (await getMcpKeyStatus()).configured
}
onMounted(load)

function openCreate() {
  editing.value = null
  form.value = { name: '', code: '', url: '', headersText: '{}',
                 timeout: 30, read_timeout: 300 }
  open.value = true
}
function openEdit(s: McpServer) {
  editing.value = s
  form.value = { name: s.name, code: s.code, url: s.url,
                 headersText: JSON.stringify(s.headers, null, 2),
                 timeout: s.timeout, read_timeout: s.read_timeout }
  open.value = true
}
async function save() {
  let headers: Record<string, string>
  try { headers = JSON.parse(form.value.headersText || '{}') }
  catch { message.error('认证头必须是合法 JSON'); return }
  const body = { name: form.value.name, url: form.value.url, headers,
                 timeout: form.value.timeout, read_timeout: form.value.read_timeout }
  const r = editing.value
    ? await updateMcpServer(editing.value.id, body)
    : await createMcpServer({ ...body, code: form.value.code })
  message.info(r.status === 'connected'
    ? '已连接，工具快照已保存' : '已保存，但连接失败（待连接）')
  open.value = false
  await load()
}
function remove(s: McpServer) {
  Modal.confirm({ title: `删除「${s.name}」？`,
    onOk: async () => { await deleteMcpServer(s.id); await load() } })
}
async function refresh(s: McpServer) {
  const r = await refreshMcpServer(s.id)
  message.info(r.status === 'connected'
    ? `快照已刷新（${r.tools_snapshot.length} 个工具）` : '连接失败，保持待连接')
  await load()
}
async function saveKey() {
  await putMcpKey(apiKeyInput.value)
  message.success(apiKeyInput.value ? '接入 Key 已保存' : '对外 MCP 端点已停用')
  apiKeyInput.value = ''
  await load()
}
</script>

<template>
  <section class="mcp-panel">
    <h3>外部 MCP 服务</h3>
    <div class="key-row">
      <span>对外接入（外部 AI 工具连 <code>/api/mcp</code>）：</span>
      <a-tag :color="keyConfigured ? 'green' : 'red'">
        {{ keyConfigured ? '已启用' : '未配置 Key' }}
      </a-tag>
      <a-input-password v-model:value="apiKeyInput" placeholder="设置/更换 MCP API Key"
                        style="width: 240px" />
      <a-button size="small" @click="saveKey">保存</a-button>
    </div>
    <a-table :data-source="servers" :columns="columns" row-key="id" size="small">
      <template #bodyCell="{ column, record }">
        <template v-if="column.dataIndex === 'status'">
          <a-tag :color="record.status === 'connected' ? 'green' : 'orange'">
            {{ record.status === 'connected' ? '已连接' : '待连接' }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'ops'">
          <a-tooltip title="刷新快照">
            <a-button size="small" type="text" @click="refresh(record)">
              <template #icon><ReloadOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="编辑">
            <a-button size="small" type="text" @click="openEdit(record)">
              <template #icon><EditOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="删除">
            <a-button size="small" type="text" danger @click="remove(record)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </a-tooltip>
        </template>
      </template>
      <template #expandedRowRender="{ record }">
        <a-collapse>
          <a-collapse-panel
            v-for="t in record.tools_snapshot" :key="t.name"
            :header="`${t.name} — ${t.description}`" :show-arrow="false" />
        </a-collapse>
        <div v-if="!record.tools_snapshot.length">（无工具快照）</div>
      </template>
    </a-table>
    <a-button type="primary" size="small" @click="openCreate">注册外部服务</a-button>

    <a-modal v-model:open="open" :title="editing ? '编辑 MCP 服务' : '注册 MCP 服务'"
             @ok="save">
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" />
        </a-form-item>
        <a-form-item v-if="!editing" label="标识（插件引用用，小写字母/数字/-/_）" required>
          <a-input v-model:value="form.code" placeholder="如 ext_data" />
        </a-form-item>
        <a-form-item label="URL" required>
          <a-input v-model:value="form.url" placeholder="http://host:port/mcp" />
        </a-form-item>
        <a-form-item label="认证头（JSON）">
          <a-textarea v-model:value="form.headersText" :rows="3" />
        </a-form-item>
        <a-form-item label="连接超时（秒）/ 读取超时（秒）">
          <a-input-number v-model:value="form.timeout" :min="1" />
          <a-input-number v-model:value="form.read_timeout" :min="1" />
        </a-form-item>
      </a-form>
    </a-modal>
  </section>
</template>
```

（图标从 '@ant-design/icons-vue' 导入 ReloadOutlined/EditOutlined/DeleteOutlined；样式类按 PacksTab 现有分区风格对齐。）

- [ ] **Step 3: PacksTab.vue 挂面板**

在 PacksTab.vue 模板末尾（插件列表之后）追加分区：

```vue
  <McpServersPanel />
```

并 `import McpServersPanel from './McpServersPanel.vue'`。不动 AdminApp.vue 的 navItems（spec §5：不新开顶级页）。

- [ ] **Step 4: 浏览器实测**

Run: `cd frontend && pnpm dev`（后端本地起 uvicorn）
用 chrome-devtools MCP 打开管理端 → 插件 tab：MCP 服务分区——注册一个 server（指向本机 `/api/mcp` 自测，key 用已配置的）→ 验证列表/快照展开/刷新/删除全链路 + 接入 key 设置；错误路径：坏 URL 显示"待连接"。
Expected: 全部交互可用，无 console 报错。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/components/McpServersPanel.vue frontend/src/admin/components/PacksTab.vue frontend/src/admin/api.ts
git commit -m "feat(mcp-admin): 插件页 MCP 服务面板——注册表 CRUD+快照+接入 key"
```

---

### Task 7: 全量回归 + 出口标准核验

**Files:**
- 无新文件；如回归发现问题，修复对应任务产物。

**Interfaces:**
- Consumes: Task 1-6 全部产物。

- [ ] **Step 1: 全量 pytest**

Run: `cd backend && .venv/bin/python -m pytest tests/ -q`（需本机 Docker 起 PG 测试库，见 tests/conftest.py:19-21；~1810 例约 7 分钟）
Expected: 全部 PASS，无新增失败。

- [ ] **Step 2: 出口标准核验（设计文档 §5 一期四条）**

1. 官方 MCP client 用 key 连 `/api/mcp`：tools/list 可见 kb_search（描述含知识库清单），检索往返成功（本地起 uvicorn + 真实 KG 环境手测一次）；
2. 错 key → 401（test_mcp_server.py 已自动化覆盖，抽查复跑）；
3. 管理端注册外部 server，快照可见，坏 URL 进 pending（Task 5 测试 + Task 6 浏览器实测已覆盖，抽查复跑）；
4. 全量 pytest 通过（Step 1）。

- [ ] **Step 3: 收尾提交**

```bash
git add -A && git commit -m "test(mcp): 一期全量回归通过"
```

（若 Step 1/2 有修复，逐修复单独提交后再做本收尾提交。）

