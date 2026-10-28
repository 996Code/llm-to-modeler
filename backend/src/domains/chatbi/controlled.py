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
(columns/rows、list[dict]、裸 dict 单条记录)。信封形状但状态枚举
未知 = 契约错误(invalid_input),不交给模型补齐(设计 4.4/13.2)。

执行前按注册表原始 inputSchema 完整本地验参,禁止外部 ref;探测失败
保留能力快照供查看,但不得执行。身份注入由调用方负责。
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


# JSON Schema validation is entirely local; remote references never resolve.

def validate_against_schema(args: dict, schema) -> Optional[str]:
    """Validate the complete declared dialect and preserve readable legacy errors."""
    if schema is None:
        return None
    if not isinstance(schema, (dict, bool)):
        return "工具 inputSchema 格式无效"
    from jsonschema import Draft202012Validator, validators
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def no_remote(uri):
        raise NoSuchResource(ref=uri)

    def check_refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$ref", "$dynamicRef", "$recursiveRef") and (
                        not isinstance(value, str) or not value.startswith("#")):
                    raise ValueError("禁止远程或外部 JSON Schema ref")
                check_refs(value)
        elif isinstance(node, list):
            for value in node:
                check_refs(value)

    try:
        check_refs(schema)
        validator_cls = validators.validator_for(schema, default=Draft202012Validator)
        validator_cls.check_schema(schema)
        validator = validator_cls(schema, registry=Registry(retrieve=no_remote))
        error = next(validator.iter_errors(args), None)
        if error:
            if error.validator == "required":
                missing = next((n for n in error.validator_value if n not in error.instance), "")
                return f"缺少必填参数 {missing}"
            path = ".".join(map(str, error.absolute_path)) or "$"
            if error.validator == "enum":
                return f"参数 {path} 的值必须是: {error.validator_value}"
            return f"参数 {path} 校验失败: {error.message}"
        # Existing interactive callers treat blank required text as a missing slot.
        if isinstance(schema, dict) and isinstance(args, dict):
            for name in schema.get("required") or []:
                if isinstance(args.get(name), str) and not args[name].strip():
                    return f"缺少必填参数 {name}"
    except Exception as exc:
        return f"工具 inputSchema 无法本地校验: {str(exc)[:250]}"
    return None


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
    """Normalize named tables without dropping fields or guessing row widths."""
    if payload is None or payload == {}:
        return {"columns": [], "rows": []}
    if isinstance(payload, dict) and ("columns" in payload or "rows" in payload):
        if not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
            return None
        columns = payload["columns"]
        if not all(isinstance(c, str) for c in columns) or len(set(columns)) != len(columns):
            return None
        rows = []
        for row in payload["rows"]:
            if isinstance(row, dict):
                if set(row) - set(columns):
                    return None
                rows.append(tuple(row.get(c) for c in columns))
            elif isinstance(row, (list, tuple)) and len(row) == len(columns):
                rows.append(tuple(row))
            else:
                return None
        return {"columns": list(columns), "rows": rows}
    if isinstance(payload, list):
        if not payload:
            return {"columns": [], "rows": []}
        if all(isinstance(r, dict) for r in payload):
            columns = list(dict.fromkeys(k for r in payload for k in r))
            rows = [tuple(r.get(c) for c in columns) for r in payload]
            return {"columns": columns, "rows": rows}
        if any(isinstance(r, (dict, list, tuple)) for r in payload):
            return None
        return {"columns": ["value"], "rows": [(r,) for r in payload]}
    if isinstance(payload, dict):
        return {"record": dict(payload)}
    if isinstance(payload, (int, float, bool, str)):
        return {"columns": ["value"], "rows": [(payload,)]}
    return None




def _invoke_mcp_core(info, tool: str, args: dict, started: float,
                     source: Optional[dict] = None) -> "ControlledResult":
    """共享执行内核:Schema 验参 → SDK 传输 → 信封判定 → 归一。

    execute_controlled(客服受控入口)与 datasources.execute_mcp_tool
    (ask_data/switch_chart 交互入口)都走这里——设计 4.4"原 ask_data
    生成候选调用后也走同一执行内核,交互副作用留在外层":传输/信封/
    验参只有一份实现,信封不再被当成功数据行(此前 execute_mcp_tool
    的真实缺口)。
    """
    # Validate the registry's complete inputSchema before any remote execution.
    try:
        from services.mcp_registry import McpServerStore
        server = McpServerStore().get_by_code(info.host)
    except Exception:
        return _err("unavailable", "MCP 注册表暂不可读,拒绝执行", started, source)
    from sdk import mcp_client
    if server:
        execution_error = mcp_client.server_execution_error(server)
        if execution_error:
            return _err("unavailable", execution_error, started, source)
    snapshot = (server or {}).get("tools_snapshot") or []
    declaration = next((t for t in snapshot if t.get("name") == tool), None)
    if server and declaration is None:
        known = [t.get("name", "") for t in snapshot]
        return _err("invalid_input",
                    f"工具 {tool} 不在该 MCP 服务当前快照"
                    f"(现有: {', '.join(known[:8])})——请刷新数据源/语义层快照后重试",
                    started, source)
    tool_schema = declaration.get("inputSchema") if declaration else None
    schema_err = validate_against_schema(args, tool_schema)
    if schema_err:
        return _err("invalid_input", schema_err, started, source)

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
            if "data" not in payload:
                return _err("invalid_input", "业务信封 ok 缺少 data", started, source)
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
    # 信封形状(带 message/data 载荷键)但状态枚举未知 = 契约错误,
    # 不当成功记录、不交给模型补齐(设计 4.4 步 3/4,13.2 P1 必测项)。
    # 纯记录恰含普通 status 字段(不带载荷键)不受影响。
    if isinstance(payload, dict) and isinstance(payload.get("status"), str) \
            and payload["status"] not in ENVELOPE_STATUS \
            and ("data" in payload or "message" in payload):
        return _err("invalid_input",
                    f"上游返回未知业务状态 {payload['status']!r}(返回契约不符)",
                    started, source)
    normalized = _normalize_tabular(payload)
    if normalized is None:
        return _err("invalid_input",
                    "上游返回格式不符合表格契约(无列名数组不猜列名)",
                    started, source)
    return ControlledResult(ok=True, status="ok", data=normalized,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            source=source)


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

    # 数据源加载失败(PG 不可达等)→ unavailable,不把内部异常原文
    # (可能含连接串)发给用户(设计 7.5);MCP 传输只用 server code,
    # 不需要解密密码(decrypt=False)
    try:
        info = datasources.get_datasource(get_pack_db(), datasource_id,
                                          decrypt=False)
    except Exception:
        return _err("unavailable", "数据源服务暂不可用,请稍后重试", started)
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
    return _invoke_mcp_core(info, tool, args, started, source)
