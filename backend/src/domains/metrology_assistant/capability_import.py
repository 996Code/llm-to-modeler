"""metrology 侧 MCP 能力导入(平台件 sdk/mcp_draft 的形状适配)。

职责边界:
  - 平台件给通用槽位与新增/失效清单(sdk/mcp_draft);
  - 本模块把通用槽位翻译成"场景表一行"的草稿(工具→数据源绑定、
    槽位→required、参数→params 映射),并提供库存视图(哪些工具已
    接入/可接入/绑定已失效);
  - 起草不落库——管理页确认后走既有整档保存(revision CAS),
    人工润色对客话术是设计要求,不是流程负担。
"""
import logging
from typing import Any, Dict, List, Optional

from sdk.mcp_draft import capability_diff, translate_input_schema

logger = logging.getLogger(__name__)


def _snapshots() -> Dict[str, List[dict]]:
    """注册表全量快照: {server_code: [{name, inputSchema, description}]}。"""
    from services.mcp_registry import McpServerStore
    servers = McpServerStore().list_servers()
    out: Dict[str, List[dict]] = {}
    for s in servers or []:
        code = s.get("code") or ""
        if not code:
            continue
        tools = []
        for t in s.get("tools_snapshot") or []:
            if t.get("name"):
                tools.append({"name": t["name"],
                              "inputSchema": t.get("inputSchema"),
                              "description": t.get("description") or ""})
        if tools:
            out[code] = tools
    return out


def _scenario_bindings(table: dict) -> List[tuple]:
    """场景表已绑定的 (server, tool) 集(chatbi 动作;server 经数据源)。"""
    from domains.chatbi import datasources, runtime
    host_by_ds = {}
    try:
        for info in datasources.list_datasources(runtime.get_pack_db(),
                                                 active_only=True):
            host_by_ds[info.id] = (info.host or "") if info.db_type == "mcp" else ""
    except Exception:  # noqa: BLE001
        logger.exception("能力导入:数据源清单读取失败")
    import os
    bindings = []
    for cfg in table.values():
        action = cfg.get("action") or {}
        if action.get("kind") != "chatbi":
            continue
        # 显式 datasource_id 优先;env 绑定解析 env 变量拿缺省 ID
        ds = str(action.get("datasource_id") or "").strip() \
            or os.getenv(str(action.get("env") or ""), "").strip()
        server = host_by_ds.get(ds, "") if ds else ""
        if server:
            bindings.append((server, action.get("tool") or ""))
    return bindings


def inventory() -> Dict[str, Any]:
    """能力库存 + 发现 diff(管理页向导数据面)。

    返回 {servers: [{code, tools: [{name, description, imported,
    draftable, reason}]}], diff: {new, stale}}。
    """
    snaps = _snapshots()
    from domains.metrology_assistant.scenario_config import load_scenarios
    bindings = set(_scenario_bindings(load_scenarios()))
    diff = capability_diff(
        {code: [t["name"] for t in tools] for code, tools in snaps.items()},
        sorted(bindings))
    servers = []
    for code, tools in snaps.items():
        rows = []
        for t in tools:
            imported = (code, t["name"]) in bindings
            rows.append({
                "name": t["name"], "description": t.get("description") or "",
                "imported": imported,
                "draftable": not imported and bool(t["inputSchema"]),
            })
        servers.append({"code": code, "tools": rows})
    return {"servers": servers, "diff": diff}


def draft_scenario(server_code: str, tool: str) -> Dict[str, Any]:
    """MCP 工具声明 → 场景表草稿(不落库;管理页润色后整档保存)。

    绑定数据源:绑定到该 server 的 MCP 数据源中优先"限定工具==目标
    工具"的固定工具源,否则第一个启用源;找不到时报错(先去数据源页接)。
    """
    snaps = _snapshots()
    tools = snaps.get(server_code) or []
    decl = next((t for t in tools if t["name"] == tool), None)
    if decl is None:
        raise ValueError(f"MCP 服务 {server_code} 快照中没有工具 {tool}")

    from domains.chatbi import datasources, runtime
    ds_id: Optional[str] = None
    try:
        infos = [i for i in datasources.list_datasources(
            runtime.get_pack_db(), active_only=True)
            if i.db_type == "mcp" and (i.host or "") == server_code]
        fixed = next((i for i in infos if (i.database or "") == tool), None)
        generic = next((i for i in infos if not (i.database or "").strip()), None)
        picked = fixed or generic or (infos[0] if infos else None)
        ds_id = picked.id if picked else None
    except Exception:  # noqa: BLE001
        logger.exception("能力导入:数据源解析失败")

    translated = translate_input_schema(decl["inputSchema"])
    slots = translated["slots"]
    if not slots:
        raise ValueError(f"工具 {tool} 的 inputSchema 没有可映射参数")
    # 主槽位 = 首个 required(无 required 取首个属性)
    main = next((s for s in slots if s["required"]), slots[0])
    params = {s["name"]: s["name"] for s in slots}

    desc = decl.get("description") or f"{tool} 查询"
    header = _zh_label(main["name"]) or main["name"]
    required: Dict[str, Any] = {
        "slot": main["name"],
        "header": header,
        "question": (f"请提供{header}"
                     + (f"(如 {main['description'][:20]})"
                        if main["description"] else "")),
        "options": [],
        "extract_hint": (f"{main['name']}: "
                         + (main["description"] or "本轮明确提到的值")
                         + ";没提 → 空串"),
    }
    if main["slot_type"] != "text":
        required["slot_type"] = main["slot_type"]
    if main["slot_type"] == "enum":
        required["enum_options"] = main["enum_options"]

    if ds_id is None:
        raise ValueError(
            f"没有绑定到 MCP 服务「{server_code}」的启用数据源——"
            "请先在智能问数的数据源页接入该 server(可建固定工具源)"
            "再导入能力")
    action: Dict[str, Any] = {
        "kind": "chatbi",
        "datasource_id": ds_id,
        "env": "",  # 显式绑定已入库;env 缺省留空(校验规则二者其一即可)
        "tool": tool,
        "params": params,
        "stage": f"调用 {tool} 查询…",
        "error_noun": desc[:12],
        "notices": {},
        "card": "_table_card",
    }
    return {
        "key": tool,
        "scenario": {
            "label": f"{desc}(MCP 导入草稿)",
            "intent": f"问{desc}相关问题(草稿,请润色为面向用户的意图描述)",
            "required": required,
            "action": action,
        },
        # 其余参数槽位(非主槽位)一并列出供人工在文案中引导
        "extra_slots": [s["name"] for s in slots if s["name"] != main["name"]],
    }


_ZH: Dict[str, str] = {
    "name": "名称", "instrument": "器具名称", "order_no": "委托单号",
    "date": "日期", "kb": "知识库", "model": "型号", "type": "类型",
    "code": "编号", "id": "编号", "amount": "数量", "period": "周期",
}


def _zh_label(slot_name: str) -> Optional[str]:
    """槽位名→中文 header 常见映射(草稿用,人工可改)。"""
    if slot_name in _ZH:
        return _ZH[slot_name]
    if slot_name.endswith("_no"):
        return slot_name[:-3] + "编号"
    return None
