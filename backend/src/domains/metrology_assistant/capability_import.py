"""MCP 声明目录、能力覆盖绑定及客服配置投影。"""
import copy
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from sdk.mcp_draft import capability_diff, translate_input_schema

logger = logging.getLogger(__name__)
_SNAPSHOT_TTL = 60.0
_probe_inflight: set = set()
_probe_lock = threading.Lock()


def capability_key(server: str, tool: str) -> str:
    return f"{server}::{tool}"


def _servers_rows() -> List[dict]:
    from services.mcp_registry import McpServerStore
    return [s for s in (McpServerStore().list_servers() or []) if s.get("code")]


def _available(server: dict) -> bool:
    return bool(server.get("enabled", True)) and server.get("status", "connected") == "connected" \
        and server.get("probe_status") not in ("failed", "error")


def _snapshots() -> Dict[str, List[dict]]:
    return {s["code"]: copy.deepcopy(s.get("tools_snapshot") or [])
            for s in _servers_rows() if _available(s)}


def _action_identity(action: dict, hosts: Optional[dict] = None) -> tuple:
    server = str(action.get("server") or "").strip()
    ds = str(action.get("datasource_id") or "").strip() \
        or os.getenv(str(action.get("env") or ""), "").strip()
    if not server and ds:
        if hosts is None:
            from domains.chatbi import datasources, runtime
            try:
                hosts = {i.id: i.host for i in datasources.list_datasources(
                    runtime.get_pack_db(), active_only=True) if i.db_type == "mcp"}
            except Exception:
                logger.exception("能力绑定数据源读取失败")
                hosts = {}
        server = str(hosts.get(ds) or "")
    return server, str(action.get("tool") or "")


def _scenario_bindings(table: dict) -> List[tuple]:
    return [identity for cfg in table.values()
            if (cfg.get("action") or {}).get("kind") == "chatbi"
            for identity in [_action_identity(cfg["action"])] if all(identity)]


def inventory() -> Dict[str, Any]:
    from domains.metrology_assistant.scenario_config import load_scenarios
    snaps = _snapshots()
    bindings = set(_scenario_bindings(load_scenarios()))
    return {"servers": [{"code": code, "tools": [{
        "name": t["name"], "description": t.get("description") or "",
        "imported": (code, t["name"]) in bindings,
        "draftable": isinstance(t.get("inputSchema"), dict)}
        for t in tools if t.get("name")]} for code, tools in snaps.items()],
        "diff": capability_diff({code: [t["name"] for t in tools if t.get("name")]
                                  for code, tools in snaps.items()}, sorted(bindings))}


def resolve_datasource_for_tool(server_code: str, tool: str) -> Optional[str]:
    from domains.chatbi import datasources, runtime
    try:
        infos = [i for i in datasources.list_datasources(runtime.get_pack_db(), active_only=True)
                 if i.db_type == "mcp" and (i.host or "") == server_code]
    except Exception:
        logger.exception("数据源清单读取失败")
        return None
    fixed = [i for i in infos if (i.database or "").strip() == tool]
    generic = [i for i in infos if not (i.database or "").strip()]
    candidates = fixed or generic
    return candidates[0].id if len(candidates) == 1 else None


def _refresh_stale_snapshots(rows: List[dict]) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for server in rows:
        sid = server.get("id")
        if not sid or not server.get("enabled", True):
            continue
        try:
            updated = datetime.fromisoformat(str(server.get("updated_at") or ""))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (now - updated).total_seconds() < _SNAPSHOT_TTL:
                continue
        except ValueError:
            continue
        with _probe_lock:
            if sid in _probe_inflight:
                continue
            _probe_inflight.add(sid)

        def run(sid=sid):
            try:
                from services.mcp_registry import McpServerStore
                McpServerStore().apply_probe(sid)
            except Exception:
                logger.exception("MCP 快照后台刷新失败(%s)", sid)
            finally:
                with _probe_lock:
                    _probe_inflight.discard(sid)
        threading.Thread(target=run, daemon=True, name=f"mcp-refresh-{sid[:8]}").start()


def refresh_snapshots() -> None:
    from services.mcp_registry import McpServerStore
    store = McpServerStore()
    for server in _servers_rows():
        if server.get("enabled", True):
            store.apply_probe(server["id"])


def _tool_issues(decl: dict) -> List[str]:
    issues = []
    if len(str(decl.get("description") or "").strip()) < 6:
        issues.append("工具描述缺失或过短(不足 6 字)")
    schema = decl.get("inputSchema")
    if not isinstance(schema, dict):
        issues.append("工具 inputSchema 必须是对象")
    else:
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            prop = props.get(name)
            if isinstance(prop, dict) and not str(prop.get("description") or "").strip():
                issues.append(f"必填参数 {name} 无说明")
    return issues


def _blocking_issues(decl: dict) -> bool:
    if len(str(decl.get("description") or "").strip()) < 6:
        return True
    schema = decl.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return True
    from jsonschema.validators import validator_for
    try:
        validator_for(schema).check_schema(schema)
    except Exception:
        return True
    return False


def _ask_question(header: str, description: str) -> str:
    desc = (description or "").strip()
    return f"请提供{header}" + (f"（{desc[:100]}）" if desc and desc != header else "")


def _synthesize_auto(server_code: str, decl: dict, slots: list) -> dict:
    schema = copy.deepcopy(decl.get("inputSchema") or {"type": "object", "properties": {}})
    main = next((slot for slot in slots if slot["required"]), None)
    required = {}
    if main:
        prop = (schema.get("properties") or {}).get(main["name"], {})
        header = prop.get("title") or main["name"]
        required = {"slot": main["name"], "header": header,
                    "question": _ask_question(header, main["description"]), "options": [],
                    "extract_hint": f"{main['name']}: {main['description']}",
                    "slot_type": main["slot_type"]}
        if main.get("enum_options"):
            required["enum_options"] = main["enum_options"]
    desc = decl.get("description") or decl["name"]
    return {"label": decl.get("title") or desc, "intent": desc,
            "required": required,
            "action": {"kind": "chatbi", "server": server_code,
                       "datasource_id": "", "env": "", "tool": decl["name"],
                       "params": {slot["name"]: slot["name"] for slot in slots},
                       "input_schema": schema,
                       "output_schema": copy.deepcopy(decl.get("outputSchema")),
                       "annotations": copy.deepcopy(decl.get("annotations") or {}),
                       "stage": f"调用 {decl['name']}…", "error_noun": "业务接口",
                       "notices": {}, "card": "_table_card"},
            "_param_hints": {slot["name"]: slot["description"] for slot in slots},
            "_auto": True}


def draft_scenario(server_code: str, tool: str) -> Dict[str, Any]:
    decl = next((t for t in _snapshots().get(server_code, []) if t.get("name") == tool), None)
    if decl is None:
        raise ValueError(f"MCP 服务 {server_code} 快照中没有工具 {tool}")
    if _blocking_issues(decl):
        raise ValueError("工具声明不完整或参数 schema 非法，请先修正 MCP 声明")
    ds_id = resolve_datasource_for_tool(server_code, tool)
    if not ds_id:
        raise ValueError(f"没有唯一可执行的 MCP 数据源绑定：{server_code}/{tool}")
    slots = translate_input_schema(decl.get("inputSchema"))["slots"]
    row = _synthesize_auto(server_code, decl, slots)
    row.pop("_auto", None)
    row["action"]["datasource_id"] = ds_id
    return {"key": capability_key(server_code, tool), "scenario": row,
            "extra_slots": [slot["name"] for slot in slots
                            if slot["name"] != row["required"].get("slot")]}


def resolve_scenarios(table: dict) -> Dict[str, dict]:
    """覆盖只替换话术和映射，工具参数契约始终使用已绑定服务的当前声明。"""
    rows = _servers_rows()
    servers = {s["code"]: s for s in rows}
    out = {}
    for key, original in table.items():
        cfg = copy.deepcopy(original)
        action = cfg.get("action") or {}
        if action.get("kind") != "chatbi":
            out[key] = cfg
            continue
        server, tool = _action_identity(action)
        if not server:
            # 旧配置未绑定时保留为显式执行错误，不根据同名工具猜业务域。
            out[key] = cfg
            continue
        row = servers.get(server)
        if row is None or not _available(row):
            continue
        decl = next((t for t in row.get("tools_snapshot") or []
                     if isinstance(t, dict) and t.get("name") == tool), None)
        if decl is None or not isinstance(decl.get("inputSchema"), dict):
            continue
        action["server"] = server
        action["input_schema"] = copy.deepcopy(decl["inputSchema"])
        action["output_schema"] = copy.deepcopy(decl.get("outputSchema"))
        action["annotations"] = copy.deepcopy(decl.get("annotations") or {})
        params = action.setdefault("params", {})
        hints = cfg.setdefault("_param_hints", {})
        for slot in translate_input_schema(decl["inputSchema"])["slots"]:
            params.setdefault(slot["name"], slot["name"])
            hints.setdefault(params[slot["name"]], slot["description"])
        cfg["action"] = action
        out[key] = cfg
    return out


def auto_scenarios(table: dict) -> Dict[str, dict]:
    rows = _servers_rows()
    _refresh_stale_snapshots(rows)
    bound = set(_scenario_bindings(table))
    out = {}
    for server in rows:
        if not _available(server):
            continue
        for decl in server.get("tools_snapshot") or []:
            if not isinstance(decl, dict) or not decl.get("name"):
                continue
            tool = decl["name"]
            if (server["code"], tool) in bound or _blocking_issues(decl):
                continue
            if not resolve_datasource_for_tool(server["code"], tool):
                continue
            key = capability_key(server["code"], tool)
            if key in table:
                continue
            slots = translate_input_schema(decl.get("inputSchema"))["slots"]
            out[key] = _synthesize_auto(server["code"], decl, slots)
    return out


def routing_overview() -> Dict[str, Any]:
    from domains.metrology_assistant.scenario_config import load_scenarios
    table = load_scenarios()
    rows = _servers_rows()
    _refresh_stale_snapshots(rows)
    overrides = {_action_identity(cfg["action"]): (key, cfg) for key, cfg in table.items()
                 if (cfg.get("action") or {}).get("kind") == "chatbi"}
    ds_names = {}
    try:
        from domains.chatbi import datasources, runtime
        ds_names = {i.id: i.name for i in datasources.list_datasources(runtime.get_pack_db(), active_only=True)}
    except Exception:
        logger.exception("路由总览数据源名称读取失败")
    caps, profiles = [], []
    for server in rows:
        code = server["code"]
        profile = {"code": code, "name": server.get("name") or code,
                   "description": server.get("description") or "", "tools": 0,
                   "incomplete": 0, "warnings": 0, "enabled": server.get("enabled", True),
                   "status": server.get("status") or ""}
        if not profile["description"]:
            profile["issues"] = ["服务描述未填写(建议说明该系统提供什么业务)"]
        for decl in server.get("tools_snapshot") or []:
            if not isinstance(decl, dict) or not decl.get("name"):
                continue
            tool = decl["name"]
            profile["tools"] += 1
            issues = _tool_issues(decl)
            override = overrides.get((code, tool))
            # override 有显式绑定时按其自身解析(强校验),不要求自动
            # 解析的唯一性——多通用源场景里显式 datasource_id 就是消歧
            if override:
                from domains.metrology_assistant.capability_plan import (
                    resolve_datasource as _resolve_plan_ds)
                try:
                    ds_id = str(_resolve_plan_ds({
                        "datasource_id": override[1]["action"].get("datasource_id") or "",
                        "datasource_env": override[1]["action"].get("env") or "",
                        "server": code, "tool": tool}) or "")
                except Exception:  # noqa: BLE001
                    logger.exception("路由总览:覆盖绑定数据源校验失败")
                    ds_id = ""
            else:
                ds_id = resolve_datasource_for_tool(code, tool)
            status, reason = "auto", ""
            if not _available(server):
                status, reason = "blocked", "服务已停用或探测未成功"
            elif not override and _blocking_issues(decl):
                status, reason = "incomplete", "声明不完整或 schema 非法"
                profile["incomplete"] += 1
            elif not ds_id:
                status, reason = "blocked", "没有唯一匹配的启用数据源"
            elif override:
                status = "polished"
            if not override and issues and status != "incomplete":
                profile["warnings"] += 1
            key, cfg = override or (None, _synthesize_auto(
                code, decl, translate_input_schema(decl.get("inputSchema"))["slots"]))
            req, action = cfg.get("required") or {}, cfg["action"]
            bound_ds = action.get("datasource_id") or ds_id or ""
            caps.append({"kind": "chatbi", "tool": tool, "server": code,
                         "capability_id": capability_key(code, tool),
                         "description": decl.get("description") or cfg.get("label") or tool,
                         "status": status, "blocked_reason": reason or None,
                         "issues": issues or None, "scenario_key": key,
                         "label": cfg.get("label") or tool,
                         "datasource": {"datasource_id": bound_ds,
                                        "datasource_name": ds_names.get(bound_ds, ""),
                                        "env": action.get("env") or "", "server": code},
                         "ask": {k: req.get(k) for k in ("slot", "header", "question")} if req else None,
                         "input_schema": copy.deepcopy(decl.get("inputSchema")),
                         "card": action.get("card")})
        profiles.append(profile)
    for key, cfg in table.items():
        action = cfg.get("action") or {}
        if action.get("kind") == "kg":
            req = cfg.get("required") or {}
            caps.append({"kind": "kg", "tool": None, "server": None,
                         "description": cfg.get("label") or key, "status": "polished",
                         "scenario_key": key, "label": cfg.get("label") or key,
                         "datasource": None,
                         "ask": {k: req.get(k) for k in ("slot", "header", "question")} if req else None,
                         "card": None, "kb": action.get("kb"),
                         "answer_mode": action.get("conclusion", "answer")})
    return {"capabilities": caps, "servers": profiles}


def _zh_label(slot_name: str) -> Optional[str]:
    return None
