"""表单字段角色权限 —— 目录获取、名词解析、追问与回填（pack 私有）。

【模块定位】
njmind_form pack 的权限领域层：把 LLM 产物中的角色「中文名词」（中间态）
经 sdk.catalog 的通用匹配原语确定性解析成真实 role code。中文名词只存在
于 LLM 中间产物，最终 artifact 只允许真实 code（引擎通用层零领域知识，
"canEditFieldRoles 对应角色目录"这层映射只住在这里）。

【职责】
  - fetch_role_catalog: 取角色目录（模块级 TTL 缓存 300s，失败/空 → [] 绝不抛）
  - build_role_reference: 目录 → prompt 参考区（卫生清洗 + 上限 + 数据参考标注）
  - resolve_form_role_permissions: 制品就地解析；无法直选的语义串产出追问卡
    （header `__role:<fieldKey>:<prop>:<semantic-token>`，token=名词身份短哈希、
    跨轮稳定；候选 label「名称（code）」，附固定「忽略本条权限」选项）；
    answers 里按 header 反查回填，无第二次 LLM。header 用身份而非序号：
    pending 列表位移（部分作答/目录漂移/制品重生成）时错位答案的 key 必不
    撞键 → 重新追问，绝不静默写到别的名词（终审 Important-1）
  - resolve_form_task_permissions: 审批节点版（目录来自 pack_params 的
    task_keys，code=id；Add/Edit 组剔出抄送节点；未绑流程回阻断消息）
  - run_permission_resolution: 工具步骤统一入口（角色降级 + 节点阻断 +
    pending 标记，两类追问合并抛出）

【硬规则（Plan1 v2 审查结论）】
  - 开关置位：仅当某字段本轮有 ≥1 值经「名称匹配」解析成 code 时才把对应
    总开关置 1；存量 code 透传、未被指令触碰的字段开关一律不动
    （restore_untouched 之后执行的 resolve 不得翻动还原值）。
  - 空数组禁令（fail-open 防护）：modify 语境剥键保旧值 + warning；
    create 语境剥键 + notes 提示。取消限制只能表达为开关置 0。
  - 忽略语义：「忽略本条权限」的"条"=单个 pending 名词，同组已解析值绝不
    连坐丢弃；剥除后组内空 → 回"本次指令未涉及该权限"状态（modify 保旧
    值+旧开关，与空数组分支同构；create 剥键即可，开关不动）。
  - resume 语义：_permission_pending_artifact 存在 → 工具跳过所有 LLM 步骤，
    本模块用 answers 直接回填 pending 制品。
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sdk.catalog import (resolve_names, lookup_selected, entries_from_rows,
                         clean_text, semantic_token, IGNORE_LABEL,
                         PendingPick)
from sdk.tool import ClarificationRaised
from domains.njmind_form.keys import FIELDS, FIELD_KEY, FIELD_TITLE

logger = logging.getLogger(__name__)

# ── 字段权限键映射（roles 数组 → 总开关）──────────────────────────
ROLE_FIELDS = {
    "canShowFieldAddRoles": "isShowFieldAdd",
    "canShowFieldDetailRoles": "isShowFieldDetail",
    "canEditFieldRoles": "isEditField",
}
# 追问卡展示用的中文属性名
_PROP_LABELS = {
    "canShowFieldAddRoles": "新增可见角色",
    "canShowFieldDetailRoles": "查看可见角色",
    "canEditFieldRoles": "可编辑角色",
}

# 追问固定选项：「条」=单个 pending 名词——仅丢弃该名词；剥除后组内空才回
# 「本次指令未涉及该权限」状态（modify 保旧值+旧开关，create 剥键即可）
# ── 审批节点权限键映射（TaskKeys 数组 → (总开关, 是否允许抄送节点)）──
# 布尔 = 是否允许 taskType==4（抄送节点）：仅 Detail 组允许，Add/Edit 剔除。
TASK_FIELDS = {
    "canShowFieldAddTaskKeys": ("isShowFieldAdd", False),
    "canShowFieldDetailTaskKeys": ("isShowFieldDetail", True),
    "canEditFieldTaskKeys": ("isEditField", False),
}
# 追问卡展示用的中文属性名
_TASK_PROP_LABELS = {
    "canShowFieldAddTaskKeys": "新增可见审批节点",
    "canShowFieldDetailTaskKeys": "查看可见审批节点",
    "canEditFieldTaskKeys": "可编辑审批节点",
}
# 抄送节点的 taskType 值（designer getTaskKeyList 契约）
CC_TASK_TYPE = 4
# 未绑流程（process_definition_key 为 null 且产物带节点语义）的终态提示
TASK_UNBOUND_MSG = "请先绑定流程后再配置审批节点权限"

# ── 目录获取：模块级 TTL 缓存（传输层 params 请求 cache 是 no-op）──
ROLE_CATALOG_TTL = 300
# prompt 参考区注入上限（注入卫生约束）
MAX_PROMPT_ROWS = 200
# 追问卡候选上限（前端自动附"其他"自由输入，超限仍可手输名称/code）
_MAX_OPTIONS = 50

# {服务地址: (取数时间戳, rows)}；只缓存成功非空结果（瞬时失败不粘 300s）
_role_catalog_cache: Dict[str, tuple] = {}


def reset_role_catalog_cache() -> None:
    """测试隔离用：清空模块级目录缓存。"""
    _role_catalog_cache.clear()


def _service_base_key(ctx) -> str:
    """缓存键 = 本请求的上游服务地址（多宿主部署防串目录）。

    经 asset_client 鸭子类型取通用传输的 resolve_base；取不到（测试桩/
    非 HTTP 实现）退化为空串——键退化只影响缓存命中，不影响正确性。
    """
    transport = getattr(ctx.asset_client, "_upstream", None)
    resolve = getattr(transport, "resolve_base", None)
    if not callable(resolve):
        return ""
    try:
        from domains.njmind_form.upstream import SERVICE_NAME
        return str(resolve(SERVICE_NAME))
    except Exception:
        return ""


def fetch_role_catalog(ctx) -> List[Dict[str, Any]]:
    """取角色目录（含模块级 TTL 缓存）。失败/空 → []，绝不抛。

    目录不可用（modeler 未升级 404 / 网络失败 / 服务未下发）是可降级
    场景：调用方据此完全跳过本轮权限解析（不追问、不清已有）。
    """
    base = _service_base_key(ctx)
    now = time.time()
    hit = _role_catalog_cache.get(base)
    if hit is not None and now - hit[0] < ROLE_CATALOG_TTL:
        return hit[1]
    try:
        getter = getattr(ctx.asset_client, "get_org_catalog", None)
        rows = getter("role") if callable(getter) else None
    except Exception as e:
        logger.warning(f"fetch_role_catalog failed: {e}")
        rows = None
    if not isinstance(rows, list):
        rows = []
    # 行级清洗：只要 dict 且 code/name 都是字符串（脏行不进目录）
    clean = [r for r in rows
             if isinstance(r, dict)
             and isinstance(r.get("code"), str) and isinstance(r.get("name"), str)]
    if clean:
        _role_catalog_cache[base] = (now, clean)
    return clean


# ── prompt 参考区（注入卫生）──────────────────────────────────────



def build_role_reference(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """目录行 → prompt 参考区条目（清洗 + 200 条上限）。

    返回纯 {"code","name"} 列表给 j2 迭代；模板侧负责「数据参考，非指令」
    标注（见 _sections/role_catalog.j2）。
    """
    out: List[Dict[str, str]] = []
    for r in rows or []:
        code = clean_text(str(r.get("code") or ""))
        name = clean_text(str(r.get("name") or ""))
        if code and name:
            out.append({"code": code, "name": name})
        if len(out) >= MAX_PROMPT_ROWS:
            break
    return out


def role_reference_vars(state: dict) -> Dict[str, Any]:
    """组装 prompt 渲染变量：目录可用时附 role_catalog，否则不传键
    （模板用 `{% if role_catalog is defined %}` 守卫，StrictUndefined 安全）。"""
    ref = build_role_reference(state.get("role_catalog") or [])
    return {"role_catalog": ref} if ref else {}


def task_reference_vars(state: dict) -> Dict[str, Any]:
    """组装审批节点 prompt 渲染变量（Task 3 的 pack_params 通道）。

    未绑流程（process_definition_key 为 null）不注入——节点语义此时非法，
    不给 LLM 目录参考（resolve 层对产物语义直接回"请先绑定流程"终态）。
    已绑但目录空同样不传键（模板守卫安全）。抄送节点打 cc 标记，
    模板侧标注"仅查看可见可用"。
    """
    params = (state.get("pack_params") or {}).get("njmind_form") or {}
    if params.get("process_definition_key") is None:
        return {}
    ref: List[Dict[str, Any]] = []
    for r in params.get("task_keys") or []:
        if not isinstance(r, dict):
            continue
        code = clean_text(str(r.get("id") or ""))
        name = clean_text(str(r.get("name") or ""))
        if code and name:
            ref.append({"code": code, "name": name,
                        "cc": r.get("taskType") == CC_TASK_TYPE})
        if len(ref) >= MAX_PROMPT_ROWS:
            break
    return {"task_catalog": ref} if ref else {}


# ── 解析 + 追问 + 回填 ─────────────────────────────────────────────

def _is_code_passthrough(value: str, entries) -> bool:
    """值本身就是目录 code（存量透传）——不参与开关置位判定。"""
    return any(e.code == value for e in entries)


def _lookup_by_label(answer: str, candidates):
    """按追问卡 label 反查候选条目。

    优先 pack 自建 label 映射（"名称（code）"），兜底 sdk.lookup_selected
    （name/code 精确相等，覆盖前端"其他"自由输入场景）。"""
    for e in candidates:
        if answer == f"{e.name}（{e.code}）":
            return e
    return lookup_selected(answer, candidates)


def _repick_with_user_input(pick, user_input: str):
    """自由输入未命中候选 → 用输入换入 pending 语义（防追问死循环）。

    原样回锅原语义会让每轮重发同 header 追问卡，用户除"忽略"外无法
    脱身（候选为空时反查必 None）。换入用户输入后下一轮 resolve_names
    按全规则匹配（①-④）——手输的真实名词可经包含匹配直选；编造词
    仍 pending，但 header 已变、卡面带最新语义，用户看得见要忽略什么。
    绝不把输入直接当 code 落库（无中文落库不变量不破）。
    """
    return PendingPick(semantic=user_input, candidates=pick.candidates)


def _question(field_title: str, anchor: str, prop: str,
              semantic: str, candidates) -> str:
    """单条追问卡（JSON 字符串，引擎按结构化透传渲染点选卡）。

    header 末段是名词身份 token（见 _semantic_token）而非列表序号：
    跨轮稳定、与具体名词一一对应，防答案错位回填。
    """
    options = [
        {"label": f"{e.name}（{e.code}）", "description": f"编码 {e.code}"}
        for e in candidates[:_MAX_OPTIONS]
    ]
    options.append({"label": IGNORE_LABEL, "description": "不配置这条权限"})
    return json.dumps({
        "question": (f"字段「{field_title}」的{_PROP_LABELS.get(prop, prop)}"
                     f"「{semantic}」无法唯一匹配，请选择："),
        "header": f"__role:{anchor}:{prop}:{semantic_token(semantic)}",
        "options": options,
    }, ensure_ascii=False)


def resolve_form_role_permissions(
    artifact: Dict[str, Any],
    catalog_rows: List[Dict[str, Any]],
    answers: Dict[str, Any],
    *,
    source_artifact: Optional[Dict[str, Any]] = None,
    mode: str = "create",
    notes: Optional[List[str]] = None,
) -> List[str]:
    """就地解析制品中的角色名词；返回追问卡列表（空 = 全部落定）。

    Args:
        artifact: 制品（formFieldConfigVos 就地改写：名词→code）。
        catalog_rows: 角色目录行（state["role_catalog"] 原始 list[dict]）。
        answers: 追问回答 {header: label}（engine 注入的 clarify_answers）。
        source_artifact: modify 语境的原始配置（空数组保旧值用）。
        mode: "create" | "modify"（空数组处置分叉）。
        notes: 附注通道（create 语境空数组提示等，工具拼进 summary）。

    Returns:
        JSON 追问卡字符串列表；每项一问、单选、附"忽略本条权限"。
    """
    entries = entries_from_rows(catalog_rows or [])
    questions: List[str] = []
    if not entries:
        return questions

    source_fields = {}
    if isinstance(source_artifact, dict):
        for f in source_artifact.get(FIELDS) or []:
            if isinstance(f, dict) and f.get(FIELD_KEY):
                source_fields[str(f.get(FIELD_KEY))] = f

    fields = artifact.get(FIELDS)
    if not isinstance(fields, list):
        return questions

    for f in fields:
        if not isinstance(f, dict):
            continue
        anchor = str(f.get(FIELD_KEY) or f.get(FIELD_TITLE) or "")
        title = str(f.get(FIELD_TITLE) or anchor)
        if not anchor:
            continue
        src = source_fields.get(anchor) or {}

        for roles_key, switch_key in ROLE_FIELDS.items():
            if roles_key not in f:
                continue
            raw = f.get(roles_key)

            # 空数组禁令：非法表达，绝不落库（fail-open 防护）
            if not isinstance(raw, list) or not raw:
                if mode == "modify" and src.get(roles_key) is not None:
                    f[roles_key] = src.get(roles_key)     # 剥产物键、保旧值
                    if switch_key in src:
                        f[switch_key] = src.get(switch_key)
                    logger.warning(
                        f"resolve_permissions: {anchor}.{roles_key} 为空数组，"
                        f"已保留源配置旧值 {src.get(roles_key)!r}")
                else:
                    f.pop(roles_key, None)
                    if notes is not None:
                        notes.append("检测到空的角色权限数组，已按未提及处理")
                continue

            # 逐值解析：code 透传 / 名称直选 / pending（追问）
            resolved: List[str] = []
            pending: List = []
            name_matched = False
            for v in raw:
                s = v.strip() if isinstance(v, str) else ""
                if not s:
                    continue
                r = resolve_names([s], entries)
                if r.resolved:
                    code = r.resolved[0]
                    if code not in resolved:
                        resolved.append(code)
                    if not _is_code_passthrough(s, entries):
                        name_matched = True   # 名称匹配 → 本轮语义，置开关
                else:
                    pending.append(r.pending[0])

            # 追问回填：按 header 反查（无第二次 LLM 调用）。header 末段是
            # 名词身份 token——错位/残留/旧格式（序号版）答案的 key 与当前
            # 名词必不匹配 → 视为未作答，重新追问（绝不静默写错 code）
            still_pending = []
            ignored = False
            for pick in pending:
                header = (f"__role:{anchor}:{roles_key}:"
                          f"{semantic_token(pick.semantic)}")
                ans = answers.get(header)
                if ans is None or not str(ans).strip():
                    still_pending.append(pick)
                    continue
                ans = str(ans).strip()
                if ans == IGNORE_LABEL:
                    # 「条」= 单个 pending 名词：仅丢弃该名词，
                    # 同组已解析值与其余待定名词不受影响
                    ignored = True
                    continue
                entry = _lookup_by_label(ans, pick.candidates)
                if entry is not None:
                    if entry.code not in resolved:
                        resolved.append(entry.code)
                    name_matched = True
                else:
                    # 自由输入未命中 → 换入用户输入下轮全规则重解析
                    # （原样回锅原语义 = 同 header 卡死循环）
                    still_pending.append(_repick_with_user_input(pick, ans))

            if resolved or still_pending:
                # 已定 code 在前、待定名词殿后（resume 再解析保持稳定）
                f[roles_key] = resolved + [p.semantic for p in still_pending]
                if name_matched and resolved:
                    f[switch_key] = 1
            elif ignored:
                # 忽略后组内空 → 回「本次指令未涉及该权限」状态（与空数组
                # 禁令分支同构）：modify 保旧值+旧开关（忽略=本次不写，
                # 不是清空旧权限）；create（新字段）剥键即可
                if mode == "modify" and src.get(roles_key) is not None:
                    f[roles_key] = src.get(roles_key)
                    if switch_key in src:
                        f[switch_key] = src.get(switch_key)
                    logger.warning(
                        f"resolve_permissions: {anchor}.{roles_key} 本轮权限名词"
                        f"被用户忽略，已保留源配置旧值 {src.get(roles_key)!r}")
                else:
                    f.pop(roles_key, None)
            else:
                # 全部值非法（非字符串/空白）→ 等同空数组禁令处置
                f.pop(roles_key, None)
                if notes is not None:
                    notes.append("检测到空的角色权限数组，已按未提及处理")
            # 同语义重复名词只发一张卡（header 即身份，答案对同语义全部生效）
            asked: set = set()
            for pick in still_pending:
                header = (f"__role:{anchor}:{roles_key}:"
                          f"{semantic_token(pick.semantic)}")
                if header in asked:
                    continue
                asked.add(header)
                questions.append(_question(
                    title, anchor, roles_key,
                    pick.semantic, pick.candidates))
    return questions


# ── 审批节点解析 + 追问 + 回填 ─────────────────────────────────────

@dataclass
class TaskPermissionOutcome:
    """节点权限解析结果：追问卡列表 + 未绑流程阻断消息（二选一）。"""
    questions: List[str] = field(default_factory=list)
    block_message: Optional[str] = None


def _task_question(field_title: str, anchor: str, prop: str,
                   semantic: str, candidates) -> str:
    """单条节点追问卡（结构同角色卡，header 前缀 `__task:`）。

    header 末段同角色卡用名词身份 token（_semantic_token），跨轮稳定。
    """
    options = [
        {"label": f"{e.name}（{e.code}）", "description": f"节点 id {e.code}"}
        for e in candidates[:_MAX_OPTIONS]
    ]
    options.append({"label": IGNORE_LABEL, "description": "不配置这条权限"})
    return json.dumps({
        "question": (f"字段「{field_title}」的{_TASK_PROP_LABELS.get(prop, prop)}"
                     f"「{semantic}」无法唯一匹配，请选择："),
        "header": f"__task:{anchor}:{prop}:{semantic_token(semantic)}",
        "options": options,
    }, ensure_ascii=False)


def resolve_form_task_permissions(
    artifact: Dict[str, Any],
    task_rows: List[Dict[str, Any]],
    process_key: Optional[str],
    answers: Dict[str, Any],
    *,
    source_artifact: Optional[Dict[str, Any]] = None,
    mode: str = "create",
    notes: Optional[List[str]] = None,
) -> TaskPermissionOutcome:
    """就地解析制品中的审批节点名词；返回追问卡或未绑流程阻断消息。

    与角色解析同构（守卫全套复用：开关置位 / 空数组禁令 / 忽略=仅丢弃
    单条名词 / `__task:` header 回填无第二次 LLM），差异只有三点：
      - 目录来自 pack_params 的 task_keys（code=id），非组织目录；
      - Add/Edit 组候选剔出抄送节点（taskType==4），Detail 组保留；
        存量 id（含抄送 id）透传保留不翻开关——不清已有；
      - process_key 为 null 且产物带节点语义（非 ASCII 值）→ 阻断消息，
        调用方回 reply-only 终态（不追问、不校验、不重试）。

    目录空但 process_key 非空：语义名词照常进 pending（零候选追问卡，
    仅"忽略本条权限"可点）——目录缺失≠未绑流程。
    """
    fields = artifact.get(FIELDS)
    if not isinstance(fields, list):
        return TaskPermissionOutcome()

    # 行级清洗：code=id（数字 id 归一为字符串）、name 必须字符串
    clean_rows = []
    for r in task_rows or []:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if isinstance(rid, int):
            rid = str(rid)
        if not isinstance(rid, str) or not isinstance(r.get("name"), str):
            continue
        clean_rows.append({"code": rid, "name": r["name"],
                           "taskType": r.get("taskType")})
    full_entries = entries_from_rows(clean_rows)
    full_ids = {e.code for e in full_entries}
    cc_ids = {r["code"] for r in clean_rows if r["taskType"] == CC_TASK_TYPE}
    # 组级目录：Add/Edit 剔出抄送节点，Detail 保留全量
    group_entries = {
        key: tuple(e for e in full_entries
                   if allow_cc or e.code not in cc_ids)
        for key, (_switch, allow_cc) in TASK_FIELDS.items()
    }

    # 未绑流程短路：产物带节点语义（非 ASCII 值；纯 ASCII 视为存量 id
    # 透传，不阻断）→ 阻断消息，调用方回 reply-only 终态
    if process_key is None:
        for f in fields:
            if not isinstance(f, dict):
                continue
            for key in TASK_FIELDS:
                raw = f.get(key)
                if isinstance(raw, list) and any(
                        isinstance(v, str) and not v.isascii() and v.strip()
                        for v in raw):
                    return TaskPermissionOutcome(block_message=TASK_UNBOUND_MSG)

    source_fields = {}
    if isinstance(source_artifact, dict):
        for f in source_artifact.get(FIELDS) or []:
            if isinstance(f, dict) and f.get(FIELD_KEY):
                source_fields[str(f.get(FIELD_KEY))] = f

    questions: List[str] = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        anchor = str(f.get(FIELD_KEY) or f.get(FIELD_TITLE) or "")
        title = str(f.get(FIELD_TITLE) or anchor)
        if not anchor:
            continue
        src = source_fields.get(anchor) or {}

        for task_key, (switch_key, _allow_cc) in TASK_FIELDS.items():
            if task_key not in f:
                continue
            raw = f.get(task_key)

            # 空数组禁令（同角色：modify 保旧值+旧开关，create 剥键+notes）
            if not isinstance(raw, list) or not raw:
                if mode == "modify" and src.get(task_key) is not None:
                    f[task_key] = src.get(task_key)
                    if switch_key in src:
                        f[switch_key] = src.get(switch_key)
                    logger.warning(
                        f"resolve_permissions: {anchor}.{task_key} 为空数组，"
                        f"已保留源配置旧值 {src.get(task_key)!r}")
                else:
                    f.pop(task_key, None)
                    if notes is not None:
                        notes.append("检测到空的审批节点权限数组，已按未提及处理")
                continue

            resolved: List[str] = []
            pending: List = []
            name_matched = False
            for v in raw:
                s = v.strip() if isinstance(v, str) else ""
                if not s:
                    continue
                if process_key is None:
                    # 未绑流程：非 ASCII 已在上面短路阻断，这里只剩
                    # 纯 ASCII 存量 id —— 透传保留，不置开关不追问
                    if s not in resolved:
                        resolved.append(s)
                    continue
                if s in full_ids:
                    # 存量 id 透传（含抄送 id，Add/Edit 组同样不清已有）
                    if s not in resolved:
                        resolved.append(s)
                    continue
                r = resolve_names([s], group_entries[task_key])
                if r.resolved:
                    code = r.resolved[0]
                    if code not in resolved:
                        resolved.append(code)
                    name_matched = True   # 名称匹配 → 本轮语义，置开关
                else:
                    pending.append(r.pending[0])

            # 追问回填：按 header 反查（无第二次 LLM 调用；身份 token 同角色卡）
            still_pending = []
            ignored = False
            for pick in pending:
                header = (f"__task:{anchor}:{task_key}:"
                          f"{semantic_token(pick.semantic)}")
                ans = answers.get(header)
                if ans is None or not str(ans).strip():
                    still_pending.append(pick)
                    continue
                ans = str(ans).strip()
                if ans == IGNORE_LABEL:
                    # 「条」= 单个 pending 名词，同组已解析值绝不连坐丢弃
                    ignored = True
                    continue
                entry = _lookup_by_label(ans, pick.candidates)
                if entry is not None:
                    if entry.code not in resolved:
                        resolved.append(entry.code)
                    name_matched = True
                else:
                    # 自由输入未命中 → 换入用户输入下轮全规则重解析
                    still_pending.append(_repick_with_user_input(pick, ans))

            if resolved or still_pending:
                f[task_key] = resolved + [p.semantic for p in still_pending]
                if name_matched and resolved:
                    f[switch_key] = 1
            elif ignored:
                # 忽略后组内空 → "本次指令未涉及该权限"（modify 保旧值）
                if mode == "modify" and src.get(task_key) is not None:
                    f[task_key] = src.get(task_key)
                    if switch_key in src:
                        f[switch_key] = src.get(switch_key)
                    logger.warning(
                        f"resolve_permissions: {anchor}.{task_key} 本轮权限名词"
                        f"被用户忽略，已保留源配置旧值 {src.get(task_key)!r}")
                else:
                    f.pop(task_key, None)
            else:
                f.pop(task_key, None)
                if notes is not None:
                    notes.append("检测到空的审批节点权限数组，已按未提及处理")
            # 同语义重复名词只发一张卡（header 即身份，同角色卡）
            asked: set = set()
            for pick in still_pending:
                header = (f"__task:{anchor}:{task_key}:"
                          f"{semantic_token(pick.semantic)}")
                if header in asked:
                    continue
                asked.add(header)
                questions.append(_task_question(
                    title, anchor, task_key,
                    pick.semantic, pick.candidates))
    return TaskPermissionOutcome(questions=questions)


# ── 工具步骤统一入口 ───────────────────────────────────────────────

def strip_unresolvable_role_keys(artifact: Dict[str, Any]) -> None:
    """目录不可用降级：剥掉本轮产物中的中文语义角色键（防中文落库）。

    纯 ASCII code 数组视为存量透传原样保留（「不清已有」）；含非 ASCII
    值或空数组视为本轮语义产物，剥除。
    """
    fields = artifact.get(FIELDS)
    if not isinstance(fields, list):
        return
    for f in fields:
        if not isinstance(f, dict):
            continue
        for roles_key in ROLE_FIELDS:
            raw = f.get(roles_key)
            if not isinstance(raw, list) or not raw:
                f.pop(roles_key, None)
                continue
            if any(not (isinstance(v, str) and v.isascii() and v.strip())
                   for v in raw):
                f.pop(roles_key, None)


def run_permission_resolution(state: dict, ctx, *, mode: str) -> None:
    """resolve_permissions 步 / validate 入口的统一幂等入口（角色 + 节点）。

    - 无制品 → 直通（清 pending/阻断标记）
    - 未绑流程且产物带节点语义 → 置 `_task_block_message`（调用方回
       reply-only 终态：不追问、不校验、无 LLM 重试），清 pending
    - 角色目录不可用 → 降级：剥中文语义角色键、不追问不清已有、标记提示
      （节点解析不受组织目录影响，照常执行）
    - 有 pending（含 resume 回填后仍未落定）→ 存 pending 制品并抛追问
    - 全部落定 → 清 pending 标记（resume 语义：解析完成后删除）
    """
    artifact = state.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get(FIELDS):
        state.pop("_permission_pending_artifact", None)
        state.pop("_task_block_message", None)
        return

    answers = state.get("clarify_answers") or {}
    source = state.get("source_artifact") if mode == "modify" else None
    notes = state.setdefault("_permission_notes", [])

    # 节点解析（pack_params 通道；未绑流程阻断优先于一切追问）
    params = (state.get("pack_params") or {}).get("njmind_form") or {}
    task_outcome = resolve_form_task_permissions(
        artifact, params.get("task_keys"), params.get("process_definition_key"),
        answers, source_artifact=source, mode=mode, notes=notes)
    if task_outcome.block_message:
        state["_task_block_message"] = task_outcome.block_message
        state.pop("_permission_pending_artifact", None)
        return
    state.pop("_task_block_message", None)

    # 角色解析（组织目录不可用 → 降级剥键，不产生追问）
    questions: List[str] = list(task_outcome.questions)
    rows = state.get("role_catalog") or []
    if not rows:
        state["_role_catalog_unavailable"] = True
        strip_unresolvable_role_keys(artifact)
    else:
        # 目录恢复可用：清上一轮的降级标志（只置不清会让 summary
        # 在 resume 轮谎报"组织目录不可用"）
        state.pop("_role_catalog_unavailable", None)
        questions = resolve_form_role_permissions(
            artifact, rows, answers,
            source_artifact=source, mode=mode, notes=notes) + questions

    if questions:
        state["_permission_pending_artifact"] = artifact
        raise ClarificationRaised(questions)
    state.pop("_permission_pending_artifact", None)


def copy_role_semantics_to_config(parsed_fields, config: Dict[str, Any]) -> None:
    """create 管线的确定性兜底：ParsedField 语义数组 → 产物字段。

    prompt 已要求 LLM 拷贝（generate.j2），此处只在产物缺键时补拷
    （按 fieldTitleKey 对齐），语义名词仍由 resolve 步统一解析。
    覆盖全部六个语义键：三个 Roles + 三个 TaskKeys。
    """
    by_key = {}
    for f in (config.get(FIELDS) or []):
        if isinstance(f, dict) and f.get(FIELD_KEY):
            by_key[str(f.get(FIELD_KEY))] = f
    for pf in parsed_fields or []:
        target = by_key.get(str(getattr(pf, FIELD_KEY, "") or ""))
        if target is None:
            continue
        for semantic_key in list(ROLE_FIELDS) + list(TASK_FIELDS):
            vals = getattr(pf, semantic_key, None)
            if (isinstance(vals, list) and vals
                    and semantic_key not in target):
                target[semantic_key] = list(vals)
