"""BPM draft 共用管线逻辑：宿主身份注入 / 机械修复 / 规则合并 / 字段目录蒸馏。

被 generate_process 使用;所有函数纯操作 dict,
不触网络(网络在工具步骤里经 service_locator.get_api())。
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from domains.njmind_bpm import keys as K

logger = logging.getLogger(__name__)

MAX_MECHANICAL_ROUNDS = 4


def inject_host_identity(draft: Dict[str, Any],
                         host_process_key: Optional[str],
                         host_process_name: Optional[str],
                         host_form_key: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """F1:剥离 LLM 可能自造的 processKey/processName,注入宿主值。

    formKey 同理(真实事故:LLM 从模板示例学到 generic_form 直接输出,V5 校验
    FORM_NOT_FOUND 反复拦截耗尽重试)——宿主有值一律覆盖,LLM 值不可信。
    返回 (draft, error);宿主流程标识缺失 → error 非空(fail-closed)。
    """
    draft = dict(draft or {})
    draft.pop(K.PROCESS_KEY, None)   # LLM 自造值一律丢弃,不信
    draft.pop(K.PROCESS_NAME, None)
    if not host_process_key or not host_process_name:
        return draft, ("宿主上下文缺少流程标识(processKey/processName),"
                       "已阻止渲染——请在流程设计器内打开 AI 助手后重试")
    draft[K.PROCESS_KEY] = host_process_key
    draft[K.PROCESS_NAME] = host_process_name
    if host_form_key:
        draft[K.FORM_KEY] = host_form_key
    return draft, None


def mechanical_repair(draft: Dict[str, Any], errors: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """对 render 校验 errors 做确定性修复(≤4 轮由调用方控制)。

    当前覆盖:
    - key 悬空(RULE_TARGET_NOT_FOUND 类):按节点名精确匹配回填
    - 未知顶层/规则字段:剥除(schema 投影的 BPM 版)
    返回 (repaired_draft, remaining_errors)。remaining 逐条对应:
    只剔除真正被修掉的 RULE_TARGET_NOT_FOUND(按 field 路径定位到
    具体规则条目),修不掉的保留——否则过宽清除会让未修复错误
    一并消失,管线在 pass=False 时误判成功。
    """
    import copy
    d = copy.deepcopy(draft)

    # 前置类型归一(反序列化级别的错,Java 侧直接整包拒绝,连 validation 都不返回,
    # 必须在 render 前修):logic 接受 AND/OR 或数字字符串,统一转 int(10=AND,20=OR);
    # 字符串数字字段(logicType/approveOptRange 等)转 int。
    _LOGIC_MAP = {"AND": 10, "OR": 20}
    for br in d.get(K.BRANCH_RULES, []) or []:
        lv = br.get("logic")
        if isinstance(lv, str):
            br["logic"] = _LOGIC_MAP.get(lv.upper(), int(lv) if lv.isdigit() else 10)
    for rk in (K.APPROVE_RULES, K.CC_RULES):
        for r in d.get(rk, []) or []:
            for fk in ("approveOptRange", "multiApproveMethod"):
                fv = r.get(fk)
                if isinstance(fv, str) and fv.strip().isdigit():
                    r[fk] = int(fv)
    for br in d.get(K.BRANCH_RULES, []) or []:
        for cond in (br.get("rules") or []) + (br.get("roleRules") or []):
            lt = cond.get("logicType")
            if isinstance(lt, str) and lt.strip().isdigit():
                cond["logicType"] = int(lt)

    name_to_key = {}
    topo = d.get(K.TOPOLOGY) or {}
    for n in topo.get(K.NODES, []):
        name_to_key.setdefault(n.get(K.NODE_NAME, ""), n.get(K.NODE_KEY, ""))
    keyset = {n.get(K.NODE_KEY, "") for n in topo.get(K.NODES, [])}

    allowed_top = {K.TOPOLOGY, K.FORM_KEY, K.PROCESS_KEY, K.PROCESS_NAME,
                   K.APPROVE_RULES, K.CC_RULES, K.BRANCH_RULES}
    for k in list(d.keys()):
        if k not in allowed_top:
            d.pop(k)

    def _rule_at(field: str) -> Optional[Dict[str, Any]]:
        """从错误 field 路径(如 approveRules[0].nodeKey)定位具体规则条目。"""
        import re
        m = re.match(r"^(approveRules|ccRules)\[(\d+)\]", field or "")
        if not m:
            return None
        rules = d.get(m.group(1)) or []
        idx = int(m.group(2))
        return rules[idx] if idx < len(rules) else None

    def _fix_ref(rule: Dict[str, Any], ref_field: str, valid: set,
                 by_name: Dict[str, str]) -> bool:
        cur = rule.get(ref_field)
        if cur in valid:
            return False
        # 按名称精确匹配回填(规则里常带 nodeName 便于人读)
        nm = rule.get("nodeName") or rule.get(K.NODE_NAME, "")
        hit = by_name.get(nm)
        if hit:
            rule[ref_field] = hit
            return True
        return False

    fixed_fields = set()   # 修好的错误 field 路径(逐条对应,不整体清除)
    for e in errors:
        if e.get("code") != "RULE_TARGET_NOT_FOUND":
            continue
        rule = _rule_at(e.get("field", ""))
        if rule is None:
            continue
        if _fix_ref(rule, K.NODE_KEY_REF, keyset, name_to_key):
            fixed_fields.add(e.get("field", ""))

    # 操作符兼容性降级:字段类型与操作符不匹配(如部门字段配"大于")时,
    # 按校验器给出的 allowedValues(该字段类型兼容操作符)取首个兼容项替换,
    # 不丢条件、不重生成(LLM 重生成常把 branchRules 整个丢掉,代价更高)
    import re as _re
    _fixed_operator_fields = set()
    for e in errors:
        if e.get("code") != "FIELD_OPERATOR_INCOMPATIBLE":
            continue
        m = _re.match(r"^branchRules\[(\d+)\]\.rules\[(\d+)\]\.logicType$",
                      e.get("field") or "")
        allowed = e.get("allowedValues") or []
        if not m or not allowed:
            continue
        brs = d.get(K.BRANCH_RULES) or []
        bi, ri = int(m.group(1)), int(m.group(2))
        if bi < len(brs) and ri < len(brs[bi].get("rules") or []):
            cond = brs[bi]["rules"][ri]
            try:
                cond["logicType"] = int(str(allowed[0]))
                _fixed_operator_fields.add(e.get("field", ""))
            except (TypeError, ValueError):
                pass

    for rule in d.get(K.BRANCH_RULES, []) or []:
        unknown = [k for k in rule if k not in
                   {K.EDGE_KEY_REF, "logic", "rules", "roleRules", "nodeName"}]
        for k in unknown:
            rule.pop(k)

    remaining = [e for e in errors
                 if e.get("field", "") not in fixed_fields
                 and e.get("field", "") not in _fixed_operator_fields]
    return d, remaining


def bridge_host_keys(existing_topology: Optional[Dict[str, Any]],
                     new_topology: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """C-1:宿主规则行键(BPMN id:taskId/nodeId/sequenceFlowId)→新草稿语义 key 的桥接表。

    宿主 GET_CONTEXT 的 rules 是最终落库形态(键=BPMN id),新草稿规则键是
    LLM 重造的语义 key,两域唯一稳定公共轴是节点/连线名称:
    旧拓扑(同一画布 parse 产物)id→name,新拓扑 name→语义 key。
    名称在任一侧缺失或不唯一 → 不桥接(该行不继承明细,宁缺勿错)。

    返回 {"approve"/"cc": {bpmn_id: node_key}, "branch": {bpmn_id: edge_key}}。
    """
    def _by_name(topo, kind):
        names = {}
        items = (topo or {}).get(kind, []) or []
        for it in items:
            nm = it.get(K.NODE_NAME)
            if not nm:
                continue
            names.setdefault(nm, []).append(it.get(K.NODE_KEY))
        # 名称不唯一(多个同 key)即不可信
        return {nm: ks[0] for nm, ks in names.items() if len(ks) == 1 and ks[0]}

    old_nodes = _by_name(existing_topology, K.NODES)
    new_nodes = _by_name(new_topology, K.NODES)
    old_edges = _by_name(existing_topology, K.EDGES)
    new_edges = _by_name(new_topology, K.EDGES)
    node_bridge = {oid: new_nodes[nm] for oid, nm in
                   {n.get(K.NODE_KEY): n.get(K.NODE_NAME)
                    for n in (existing_topology or {}).get(K.NODES, []) or []}.items()
                   if nm in old_nodes and nm in new_nodes}
    edge_bridge = {oid: new_edges[nm] for oid, nm in
                   {e.get(K.NODE_KEY): e.get(K.NODE_NAME)
                    for e in (existing_topology or {}).get(K.EDGES, []) or []}.items()
                   if nm in old_edges and nm in new_edges}
    # old_nodes/old_edges 仅用于双侧唯一性确认,桥接表按 id→语义 key 输出
    return {"approve": node_bridge, "cc": node_bridge, "branch": edge_bridge}


def _host_rows_to_draft_domain(rows: List[Dict[str, Any]],
                               key_field: str) -> List[Dict[str, Any]]:
    """宿主最终形态行 → 草稿域行:键字段改名(taskId/nodeId→nodeKey 等),明细字段原样保留。"""
    out = []
    for r in (rows or []):
        host_key = r.get(key_field)
        if not host_key:
            continue
        item = {k: v for k, v in r.items() if k != key_field}
        item["nodeKey" if key_field in ("taskId", "nodeId") else "edgeKey"] = host_key
        if key_field == "taskId" and "mutexTaskIds" in item:
            item["mutexTaskKeys"] = item.pop("mutexTaskIds")
        out.append(item)
    return out


def merge_rule_details(new_rules: Dict[str, Any],
                       host_rules: Dict[str, Any],
                       host_topology: Optional[Dict[str, Any]] = None,
                       new_topology: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """I2:以宿主规则明细为基线合并 LLM 增量。

    宿主 rules 是最终落库形态(键=taskId/nodeId/sequenceFlowId,即 BPMN id)。
    提供旧/新拓扑时先按名称桥接翻译(见 bridge_host_keys),再进语义键域合并;
    桥接不上(名称缺失/不唯一/全新节点)的宿主行不继承——宁缺勿错。

    已知限制(整体重造语义下的开放项):merge 无法区分"LLM 没提到"与
    "LLM 提到但故意删了",当前默认继承优先,显式删节点类需求需用户在
    面板二次确认。

    - approveRules/ccRules:按 nodeKey 对齐,LLM 未提及的 key 整条继承宿主;
      提及的 key 以 LLM 版本为主(但宿主明细里 LLM 未覆盖的属性级字段回填)。
    - branchRules:按 edgeKey 对齐,同上。
    - formKey:LLM 版本优先,缺省继承。
    """
    bridge = bridge_host_keys(host_topology, new_topology)
    host_approve = _host_rows_to_draft_domain(
        (host_rules or {}).get(K.APPROVE_RULES), K.TASK_ID)
    host_cc = _host_rows_to_draft_domain(
        (host_rules or {}).get(K.CC_RULES), "nodeId")
    host_branch = _host_rows_to_draft_domain(
        (host_rules or {}).get(K.BRANCH_RULES), K.SEQUENCE_FLOW_ID)

    def _apply_bridge(rows, bridge_map):
        """宿主行键(BPMN id)经桥接表改写为新草稿语义 key;桥接不上的行丢弃
        (名称缺失/不唯一/拓扑缺位——宁可不继承也不能把 BPMN id 混进草稿域)。"""
        mapped = []
        for r in rows:
            sem = bridge_map.get(r.get("nodeKey") or r.get("edgeKey"))
            if sem is None:
                continue
            r = dict(r)
            if "nodeKey" in r:
                r["nodeKey"] = sem
            else:
                r["edgeKey"] = sem
            mapped.append(r)
        return mapped

    host_approve = _apply_bridge(host_approve, bridge["approve"])
    host_cc = _apply_bridge(host_cc, bridge["cc"])
    host_branch = _apply_bridge(host_branch, bridge["branch"])

    out: Dict[str, Any] = {}
    out[K.FORM_KEY] = (new_rules or {}).get(K.FORM_KEY) or (host_rules or {}).get(K.FORM_KEY)

    def _merge_list(new_list, host_list, ref_field):
        host_by = {r.get(ref_field): r for r in (host_list or []) if r.get(ref_field)}
        merged = []
        seen = set()
        for r in (new_list or []):
            k = r.get(ref_field)
            seen.add(k)
            base = host_by.get(k) or {}
            item = dict(base)
            item.update({kk: vv for kk, vv in r.items() if vv is not None})
            merged.append(item)
        # 未提及的 key 整条继承
        for k, r in host_by.items():
            if k not in seen:
                merged.append(dict(r))
        return merged

    out[K.APPROVE_RULES] = _merge_list(
        (new_rules or {}).get(K.APPROVE_RULES), host_approve, K.NODE_KEY_REF)
    out[K.CC_RULES] = _merge_list(
        (new_rules or {}).get(K.CC_RULES), host_cc, K.NODE_KEY_REF)
    out[K.BRANCH_RULES] = _merge_list(
        (new_rules or {}).get(K.BRANCH_RULES), host_branch, K.EDGE_KEY_REF)
    return out


def build_form_catalog(form_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """绑定表单字段目录蒸馏(每字段一行: key/名称/类型/选项)。

    结构对齐 njmind_form 的 build_field_catalog 语义;只取分支条件
    可引用的字段(过滤子表单内部结构,扁平化顶层)。
    """
    catalog: List[Dict[str, Any]] = []
    for f in (form_config or {}).get("formFieldConfigVos", []) or []:
        key = f.get("fieldTitleKey")
        if not key:
            continue
        entry = {
            "key": key,
            "title": f.get("fieldTitleText", ""),
            "type": f.get("formFieldType"),
            "typeName": f.get("fieldTypeName", ""),
        }
        opts = (f.get("optionSettings") or {}).get("optionFields") or []
        if opts:
            entry["options"] = [
                o.get("optionValue") or o.get("optionName") or o.get("label")
                for o in opts][:20]
        catalog.append(entry)
    return catalog


def collect_unknown_branch_fields(draft: Dict[str, Any],
                                  form_catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """收集分支条件里表单外的 formFieldKey(供确认门构建结构化问题)。

    返回 [{missing, candidates:[{label,value}]}];名称与字段标题精确相同时
    直接静默修正(不问)。空列表 = 全部命中/可静默修,无需确认。
    """
    valid_keys = {f.get("key") for f in (form_catalog or [])}
    by_title = {f.get("title"): f.get("key") for f in (form_catalog or [])}
    if not valid_keys:
        return []
    missing_map: Dict[str, Dict] = {}
    for br in (draft or {}).get(K.BRANCH_RULES, []) or []:
        for cond in (br.get("rules") or []):
            fk = cond.get("formFieldKey")
            if not fk or fk in valid_keys:
                continue
            hit = by_title.get(fk)
            if hit:   # 名称当 key 用了:静默修正
                cond["formFieldKey"] = hit
                continue
            if fk not in missing_map:
                missing_map[fk] = {"missing": fk}
    if not missing_map:
        return []
    titles = [f.get("title") or f.get("key") for f in (form_catalog or [])]
    keys_by_title = {f.get("title") or f.get("key"): f.get("key") for f in (form_catalog or [])}
    for fk, item in missing_map.items():
        # 语义相近优先;候选给全量(前端 >6 自动折叠为可搜索下拉,不会撑爆卡片)。
        # 之前 [:6] 截断曾把排在末尾的数值字段截掉,用户想选也选不到(实测)。
        near = [t for t in titles if fk in t or t in fk] or titles
        item["candidates"] = [{"label": t, "value": keys_by_title.get(t)} for t in near]
    return list(missing_map.values())


def apply_branch_field_answers(draft: Dict[str, Any],
                               answers: Dict[str, Any],
                               form_catalog: List[Dict[str, Any]]) -> int:
    """确定性回填用户点选的字段映射,返回替换条数(不过 LLM)。

    answers 形如 {missing_key: field_key 或 字段中文名};值为中文名时按
    目录翻译成 key。找不到映射的字段保持原样(render 校验兜底)。
    """
    by_title = {f.get("title"): f.get("key") for f in (form_catalog or [])}
    valid = {f.get("key") for f in (form_catalog or [])}
    n = 0
    for br in (draft or {}).get(K.BRANCH_RULES, []) or []:
        for cond in (br.get("rules") or []):
            fk = cond.get("formFieldKey")
            if fk not in answers:
                continue
            chosen = answers.get(fk)
            resolved = chosen if chosen in valid else by_title.get(chosen)
            if resolved:
                cond["formFieldKey"] = resolved
                n += 1
    return n


def host_context_from(state: Dict[str, Any]) -> Dict[str, Any]:
    """从工具 state 提取宿主上下文(嵌入模式 INIT/GET_CONTEXT 下发的 artifact)。

    引擎把画布 artifact 放在 state["source_artifact"](nodes.py tool_state 组装),
    形态 {formKey, bpmnXml, rules, modelKey, name}(Task 7 宿主契约)。
    返回 {formKey, bpmnXml, rules, processKey, processName};无宿主态时
    processKey/processName 为 None(F1 将拒绝渲染)。
    """
    art = state.get("source_artifact") or {}
    rules = art.get(K.RULES) or {}
    return {
        "formKey": art.get(K.FORM_KEY) or rules.get(K.FORM_KEY),
        "bpmnXml": art.get(K.BPMN_XML),
        "rules": rules,
        "processKey": art.get("modelKey"),
        "processName": art.get("name"),
    }
