"""ChatBI 受控执行服务(客服等编排方消费的薄入口;设计文档 C3/4.3/4.4)。

与 execute_mcp_tool 的关键差异(离线探针实证的缺口,此处薄版修复):
  - 显式 datasource_id + 固定工具名,不猜第一个 active 源;
  - **先判业务信封再归一**——上游返回 {status: "forbidden"} 不再被
    当成 ok=True 的"成功数据行",not_found/invalid_input/unavailable
    同样按业务状态分类返回;
  - PackInvokeError 的错误类型(credential/retryable/permanent)原样
    保留,不抹平成字符串。

信封约定:上游返回 dict 且含已知 status 值(ok/forbidden/not_found/
invalid_input/unavailable/error)即业务信封;非信封按表格形状归一
(columns/rows、list[dict]、裸 dict 单条记录)。
"""
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from sdk.tool import PackInvokeError

ENVELOPE_STATUS = ("ok", "forbidden", "not_found",
                   "invalid_input", "unavailable", "error")

# 业务状态 → (error_type, retryable)
_STATUS_CLASSIFICATION = {
    "forbidden": ("permanent", False),
    "not_found": ("permanent", False),
    "invalid_input": ("permanent", False),
    "unavailable": ("retryable", True),
    "error": ("permanent", False),
}


@dataclass
class ControlledError:
    code: str            # forbidden/not_found/invalid_input/unavailable/credential/transport
    message: str         # 用户可读文案(不含凭据/内部路径)
    error_type: str      # permanent/retryable/credential(与 PackInvokeError 同口径)
    retryable: bool


@dataclass
class ControlledResult:
    ok: bool
    status: str                      # ok / 业务状态 / credential / transport / invalid_input
    data: Optional[dict] = None      # {"columns","rows"} 或 {"record"}(成功时)
    error: Optional[ControlledError] = None
    duration_ms: int = 0
    source: dict = field(default_factory=dict)  # {"datasource", "tool"}(观测用)


def _err(status: str, message: str, started: float,
         source: Optional[dict] = None) -> "ControlledResult":
    error_type, retryable = _STATUS_CLASSIFICATION.get(
        status, ("permanent", False))
    return ControlledResult(
        ok=False, status=status,
        error=ControlledError(code=status, message=message,
                              error_type=error_type, retryable=retryable),
        duration_ms=int((time.monotonic() - started) * 1000),
        source=source or {})


def _normalize_tabular(payload: Any) -> Optional[dict]:
    """非信封返回的表格形状归一(与 execute_mcp_tool 同映射)。"""
    if isinstance(payload, dict) and isinstance(payload.get("columns"), list) \
            and isinstance(payload.get("rows"), list):
        columns = [str(c) for c in payload["columns"]]
        rows = [tuple(r) if isinstance(r, (list, tuple)) else (r,)
                for r in payload["rows"]]
        return {"columns": columns, "rows": rows}
    if isinstance(payload, list):
        if not payload:
            return {"columns": [], "rows": []}
        if all(isinstance(r, dict) for r in payload):
            columns = [str(k) for k in payload[0].keys()]
            rows = [tuple(r.get(c) for c in columns) for r in payload]
            return {"columns": columns, "rows": rows}
        if all(isinstance(r, (list, tuple)) for r in payload):
            return None  # 无列名,不猜
        return {"columns": ["value"],
                "rows": [(r,) for r in payload]}
    if isinstance(payload, dict):
        return {"record": dict(payload)}
    return None


def execute_controlled(app_state, datasource_id: str, tool: str,
                       args: dict) -> ControlledResult:
    """受控执行:验源验参 → SDK 调用 → 先判信封 → 仅成功数据归一。

    失败如实返回 ControlledResult(ok=False),不静默、不冒充成功;
    前置校验失败时不发 MCP 请求。
    """
    started = time.monotonic()
    from domains.chatbi import datasources
    from domains.chatbi.runtime import get_pack_db

    if not isinstance(args, dict):
        return _err("invalid_input", "调用参数必须是 JSON 对象", started)

    info = datasources.get_datasource(get_pack_db(), datasource_id, decrypt=True)
    source = {"datasource": (info.name if info else datasource_id), "tool": tool}
    if info is None:
        return _err("invalid_input", f"数据源不存在: {datasource_id}", started, source)
    if not info.is_active:
        return _err("unavailable", f"数据源已停用: {info.name}", started, source)
    if info.db_type != "mcp":
        return _err("invalid_input",
                    f"受控执行仅支持 MCP 数据源, {info.name} 是 {info.db_type}",
                    started, source)
    if not isinstance(tool, str) or not tool.strip():
        return _err("invalid_input", "缺少固定工具名", started, source)
    # 工具白名单: info.database(=tool_name 限定)非空时只允许该工具
    if (info.database or "").strip() and tool != info.database:
        return _err("invalid_input",
                    f"工具 {tool} 不在数据源限定范围内(仅允许 {info.database})",
                    started, source)

    # ── SDK 调用(隔离性铁律: 只 import sdk;错误类型保留) ──
    from sdk import mcp_client
    try:
        payload = mcp_client.call_tool(info.host, tool, args)
    except PackInvokeError as e:
        status = {"credential": "credential",
                  "retryable": "unavailable"}.get(e.error_type, "transport")
        return ControlledResult(
            ok=False, status=status,
            error=ControlledError(
                code=status, message=e.message[:300],
                error_type=e.error_type,
                retryable=(e.error_type == "retryable")),
            duration_ms=int((time.monotonic() - started) * 1000),
            source=source)
    except Exception as e:  # 兜底: mcp_client 理论上已包装
        return _err("transport", f"MCP 调用失败: {e}", started, source)

    # ── 信封判定(先于任何形状归一) ──
    # 信封 = status 为已知值 且 带 data/message 载荷键——纯记录恰好含
    # status 字段(如订单状态列)不被误判;ok 配 data,其余配 message
    if isinstance(payload, dict) and payload.get("status") in ENVELOPE_STATUS \
            and ("data" in payload or "message" in payload):
        status = str(payload["status"])
        if status == "ok":
            normalized = _normalize_tabular(payload.get("data"))
            if normalized is None:
                return _err("invalid_input",
                            "业务信封 ok 但 data 格式不符合契约", started, source)
            return ControlledResult(ok=True, status="ok", data=normalized,
                                    duration_ms=int((time.monotonic() - started) * 1000),
                                    source=source)
        message = str(payload.get("message") or f"业务平台返回 {status}")
        return _err(status, message, started, source)

    # ── 非信封:表格形状归一 ──
    normalized = _normalize_tabular(payload)
    if normalized is None:
        return _err("invalid_input", "上游返回格式不符合声明的表格契约",
                    started, source)
    return ControlledResult(ok=True, status="ok", data=normalized,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            source=source)
