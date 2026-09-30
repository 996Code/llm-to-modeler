"""njmind_list 管线公共件：宿主身份注入 / 字段确认门 / 按钮合并 / 机械修复。

被 generate_list 使用；全部纯操作 dict、不触网络（网络在工具步骤里经
service_locator.get_api()）。错误码契约对齐 A2 ListConfigValidator
（{code, field, message}，field 为点路径如 tableConfig.tableShowFields[0].defaultShow）。

关键桥接责任（A2 测试固化的契约事实）：Jackson 对 Integer 字段遇 JSON
true 整包拒绝——boolean 开关必须在发上游 validate 之前归一为 0|1。
generate_list 管线在首次 validate 前无条件调 normalize_switch_ints
（真实 A2 端点 FastJSON 对 true 宽松强转 1、首验即过，等错误驱动的
mechanical_repair 才归一会让 boolean 穿透进 artifact）。
"""
import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from domains.njmind_list import keys as K
from domains.njmind_list.keys import IDENTITY_FIELDS

MAX_MECHANICAL_ROUNDS = 4

# keys.py 之外的保存形态键（本模块私有，拼错在运行首日即暴露）
SERVER_KEY = "serverKey"
TABLE_CODE = "tableCode"
CONDITION_CONFIG = "conditionConfig"
QUERY_CONDITION = "queryCondition"
LIST_TABLE_QUERY_CONDITION = "listTableQueryCondition"
CONDITIONS = "conditions"          # boolCond 树子节点数组
CONDITION = "condition"            # combineConfig 的条件数组
TABLE_QUERY_SORT = "tableQuerySort"
FIELD_KEY = "fieldKey"
ADVANCE_CONFIG = "advanceConfig"
ADVANCE_CONFIG_FIELD = "advanceConfigField"
BIZ_TABLE_FIELD = "bizTableField"
CARD_TITLE_FIELDS = "cardTitleFields"
COMBINE_CONFIG = "combineConfig"
BUTTON_CONFIG_LIST = "buttonConfigList"
BUTTON_EVENT_CONFIG = "buttonEventConfig"
FIELD_TITLE_KEY = "fieldTitleKey"
FIELD_TITLE_TEXT = "fieldTitleText"
ACTION_TYPE = "actionType"
EVENT_SCRIPT = "eventScript"

# 身份域必填三元组：宿主缺任一即 fail-closed（F1）
_REQUIRED_IDENTITY = (K.LIST_CODE, SERVER_KEY, TABLE_CODE)

# VO 开关字段全集（BooleanEmun 0|1 整数）：列 7 + 按钮 2 + 组合筛选 2。
# 前置归一按字段名全表递归，覆盖 tableShowFields/rowButtons/
# buttonConfigList/dropdownItems/combineConfig.condition 等任意嵌套位置。
SWITCH_FIELDS = frozenset({
    "hideColumn", "fieldShow", "defaultShow", "defaultFilter",
    "defaultSort", "defaultFixed", "defaultCalculate",
    "buttonConfirm", "isTextButton", "resetFilter", "showNum",
})

# I2 按钮继承字段：脚本位零生成，宿主已配脚本/权限原样继承（不信任 LLM 产物）
_BUTTON_INHERIT_KEYS = ("beforeNotifyScript", "notifyScript",
                        "hiddenScript", "disableScript", "permissionCode")

_PATH_SEG_RE = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")
_IDX_RE = re.compile(r"\[(\d+)\]")
_ALLOWED_RE = re.compile(r"合法值[:：]\s*\[([^\]]*)\]")


# ── field 路径解析器（merge/repair 共用） ─────────────────────────

def _iter_path_segments(path: str) -> Optional[List[Tuple[Optional[str], Optional[int]]]]:
    """点路径拆段：'a.b[0].c' → [('a',None),('b',0),('c',None)]；非法段返回 None。"""
    segs: List[Tuple[Optional[str], Optional[int]]] = []
    for part in (path or "").split("."):
        m = _PATH_SEG_RE.match(part)
        if not m:
            return None
        segs.append((m.group(1), None))
        for idx in _IDX_RE.findall(m.group(2)):
            segs.append((None, int(idx)))
    return segs or None


def _resolve_node(root: Any, path: str) -> Tuple[Any, Any]:
    """定位 field 路径末端的 (容器, 键)。

    容器可能是 dict（键为 str）或 list（键为 int 下标）；中间段缺失/
    类型不符返回 (None, None)。dict 末端键允许不存在（供调用方赋值创建）。
    """
    segs = _iter_path_segments(path)
    if segs is None:
        return None, None
    cur = root
    for key, idx in segs[:-1]:
        if key is not None:
            if not isinstance(cur, dict):
                return None, None
            cur = cur.get(key)
        else:
            if not isinstance(cur, list) or not 0 <= idx < len(cur):
                return None, None
            cur = cur[idx]
        if not isinstance(cur, (dict, list)):
            return None, None
    last_key, last_idx = segs[-1]
    if last_key is not None:
        if not isinstance(cur, dict):
            return None, None
        return cur, last_key
    if not isinstance(cur, list) or not 0 <= last_idx < len(cur):
        return None, None
    return cur, last_idx


def _node_at(root: Any, path: str) -> Any:
    """取 field 路径末端节点本身（_resolve_node 的取值视图）；不可达返回 None。"""
    container, key = _resolve_node(root, path)
    if container is None:
        return None
    try:
        return container[key]
    except (KeyError, IndexError, TypeError):
        return None


# ── F1 宿主身份注入 ────────────────────────────────────────────────

def inject_host_identity(draft: Dict[str, Any],
                         host_config: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """剥 draft 全部 13 身份字段，再从 host_config 注入（LLM 自造值一律丢弃）。

    宿主缺 listCode/serverKey/tableCode 任一 → error 非空 fail-closed
    （BPM inject_host_identity 同款语义：宁可拒不出，不让半成品配置出去）。
    """
    d = dict(draft or {})
    for f in IDENTITY_FIELDS:
        d.pop(f, None)
    host = host_config or {}
    missing = [k for k in _REQUIRED_IDENTITY if not host.get(k)]
    if missing:
        return d, ("宿主上下文缺少列表标识(" + "/".join(missing) + ")，"
                   "已阻止生成——请在列表设计器内打开 AI 助手后重试")
    for f in IDENTITY_FIELDS:
        if host.get(f) is not None:
            d[f] = host[f]
    return d, None


# ── 字段确认门（collect / apply，均不过 LLM） ──────────────────────

def _iter_query_condition_slots(node: Any):
    """boolCond 树递归产出 (节点, fieldKey)：QueryCondition 根 + conditions[] 子树。

    与 A2 validateQueryConditionFields 同构：AND/OR 组合节点无 fieldKey
    （Java 侧 !isBlank 守卫），仅叶子/带键节点是消费点。
    """
    if not isinstance(node, dict):
        return
    if FIELD_KEY in node:
        yield node, FIELD_KEY
    for child in node.get(CONDITIONS) or []:
        yield from _iter_query_condition_slots(child)


def _field_slots(draft: Dict[str, Any]):
    """产出 draft 里字段 key 的可写位置 (容器, 键)：七个消费点。

    列 fieldTitleKey / 查询 conditionConfig.queryCondition /
    高级筛选 advanceConfig[].advanceConfigField[].bizTableField /
    卡片标题 tableConfig.cardTitleFields / 排序 tableQuerySort[].fieldKey /
    查询条件卡 tableConfig.listTableQueryCondition.queryCondition（boolCond
    树递归）/ 组合筛选 combineConfig[].condition[].listTableQueryCondition.
    queryCondition（同递归）——与 A2 validateFieldReferences 全口径一致。
    """
    table = (draft or {}).get(K.TABLE_CONFIG) or {}
    for col in table.get(K.TABLE_SHOW_FIELDS) or []:
        if isinstance(col, dict):
            yield col, FIELD_TITLE_KEY
    for i in range(len(table.get(CARD_TITLE_FIELDS) or [])):
        yield table[CARD_TITLE_FIELDS], i
    for sort in table.get(TABLE_QUERY_SORT) or []:
        if isinstance(sort, dict) and FIELD_KEY in sort:
            yield sort, FIELD_KEY
    lqc = table.get(LIST_TABLE_QUERY_CONDITION) or {}
    if isinstance(lqc, dict):
        yield from _iter_query_condition_slots(lqc.get(QUERY_CONDITION))
    cond = (draft or {}).get(CONDITION_CONFIG) or {}
    for i in range(len(cond.get(QUERY_CONDITION) or [])):
        yield cond[QUERY_CONDITION], i
    for adv in (draft or {}).get(ADVANCE_CONFIG) or []:
        for f in (adv or {}).get(ADVANCE_CONFIG_FIELD) or []:
            if isinstance(f, dict):
                yield f, BIZ_TABLE_FIELD
    for combine in (draft or {}).get(COMBINE_CONFIG) or []:
        for c in (combine or {}).get(CONDITION) or []:
            yield from _iter_query_condition_slots(
                (c or {}).get(LIST_TABLE_QUERY_CONDITION, {}).get(QUERY_CONDITION)
                if isinstance(c, dict) else None)


def _candidate_description(entry: Dict[str, Any]) -> str:
    """候选描述：字段标识 + 类型（BPM 确认门 '字段标识: x' 同款风格）。"""
    parts = [f"字段标识: {entry.get('value')}"]
    ft = entry.get("fieldType") or entry.get("type")
    if ft is not None and str(ft) != "":
        parts.append(f"类型: {ft}")
    return " / ".join(parts)


def collect_unknown_fields(draft: Dict[str, Any],
                           catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """收集 draft 各消费点里 catalog 之外的字段 key（供确认门结构化追问）。

    消费点见 _field_slots（七处，与 A2 validateFieldReferences 同口径，
    含排序 fieldKey 与 boolCond 树递归查询条件）。

    catalog 条目 {value: fieldKey, label: fieldTitle}；返回
    [{missing, candidates: [{label, description}]}]。候选给全量目录
    （BPM 同款：>6 前端折叠为可搜索下拉，截断会让用户想选也选不到）。
    空目录 = 无从判定（fetch_guide 已 fail-closed），不出确认门。
    """
    valid = {c.get("value") for c in catalog or [] if c.get("value")}
    if not valid:
        return []
    missing: Dict[Any, Dict[str, Any]] = {}
    for container, key in _field_slots(draft):
        v = container[key]
        if not v or v in valid or v in missing:
            continue
        missing[v] = {"missing": v}
    if not missing:
        return []
    candidates = [{"label": c.get("label") or c.get("value"),
                   "description": _candidate_description(c)}
                  for c in catalog or []]
    for item in missing.values():
        item["candidates"] = list(candidates)
    return list(missing.values())


def apply_field_answers(draft: Dict[str, Any],
                        answers: Dict[str, Any],
                        catalog: List[Dict[str, Any]]) -> None:
    """resume 确定性回填 {missing名: 选中label} → label 经 catalog 反查 value。

    就地改写各消费点（见 _field_slots，含排序/嵌套查询条件），不过 LLM；
    值本身已是合法 value 时直接采用。
    找不到映射的位置保持原样（上游 validate 兜底）。
    """
    answers = answers or {}
    by_label = {c.get("label"): c.get("value") for c in catalog or [] if c.get("value")}
    valid = {c.get("value") for c in catalog or [] if c.get("value")}
    for container, key in _field_slots(draft or {}):
        cur = container[key]
        if cur not in answers:
            continue
        chosen = answers.get(cur)
        resolved = chosen if chosen in valid else by_label.get(chosen)
        if resolved:
            container[key] = resolved


# ── I2 按钮与组合筛选合并 ──────────────────────────────────────────

def _button_action_type(btn: Dict[str, Any]) -> Any:
    """actionType 在按钮顶层（LLM 习惯）或 buttonEventConfig 内（VO 真实位）都认。"""
    ev = btn.get(BUTTON_EVENT_CONFIG) or {}
    return btn.get(ACTION_TYPE) or ev.get(ACTION_TYPE)


def _group_by_semantic(buttons: List[Any]) -> Dict[Tuple[Any, Any], List[Dict[str, Any]]]:
    groups: Dict[Tuple[Any, Any], List[Dict[str, Any]]] = {}
    for b in buttons or []:
        if not isinstance(b, dict):
            continue
        gk = (b.get("buttonName"), _button_action_type(b))
        groups.setdefault(gk, []).append(b)
    return groups


def merge_buttons(new_draft: Dict[str, Any], host_config: Dict[str, Any]) -> None:
    """I2：draft 按钮按 (buttonName, actionType) 语义键对位继承宿主已配内容。

    宿主源池 = rowButtons + buttonGroupConfig.buttonConfigList（按钮可能
    在两栏间移动，语义身份与所在栏无关）；组内同名按钮按出现序对位，
    不交叉继承。继承项：buttonId、4 类脚本 + 事件 eventScript、permissionCode
    （脚本位零生成，宿主已配脚本原样保留）。无匹配的新按钮保留 LLM 产物
    （buttonId 缺失时补 button_list_N 占位）。

    combineConfig 同理按 name 对位：继承 id 与 condition[].conditionId，
    defaultConditionId 经旧→新 id 映射改写不断链。
    """
    draft = new_draft or {}
    host = host_config or {}

    host_pool: List[Any] = list((host.get(K.TABLE_CONFIG) or {}).get(K.ROW_BUTTONS) or [])
    host_pool += (host.get(K.BUTTON_GROUP_CONFIG) or {}).get(BUTTON_CONFIG_LIST) or []
    groups = _group_by_semantic(host_pool)

    targets: List[List[Any]] = []
    d_table = draft.get(K.TABLE_CONFIG) or {}
    if isinstance(d_table.get(K.ROW_BUTTONS), list):
        targets.append(d_table[K.ROW_BUTTONS])
    d_group = draft.get(K.BUTTON_GROUP_CONFIG) or {}
    if isinstance(d_group.get(BUTTON_CONFIG_LIST), list):
        targets.append(d_group[BUTTON_CONFIG_LIST])

    counters: Dict[Tuple[Any, Any], int] = {}
    seq = 0
    for buttons in targets:
        for b in buttons:
            if not isinstance(b, dict):
                continue
            gk = (b.get("buttonName"), _button_action_type(b))
            queue = groups.get(gk) or []
            idx = counters.get(gk, 0)
            counters[gk] = idx + 1
            src = queue[idx] if idx < len(queue) else None
            if src is None:
                if not b.get("buttonId"):
                    seq += 1
                    b["buttonId"] = f"button_list_{seq}"
                continue
            if src.get("buttonId"):
                b["buttonId"] = src["buttonId"]
            for k in _BUTTON_INHERIT_KEYS:
                if k in src:
                    b[k] = src[k]
            src_ev = src.get(BUTTON_EVENT_CONFIG) or {}
            d_ev = b.get(BUTTON_EVENT_CONFIG)
            if EVENT_SCRIPT in src_ev and isinstance(d_ev, dict):
                d_ev[EVENT_SCRIPT] = src_ev[EVENT_SCRIPT]

    _merge_combine_config(draft, host)


def _merge_combine_config(draft: Dict[str, Any], host: Dict[str, Any]) -> None:
    """组合筛选按 name 对位继承 id/conditionId（组内按出现序，同按钮语义）。"""
    host_groups: Dict[Any, List[Dict[str, Any]]] = {}
    for c in host.get(COMBINE_CONFIG) or []:
        if isinstance(c, dict) and c.get("name"):
            host_groups.setdefault(c["name"], []).append(c)

    counters: Dict[Any, int] = {}
    for c in draft.get(COMBINE_CONFIG) or []:
        if not (isinstance(c, dict) and c.get("name")):
            continue
        queue = host_groups.get(c["name"]) or []
        idx = counters.get(c["name"], 0)
        counters[c["name"]] = idx + 1
        src = queue[idx] if idx < len(queue) else None
        if src is None:
            continue
        if src.get("id"):
            c["id"] = src["id"]

        cond_groups: Dict[Any, List[Dict[str, Any]]] = {}
        for cond in src.get("condition") or []:
            if isinstance(cond, dict) and cond.get("name"):
                cond_groups.setdefault(cond["name"], []).append(cond)
        old_to_new: Dict[Any, Any] = {}
        cond_counters: Dict[Any, int] = {}
        for cond in c.get("condition") or []:
            if not (isinstance(cond, dict) and cond.get("name")):
                continue
            cq = cond_groups.get(cond["name"]) or []
            j = cond_counters.get(cond["name"], 0)
            cond_counters[cond["name"]] = j + 1
            hsrc = cq[j] if j < len(cq) else None
            if hsrc and hsrc.get("conditionId"):
                new_id = hsrc["conditionId"]
                old = cond.get("conditionId")
                if old != new_id:
                    old_to_new[old] = new_id
                cond["conditionId"] = new_id
        if c.get("defaultConditionId") in old_to_new:
            c["defaultConditionId"] = old_to_new[c["defaultConditionId"]]


# ── 机械修复 ──────────────────────────────────────────────────────

def normalize_switch_ints(node: Any) -> None:
    """11 个开关字段 True/False → 1/0，全表递归（就地）。

    Jackson 对 Integer 字段遇 JSON true 整包拒绝、连 validation 都不返回，
    必须在发上游 validate 之前完成——管线在首次 validate 前无条件调用；
    mechanical_repair 保留同款前置归一（幂等，兜 retry 路径的新草稿）。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k in SWITCH_FIELDS and isinstance(v, bool):
                node[k] = int(v)
            else:
                normalize_switch_ints(v)
    elif isinstance(node, list):
        for item in node:
            normalize_switch_ints(item)


def _first_allowed_value(message: str) -> Any:
    """从 ENUM message '合法值: [a, b]' 解析清单取首项（数值域转 int）。"""
    m = _ALLOWED_RE.search(message or "")
    if not m:
        return None
    raw = (m.group(1).split(",") or [""])[0].strip().strip("'\"")
    if raw == "":
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def _repair_bool_field(d: Dict[str, Any], field: str) -> bool:
    container, key = _resolve_node(d, field)
    if isinstance(container, dict) and key in container:
        v = container[key]
        if v in (0, 1):
            return True   # 前置归一已修好（boolean→0|1），错误视为已消化
        container[key] = 1 if v else 0
        return True
    return False


def _repair_enum_field(d: Dict[str, Any], field: str, message: str) -> bool:
    value = _first_allowed_value(message)
    if value is None:
        return False
    container, key = _resolve_node(d, field)
    if isinstance(container, dict):
        container[key] = value
        return True
    return False


def _repair_catalog_field(d: Dict[str, Any], field: str,
                          label_to_value: Dict[Any, Any]) -> bool:
    """FIELD_NOT_IN_CATALOG 仅修列路径：fieldTitleText 精确匹配目录名回填 key。

    查询/筛选/卡片位是裸 key 无文本可对，留给确认门（结构化候选追问）。
    """
    if not field.endswith("." + FIELD_TITLE_KEY):
        return False
    col_path = field[: field.rfind(".")]
    col = _node_at(d, col_path)
    if not isinstance(col, dict):
        return False
    hit = label_to_value.get(col.get(FIELD_TITLE_TEXT))
    if hit:
        col[FIELD_TITLE_KEY] = hit
        return True
    return False


def _repair_structure(d: Dict[str, Any], field: str, message: str) -> bool:
    """STRUCTURE_INVALID 只做无类型风险的纯改写（pageSizes 去重、defPageSize 对齐）。

    补缺/内容级错误机械不可修，一律不做（B3 审查 C1+I1 + 复审 N1 实证）：
    删除式修复会误伤好条目/整卡；补缺式修复会把对象型 QueryCondition
    写成 [] （ListTableQueryCondition.queryCondition 是单对象 POJO，非数组），
    毒化成 Jackson 整包拒绝且误标已修——mode 缺省补 10 的侧补同理。
    错误原样保留 remaining，由 ≤3 次重生成兜底。
    """
    if field == "queryPage.pageSizes":
        container, key = _resolve_node(d, field)
        if isinstance(container, dict):
            sizes, seen = [], set()
            for s in container.get(key) or []:
                if isinstance(s, int) and not isinstance(s, bool) \
                        and s > 0 and s not in seen:
                    seen.add(s)
                    sizes.append(s)
            if sizes and sizes != container.get(key):
                container[key] = sizes
                return True
        return False
    if field == "queryPage.defPageSize":
        container, key = _resolve_node(d, field)
        sizes = (d.get(K.QUERY_PAGE) or {}).get("pageSizes") or []
        if isinstance(container, dict) and sizes and container.get(key) not in sizes:
            container[key] = sizes[0]
            return True
        return False
    # 其余（SQL 空文本、坏数组条目、conditionMode/queryCondition 缺失等）
    # 补缺/内容级错误：机械不可修，保留 remaining
    return False


def mechanical_repair(draft: Dict[str, Any],
                      errors: List[Dict[str, Any]],
                      catalog: Optional[List[Dict[str, Any]]] = None
                      ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """对上游 validate errors 做确定性修复（≤4 轮由调用方控制）。

    前置归一（无条件，先于错误驱动）：11 开关字段 boolean → 0|1 全表递归。
    按码修复：
    - BOOL_FIELD_NOT_INT：越界值钳回 0|1（boolean 已被前置归一消灭）
    - ENUM_OUT_OF_RANGE：message '合法值: [..]' 取首项替换（field 路径定位）
    - FIELD_NOT_IN_CATALOG：列 fieldTitleText 精确匹配 catalog 回填 key
      （需 catalog；查询/筛选位留给确认门）
    - STRUCTURE_INVALID：仅无类型风险的纯改写（pageSizes 去重正整数化、
      defPageSize 对齐）。补缺/内容级错误（SQL 空文本、坏数组条目、
      conditionMode/queryCondition 缺失）不做修复——剥除会误删好条目/
      整卡（C1+I1），补 [] 会把对象型 QueryCondition 毒化成 Jackson
      整包拒绝且误标已修（复审 N1），一律保留 remaining 走重生成。
    - 其余（REQUIRED_MISSING/SCRIPT_NOT_ALLOWED/TEMPLATE_MISMATCH）非机械
      可修，保留给重生成循环。

    返回 (repaired_draft, remaining)；remaining 逐条对应（按 code+field
    精确剔除），过宽清除会让未修复错误一并消失、管线误判成功（BPM 同款教训）。
    """
    d = copy.deepcopy(draft or {})
    normalize_switch_ints(d)

    label_to_value = {c.get("label"): c.get("value")
                      for c in catalog or [] if c.get("value")}
    fixed = set()
    for e in errors or []:
        code = e.get("code")
        field = e.get("field") or ""
        message = e.get("message") or ""
        ok = False
        if code == "BOOL_FIELD_NOT_INT":
            ok = _repair_bool_field(d, field)
        elif code == "ENUM_OUT_OF_RANGE":
            ok = _repair_enum_field(d, field, message)
        elif code == "FIELD_NOT_IN_CATALOG" and label_to_value:
            ok = _repair_catalog_field(d, field, label_to_value)
        elif code == "STRUCTURE_INVALID":
            ok = _repair_structure(d, field, message)
        if ok:
            fixed.add((code, field))

    remaining = [e for e in errors or []
                 if (e.get("code"), e.get("field") or "") not in fixed]
    return d, remaining
