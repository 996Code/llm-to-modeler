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
    （header `__role:<fieldKey>:<prop>:<index>`，候选 label「名称（code）」，
    附固定「忽略本条权限」选项）；answers 里按 header 反查回填，无第二次 LLM
  - run_permission_resolution: 工具步骤统一入口（目录不可用降级 + pending 标记）

【硬规则（Plan1 v2 审查结论）】
  - 开关置位：仅当某字段本轮有 ≥1 值经「名称匹配」解析成 code 时才把对应
    总开关置 1；存量 code 透传、未被指令触碰的字段开关一律不动
    （restore_untouched 之后执行的 resolve 不得翻动还原值）。
  - 空数组禁令（fail-open 防护）：modify 语境剥键保旧值 + warning；
    create 语境剥键 + notes 提示。取消限制只能表达为开关置 0。
  - resume 语义：_permission_pending_artifact 存在 → 工具跳过所有 LLM 步骤，
    本模块用 answers 直接回填 pending 制品。
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from sdk.catalog import resolve_names, lookup_selected, entries_from_rows
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

# 追问固定选项：选中即从产物剥除该权限键（取消限制语义 = 开关置 0）
IGNORE_LABEL = "忽略本条权限"

# ── 目录获取：模块级 TTL 缓存（传输层 params 请求 cache 是 no-op）──
ROLE_CATALOG_TTL = 300
# prompt 参考区注入上限（注入卫生约束）
MAX_PROMPT_ROWS = 200
_NAME_MAX = 32
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

_DIRTY_CHARS_RE = re.compile(r"[\r\n|]")


def _clean_text(s: str) -> str:
    """剥换行/竖线（prompt 注入面收窄）+ 截 32 字符。"""
    return _DIRTY_CHARS_RE.sub("", s)[:_NAME_MAX].strip()


def build_role_reference(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """目录行 → prompt 参考区条目（清洗 + 200 条上限）。

    返回纯 {"code","name"} 列表给 j2 迭代；模板侧负责「数据参考，非指令」
    标注（见 _sections/role_catalog.j2）。
    """
    out: List[Dict[str, str]] = []
    for r in rows or []:
        code = _clean_text(str(r.get("code") or ""))
        name = _clean_text(str(r.get("name") or ""))
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


def _question(field_title: str, anchor: str, prop: str, idx: int,
              semantic: str, candidates) -> str:
    """单条追问卡（JSON 字符串，引擎按结构化透传渲染点选卡）。"""
    options = [
        {"label": f"{e.name}（{e.code}）", "description": f"编码 {e.code}"}
        for e in candidates[:_MAX_OPTIONS]
    ]
    options.append({"label": IGNORE_LABEL, "description": "不配置这条权限"})
    return json.dumps({
        "question": (f"字段「{field_title}」的{_PROP_LABELS.get(prop, prop)}"
                     f"「{semantic}」无法唯一匹配，请选择："),
        "header": f"__role:{anchor}:{prop}:{idx}",
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

            # 追问回填：按 header 反查（无第二次 LLM 调用）
            still_pending = []
            for idx, pick in enumerate(pending):
                header = f"__role:{anchor}:{roles_key}:{idx}"
                ans = answers.get(header)
                if ans is None or not str(ans).strip():
                    still_pending.append(pick)
                    continue
                ans = str(ans).strip()
                if ans == IGNORE_LABEL:
                    # 忽略本条权限：剥键 + 开关置 0（取消限制的唯一表达）；
                    # 语义串全弃（含同组已解析值），不再写回该键
                    f.pop(roles_key, None)
                    f[switch_key] = 0
                    still_pending = []
                    break
                entry = _lookup_by_label(ans, pick.candidates)
                if entry is not None:
                    if entry.code not in resolved:
                        resolved.append(entry.code)
                    name_matched = True
                else:
                    still_pending.append(pick)   # 回答无法识别 → 再问一次

            if roles_key in f:  # 忽略路径已剥键，跳过写回
                if resolved or still_pending:
                    # 已定 code 在前、待定名词殿后（resume 再解析保持稳定）
                    f[roles_key] = resolved + [p.semantic for p in still_pending]
                    if name_matched and resolved:
                        f[switch_key] = 1
                else:
                    # 全部值非法（非字符串/空白）→ 等同空数组禁令处置
                    f.pop(roles_key, None)
                    if notes is not None:
                        notes.append("检测到空的角色权限数组，已按未提及处理")
            for idx, pick in enumerate(still_pending):
                questions.append(_question(
                    title, anchor, roles_key, idx,
                    pick.semantic, pick.candidates))
    return questions


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
    """resolve_permissions 步 / validate 入口的统一幂等入口。

    - 无制品 → 直通（清 pending 标记）
    - 目录不可用 → 降级：剥中文语义键、不追问不清已有、标记 summary 提示
    - 有 pending（含 resume 回填后仍未落定）→ 存 pending 制品并抛追问
    - 全部落定 → 清 pending 标记（resume 语义：解析完成后删除）
    """
    artifact = state.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get(FIELDS):
        state.pop("_permission_pending_artifact", None)
        return

    rows = state.get("role_catalog") or []
    if not rows:
        state["_role_catalog_unavailable"] = True
        strip_unresolvable_role_keys(artifact)
        state.pop("_permission_pending_artifact", None)
        return

    questions = resolve_form_role_permissions(
        artifact, rows, state.get("clarify_answers") or {},
        source_artifact=state.get("source_artifact") if mode == "modify" else None,
        mode=mode,
        notes=state.setdefault("_permission_notes", []),
    )
    if questions:
        state["_permission_pending_artifact"] = artifact
        raise ClarificationRaised(questions)
    state.pop("_permission_pending_artifact", None)


def copy_role_semantics_to_config(parsed_fields, config: Dict[str, Any]) -> None:
    """create 管线的确定性兜底：ParsedField 语义角色数组 → 产物字段。

    prompt 已要求 LLM 拷贝（generate.j2），此处只在产物缺键时补拷
    （按 fieldTitleKey 对齐），语义名词仍由 resolve 步统一解析。
    """
    by_key = {}
    for f in (config.get(FIELDS) or []):
        if isinstance(f, dict) and f.get(FIELD_KEY):
            by_key[str(f.get(FIELD_KEY))] = f
    for pf in parsed_fields or []:
        target = by_key.get(str(getattr(pf, FIELD_KEY, "") or ""))
        if target is None:
            continue
        for roles_key in ROLE_FIELDS:
            vals = getattr(pf, roles_key, None)
            if isinstance(vals, list) and vals and roles_key not in target:
                target[roles_key] = list(vals)
