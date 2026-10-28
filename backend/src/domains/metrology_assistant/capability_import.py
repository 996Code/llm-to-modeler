"""metrology 侧 MCP 能力导入(平台件 sdk/mcp_draft 的形状适配)。

职责边界:
  - 平台件给通用槽位与新增/失效清单(sdk/mcp_draft);
  - 本模块把通用槽位翻译成"场景表一行"的草稿(工具→数据源绑定、
    槽位→required、参数→params 映射),并提供库存视图(哪些工具已
    接入/可接入/绑定已失效);
  - 起草不落库,管理页润色后整档保存。
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
        "question": _ask_question(header, main["description"]),
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
    # 非主参数的提取提示随草案入库(_param_hints):润色保存后可选
    # 参数仍可提取——否则回到"多参数工具只带主参数出站"的旧缺口
    scenario = {
        "label": f"{desc}(MCP 导入草稿)",
        "intent": f"问{desc}相关问题(草稿,请润色为面向用户的意图描述)",
        "required": required,
        "action": action,
        "_param_hints": {s["name"]: s["description"]
                         for s in slots if s["name"] != main["name"]},
    }
    return {
        "key": tool,
        "scenario": scenario,
        # 其余参数槽位(非主槽位)一并列出供人工在文案中引导
        "extra_slots": [s["name"] for s in slots if s["name"] != main["name"]],
    }


def resolve_datasource_for_tool(server_code: str, tool: str) -> Optional[str]:
    """按 server+tool 找可执行数据源(限定该工具的固定源优先,次选该
    server 的通用源);找不到返回 None。

    自动路由的执行期解析——绑定不落场景表,数据源接入/更换后下一轮
    对话即生效,无需改配置。
    """
    from domains.chatbi import datasources, runtime
    try:
        infos = [i for i in datasources.list_datasources(
            runtime.get_pack_db(), active_only=True)
            if i.db_type == "mcp" and (i.host or "") == server_code]
    except Exception:  # noqa: BLE001
        logger.exception("自动路由:数据源清单读取失败")
        return None
    fixed = next((i for i in infos if (i.database or "") == tool), None)
    generic = next((i for i in infos if not (i.database or "").strip()), None)
    picked = fixed or generic or (infos[0] if infos else None)
    return picked.id if picked else None


# ── 自动路由(注册即识别,零发布)────────────────────────────────

def _servers_rows() -> List[dict]:
    """注册表原始行(含 updated_at,快照刷新判定用)。"""
    from services.mcp_registry import McpServerStore
    return [s for s in (McpServerStore().list_servers() or [])
            if s.get("code")]


# 快照 TTL(秒):超过则后台异步重探测,本轮仍用旧快照——对话零阻塞,
# MCP 上新接口后下一轮自动路由即覆盖,全程无人工刷新/发布
_SNAPSHOT_TTL = 60.0
_probe_inflight: set = set()


def _refresh_stale_snapshots(rows: List[dict]) -> None:
    """快照过期的 server 丢后台线程重探测(进程级去抖,失败只留痕)。"""
    import threading as _th
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    for s in rows:
        sid = s.get("id")
        if (not sid or not s.get("enabled", True)
                or sid in _probe_inflight):
            continue
        try:
            updated = _dt.fromisoformat(str(s.get("updated_at") or ""))
        except ValueError:
            continue  # 时间无法解析(测试替身行)不触发刷新
        if (now - updated).total_seconds() < _SNAPSHOT_TTL:
            continue

        def _run(sid=sid):
            _probe_inflight.add(sid)
            try:
                from services.mcp_registry import McpServerStore
                McpServerStore().apply_probe(sid)
            except Exception:  # noqa: BLE001
                logger.exception("自动路由:MCP 快照后台刷新失败(%s)", sid)
            finally:
                _probe_inflight.discard(sid)
        _th.Thread(target=_run, daemon=True,
                   name=f"mcp-snapshot-refresh-{sid[:8]}").start()


# ── 声明质量门禁 ──────────────────────────────────────────────
# 声明(description/参数说明)是自动路由的唯一语义来源,责任在 MCP 提供
# 方。描述说不清的工具进了路由也是死能力(LLM 不知道它是干嘛的,用户
# 永远问不到),还占提示词——所以门禁拦下,标记出来让提供方补声明,
# 而不是由管理员在润色场景里替业务方写描述。

def _tool_issues(decl: dict) -> List[str]:
    """工具声明体检:返回问题清单(空=通过)。

    - 工具描述缺失/过短(<6 字) → 拦截(不进路由)
    - required 参数无说明 → 告警(可路由,但追问文案会退化成参数名直出)
    """
    issues: List[str] = []
    desc = str(decl.get("description") or "").strip()
    if len(desc) < 6:
        issues.append("工具描述缺失或过短(不足 6 字)")
    schema = decl.get("inputSchema")
    if isinstance(schema, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            p = props.get(name)
            if isinstance(p, dict) and not str(p.get("description") or "").strip():
                issues.append(f"必填参数 {name} 无说明")
    return issues


def _blocking_issues(decl: dict) -> bool:
    """是否存在拦截级问题(工具描述缺失)。"""
    return len(str(decl.get("description") or "").strip()) < 6


def auto_scenarios(table: dict) -> Dict[str, dict]:
    """未编排 MCP 工具 → 运行时自动场景(注册即识别,零发布)。

    每轮对话现算:注册表快照中未被场景表编排的工具,按声明
    (description/inputSchema)合成为与人工场景同构的一行——提取/
    追问/归一/受控执行/通用表格卡全复用既有机器。MCP 上新接口
    无需起草/刷新/发布:快照超 TTL 后台自动重探测,下一轮即出现。
    跳过并留痕:已被场景表编排的工具(润色优先,按 action.tool 判定
    ——场景 key 与工具名不同,按 key 去重会漏)/声明不完整(描述
    缺失,提供方补声明后自动出现)/无可映射参数/暂无可执行数据源
    (接入数据源后自动出现)。
    """
    from sdk.mcp_draft import translate_input_schema
    rows = _servers_rows()
    _refresh_stale_snapshots(rows)
    edited_tools = {
        str((cfg.get("action") or {}).get("tool") or "")
        for cfg in table.values()
        if (cfg.get("action") or {}).get("kind") == "chatbi"}
    out: Dict[str, dict] = {}
    for s in rows:
        code = s["code"]
        for decl in (s.get("tools_snapshot") or []):
            name = decl.get("name") if isinstance(decl, dict) else None
            if not name:
                continue
            if (name in edited_tools or name in table or name in out):
                continue
            if _blocking_issues(decl):
                logger.info("自动路由拦截 %s/%s:声明不完整(描述缺失),"
                            "请业务平台补充 MCP 声明", code, name)
                continue
            slots = translate_input_schema(decl.get("inputSchema"))["slots"]
            if not slots:
                logger.debug("自动路由跳过 %s/%s:无可映射参数", code, name)
                continue
            if resolve_datasource_for_tool(code, name) is None:
                logger.debug("自动路由跳过 %s/%s:无可执行数据源", code, name)
                continue
            out[name] = _synthesize_auto(code, decl, slots)
    return out


def _ask_question(header: str, description: str) -> str:
    """主槽位追问文案:请提供{header}(如 示例)。描述与 header 同头时
    去重(映射后 header="器具类别"、description="器具类别(如 …"会叠词)。"""
    example = (description or "").strip()
    if header and example.startswith(header):
        trimmed = example[len(header):].lstrip("（(，, ：: ")
        if trimmed:
            example = trimmed
    # 描述以"如 …"举例开头时,与外层"(如 …)"格式叠字
    if example.startswith("如 ") or example.startswith("如："):
        example = example[1:].lstrip(" ：:")
    if not example:
        return f"请提供{header}"
    return f"请提供{header}(如 {example[:20]})"


def _synthesize_auto(server_code: str, decl: dict, slots: list) -> dict:
    """工具声明 → 场景行(与 draft_scenario 同构,但零人工介入)。

    主槽位(schema required 首个,缺省首个属性)做追问门;其余参数
    全量 identity 映射(执行期未提到的参数不传出站)。
    """
    main = next((s for s in slots if s["required"]), slots[0])
    desc = decl.get("description") or f"{decl['name']} 查询"
    header = _zh_label(main["name"]) or main["name"]
    required: Dict[str, Any] = {
        "slot": main["name"],
        "header": header,
        "question": _ask_question(header, main["description"]),
        "options": [],
        "extract_hint": (f"{main['name']}: "
                         + (main["description"] or "本轮明确提到的值")
                         + ";没提 → 空串"),
    }
    if main["slot_type"] != "text":
        required["slot_type"] = main["slot_type"]
    if main["slot_type"] == "enum":
        required["enum_options"] = main["enum_options"]
    return {
        "label": desc,
        "intent": f"问{desc}相关问题",
        "required": required,
        "action": {
            "kind": "chatbi",
            "server": server_code,   # 执行期解析数据源,不落绑定
            "datasource_id": "", "env": "",
            "tool": decl["name"],
            "params": {s["name"]: s["name"] for s in slots},
            "stage": f"调用 {decl['name']} 查询…",
            "error_noun": desc[:12],
            "notices": {},
            "card": "_table_card",
        },
        # 非主参数(schema 可选参数)的提取提示——提取提示词据此把
        # 用户说到的可选条件一并带出,执行期未提到的仍不传出站
        "_param_hints": {s["name"]: s["description"]
                         for s in slots if s["name"] != main["name"]},
        "_auto": True,
    }


def routing_overview() -> Dict[str, Any]:
    """自动路由总览(管理页主视图):每个能力的路由状态与执行解析。

    行来源 = 注册表快照全部 MCP 工具 + 场景表 kg 动作。chatbi 工具
    三态:polished(场景表 action.tool 命中,润色生效)/auto(运行时
    合成,含主槽位追问预览与执行期数据源解析)/blocked(暂不可执行:
    无可映射参数或无数据源——接入数据源后自动出现)。管理页据此
    渲染"哪些能力已可对话、哪些润色过、哪些被卡"。
    """
    from domains.metrology_assistant.scenario_config import load_scenarios
    from sdk.mcp_draft import translate_input_schema
    table = load_scenarios()
    rows = _servers_rows()
    _refresh_stale_snapshots(rows)
    polish_by_tool: Dict[str, str] = {}
    for key, cfg in table.items():
        action = cfg.get("action") or {}
        if action.get("kind") == "chatbi" and action.get("tool"):
            polish_by_tool[str(action["tool"])] = key

    # 数据源 id→名称(总览展示用,读一次全表)
    ds_names: Dict[str, str] = {}
    try:
        from domains.chatbi import datasources as _bi_ds, runtime as _bi_rt
        for info in _bi_ds.list_datasources(_bi_rt.get_pack_db(),
                                            active_only=True):
            ds_names[info.id] = info.name
    except Exception:  # noqa: BLE001
        logger.exception("路由总览:数据源名称读取失败(降级显示 id)")

    caps: List[dict] = []
    server_stats: Dict[str, Dict[str, int]] = {}
    for s in rows:
        code = s["code"]
        server_stats[code] = {"tools": 0, "incomplete": 0, "warnings": 0}
        for decl in (s.get("tools_snapshot") or []):
            if not (isinstance(decl, dict) and decl.get("name")):
                continue
            name = decl["name"]
            desc = decl.get("description") or ""
            # 画像的 tools 计全部快照工具;声明质量只对"走声明路由"的
            # 工具负责——已润色的路由文案来自场景表,声明缺失不算它的账
            server_stats[code]["tools"] += 1
            key = polish_by_tool.get(name)
            if key is None:
                if _blocking_issues(decl):
                    server_stats[code]["incomplete"] += 1
                elif _tool_issues(decl):
                    server_stats[code]["warnings"] += 1
            if key:
                cfg = table[key]
                req = cfg.get("required") or {}
                action = cfg.get("action") or {}
                caps.append({
                    "kind": "chatbi", "tool": name, "server": code,
                    "description": desc or cfg.get("label") or name,
                    "status": "polished", "scenario_key": key,
                    "label": cfg.get("label") or name,
                    "datasource": {
                        "datasource_id": str(action.get("datasource_id") or ""),
                        "datasource_name": ds_names.get(
                            str(action.get("datasource_id") or ""), ""),
                        "env": str(action.get("env") or ""),
                        "server": str(action.get("server") or "")},
                    "ask": {"slot": req.get("slot"),
                            "header": req.get("header"),
                            "question": req.get("question")},
                    "card": action.get("card"),
                })
                continue
            # 声明不完整(工具描述缺失):不进路由,红色标出让提供方补声明
            if _blocking_issues(decl):
                caps.append({"kind": "chatbi", "tool": name, "server": code,
                             "description": desc or "(无描述)",
                             "status": "incomplete",
                             "issues": _tool_issues(decl),
                             "scenario_key": None, "label": name,
                             "datasource": None, "ask": None, "card": None})
                continue
            slots = translate_input_schema(decl.get("inputSchema"))["slots"]
            if not slots:
                caps.append({"kind": "chatbi", "tool": name, "server": code,
                             "description": desc, "status": "blocked",
                             "blocked_reason": "声明无可映射参数"})
                continue
            ds = resolve_datasource_for_tool(code, name)
            if ds is None:
                caps.append({"kind": "chatbi", "tool": name, "server": code,
                             "description": desc, "status": "blocked",
                             "blocked_reason":
                             f"没有绑定到 MCP 服务「{code}」的启用数据源"})
                continue
            synth = _synthesize_auto(code, decl, slots)
            caps.append({
                "kind": "chatbi", "tool": name, "server": code,
                "description": desc or synth["label"],
                "status": "auto", "scenario_key": None,
                "issues": _tool_issues(decl) or None,
                "label": synth["label"],
                "datasource": {"datasource_id": ds,
                               "datasource_name": ds_names.get(ds, ""),
                               "env": "", "server": code},
                "ask": {"slot": synth["required"]["slot"],
                        "header": synth["required"]["header"],
                        "question": synth["required"]["question"]},
                "card": synth["action"]["card"],
            })
    # kg 能力(场景表中的知识库检索动作)
    for key, cfg in table.items():
        action = cfg.get("action") or {}
        if action.get("kind") == "kg":
            caps.append({
                "kind": "kg", "tool": None, "server": None,
                "description": cfg.get("label") or key,
                "status": "polished", "scenario_key": key,
                "label": cfg.get("label") or key,
                "datasource": None,
                "ask": {"slot": (cfg.get("required") or {}).get("slot"),
                        "header": (cfg.get("required") or {}).get("header"),
                        "question": (cfg.get("required") or {}).get("question")},
                "card": None,
            })
    # 服务画像:分组标题 + 声明质量汇总(缺服务描述/若干工具声明不完整
    # 都在这里可见,提醒补声明而不是去润色场景里修)
    servers_profile = []
    for s in rows:
        st = server_stats.get(s["code"]) or {"tools": 0, "incomplete": 0, "warnings": 0}
        profile = {
            "code": s["code"], "name": s.get("name") or s["code"],
            "description": s.get("description") or "",
            "tools": st["tools"], "incomplete": st["incomplete"],
            "warnings": st["warnings"], "enabled": s.get("enabled", True),
            "status": s.get("status") or "",
        }
        if not profile["description"]:
            profile["issues"] = ["服务描述未填写(建议说明该系统提供什么业务)"]
        servers_profile.append(profile)
    return {"capabilities": caps, "servers": servers_profile}


_ZH: Dict[str, str] = {
    "name": "名称", "instrument": "器具名称", "order_no": "委托单号",
    "date": "日期", "kb": "知识库", "model": "型号", "type": "类型",
    "code": "编号", "id": "编号", "amount": "数量", "period": "周期",
    "keyword": "关键词", "status": "状态", "title": "标题",
    "customer": "客户", "phone": "手机号", "category": "器具类别",
    "report_no": "报告号", "state": "状态",
}


def _zh_label(slot_name: str) -> Optional[str]:
    """槽位名→中文 header 常见映射(草稿用,人工可改)。"""
    if slot_name in _ZH:
        return _ZH[slot_name]
    if slot_name.endswith("_no"):
        return slot_name[:-3] + "编号"
    return None
