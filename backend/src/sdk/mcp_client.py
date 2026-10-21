"""SDK MCP 客户端设施——插件消费外部 MCP 的唯二入口。

插件只调 list_tools(code)/call_tool(code, tool, args),协议细节(传输
协商/短连接/超时/结果转换)全在本层;server 配置经依赖倒置注入
(register_server_loader,装配期由 services/mcp_registry.py 注册,
模式同 sdk/relational_store.py 的 register_engine_factory)。

连接策略(Dify 验证):每次调用临时建连接用完即毁;URL 以 /sse 结尾
走 SSE,否则 streamable HTTP;失败抛 ToolError(retryable)如实上报。

mcp 1.28.1 API 裁决(Task 2 实测):streamablehttp_client 已弃用
(headers/timeout 等参数收了也会告警,pytest filterwarnings=error 直红),
改用 streamable_http_client(url, http_client=预构建 httpx.AsyncClient)——
headers/timeout 设在预构建 client 上,client 生命周期由本层负责(新 API
不托管外部 client,需自行 aclose);sse_client 仍是现行签名(headers/
timeout/sse_read_timeout 直接传,自管 client)。
"""
import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from sdk.tool import PackInvokeError as ToolError  # brief 所指 ToolError 即此类(结构化错误信封,error_type 三类)

logger = logging.getLogger(__name__)

_server_loader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
# http client 注入点(测试用):(headers, timeout, read_timeout) -> httpx.AsyncClient。
# 生产 None=默认构建 httpx.AsyncClient(headers/timeout 按配置);测试可注入
# ASGITransport 背书的 client 做进程内直连(旧 API 的 httpx_client_factory
# 形态已随 mcp 1.28.1 弃用,改为对预构建 client 的注入点)。
_http_factory: Optional[Callable[..., httpx.AsyncClient]] = None


def register_server_loader(fn: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    global _server_loader
    _server_loader = fn


def _load_server(code: str) -> Dict[str, Any]:
    cfg = _server_loader(code) if _server_loader else None
    if not cfg:
        raise ToolError(f"未注册的 MCP server: {code}", ToolError.PERMANENT)
    return cfg


def _build_http_client(headers: Dict[str, str], timeout: float,
                       read_timeout: float) -> httpx.AsyncClient:
    if _http_factory is not None:
        return _http_factory(headers, timeout, read_timeout)
    return httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(timeout, read=read_timeout))


async def _invoke(session, tool: Optional[str], args: Optional[dict]):
    if tool is None:
        return await session.list_tools()
    return await session.call_tool(tool, args or {})


async def _run_session(cfg, tool: Optional[str], args: Optional[dict]):
    """建立一次连接,执行 list_tools 或 call_tool,返回原始结果。"""
    from mcp import ClientSession
    url = cfg["url"]
    headers = cfg.get("headers") or {}
    timeout = float(cfg.get("timeout") or 30)
    read_timeout = float(cfg.get("read_timeout") or 300)
    if url.rstrip("/").endswith("/sse"):
        from mcp.client.sse import sse_client
        async with sse_client(url, headers=headers, timeout=timeout,
                              sse_read_timeout=read_timeout) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await _invoke(session, tool, args)
    from mcp.client.streamable_http import streamable_http_client
    client = _build_http_client(headers, timeout, read_timeout)
    async with client:  # 新 API 不托管外部 client,本层负责 aclose
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await _invoke(session, tool, args)


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
