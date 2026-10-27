"""MCP 声明 → 编排配置的通用翻译层(平台件,零业务知识)。

给"编排型插件"(metrology_assistant 及未来的预约/报价等)复用:
  translate_input_schema: MCP 工具的 inputSchema → 通用槽位定义
    (required/type/enum 的机械映射,不含任何插件配置形状)
  capability_diff: MCP 快照工具集 vs 消费方已绑定集 → 新增/失效清单

插件侧只做两件事:把通用槽位映射到自己的配置形状;提供管理页确认
入库(人工把关对客话术——声明是外部输入,不能自动变成对客行为)。
"""
from typing import Any, Dict, List, Optional

# JSON Schema type → 平台槽位类型(编排器统一口径)
_TYPE_MAP = {
    "string": "text",
    "number": "number",
    "integer": "number",
    "boolean": "text",   # 布尔暂映射 text(没有 bool 槽位;追问选项承载)
}


def translate_input_schema(schema: Optional[dict]) -> Dict[str, Any]:
    """inputSchema → {slots, required_slot, summary}。

    slots: [{name, slot_type, enum_options, description, required}]
    (首见序;required 属性排在前面更利于起草器选主槽位)。
    schema 非法/为空返回空 slots——起草器自行兜底。
    """
    if not isinstance(schema, dict):
        return {"slots": [], "required_slot": None, "summary": ""}
    props = schema.get("properties") or {}
    required = [str(r) for r in schema.get("required") or []]
    slots: List[Dict[str, Any]] = []
    for name in list(required) + [k for k in props if k not in required]:
        p = props.get(name)
        if not isinstance(p, dict):
            continue
        slot_type = _TYPE_MAP.get(str(p.get("type") or ""), "text")
        enum_options: List[str] = []
        if slot_type == "text" and isinstance(p.get("enum"), list) and p["enum"]:
            slot_type = "enum"
            enum_options = [str(v) for v in p["enum"]]
        slots.append({
            "name": str(name),
            "slot_type": slot_type,
            "enum_options": enum_options,
            "description": str(p.get("description") or ""),
            "required": name in required,
        })
    return {
        "slots": slots,
        "required_slot": required[0] if required else None,
        "summary": str(schema.get("description") or ""),
    }


def capability_diff(snapshots: Dict[str, List[str]],
                    bindings: List[tuple]) -> Dict[str, list]:
    """快照工具集 vs 消费方绑定集 → 新增能力/失效绑定。

    snapshots: {server_code: [tool_name,...]}(调用方从注册表快照取)
    bindings:  消费方已绑定的 (server_code, tool_name) 列表
    返回 {new: [(server, tool)], stale: [(server, tool)]}——
    new=快照有而无人绑定(可接入);stale=绑定了但快照已没有(要处理)。
    """
    bound = {(str(s), str(t)) for s, t in bindings}
    in_snapshot = {(server, tool)
                   for server, tools in snapshots.items() for tool in tools}
    return {
        "new": sorted(in_snapshot - bound),
        "stale": sorted(bound & (bound - in_snapshot)),
    }
