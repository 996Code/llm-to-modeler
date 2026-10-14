"""BPM 审批人与角色分支解析 —— 名词解析、追问与回填（pack 私有）。

【模块定位】
njmind_bpm pack 的身份解析层：把 LLM draft 中的审批人/角色「中文名词」
（中间态契约，见 prompts/generate.j2）确定性解析成真实 id/code。
中文名词只存在于 LLM 中间产物，render 输入只允许真实值（引擎通用层
零领域知识，"checkedRoleIds 对应角色目录"这层映射只住在这里）。

【职责】
  - resolve_bpm_permissions: draft 就地解析；无法直选的名词产出追问卡
    （header `__bpm_role:/__bpm_dep:/__bpm_user:` + 名词身份短哈希，
    跨轮稳定，复用 Plan 1 njmind_form 的 header 生成方式）；answers 里
    按 header 反查回填，无第二次 LLM。人员搜索零结果 → 追问更准确
    姓名或工号；多结果 → 追问选项（label「姓名｜部门」唯一化）。
  - 三条解析通道：
      approveRules[].approveOptRangeSettings.checkedRoleIds 中文名词
        → [{roleId: 真实id, depAndSupDep: 0, condition: None}]
      checkedDepIds → 真实部门 id 数组
      checkedUserIds → api.search_users(名词)（匿名 MCP 端点）
      branchRules[].roleRules[].values 中文名词 → 角色 code 数组

【降级语义（目录卫生）】
  - 角色目录空 → checkedRoleIds/roleRules.values 的非 ASCII 名词剥除
    （ASCII/对象视为存量透传保留），不追问、不清已有
  - 部门目录空 → checkedDepIds 同构剥除
  - 人员搜索不可用（网络失败）→ checkedUserIds
    非 ASCII 名词剥除；搜索可用但零命中 ≠ 降级，照常追问
  - 幂等：已解析值（对象/ASCII id/code）原样透传，render 入口重跑
    无副作用（重试环路不变量）
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from sdk.catalog import (resolve_names, lookup_selected, entries_from_rows,
                         clean_text, semantic_token, IGNORE_LABEL)
from domains.njmind_bpm import keys as K

logger = logging.getLogger(__name__)

# draft 内的权限设置键（designer 契约，Task 2 已锁定类型）
SETTINGS = "approveOptRangeSettings"
CHECKED_ROLE_IDS = "checkedRoleIds"
CHECKED_DEP_IDS = "checkedDepIds"
CHECKED_USER_IDS = "checkedUserIds"
ROLE_RULES = "roleRules"
VALUES = "values"

# 追问固定选项：「条」=单个 pending 名词——仅丢弃该名词，同组已解析值
# 绝不连坐丢弃
# 追问卡候选上限（前端自动附"其他"自由输入，超限仍可手输名称）
_MAX_OPTIONS = 50


def _question(question: str, header: str, options: List[Dict[str, str]]) -> str:
    """单条追问卡（JSON 字符串，引擎按结构化透传渲染点选卡）。"""
    options = list(options[:_MAX_OPTIONS])
    options.append({"label": IGNORE_LABEL, "description": "不配置这条权限"})
    return json.dumps({
        "question": question,
        "header": header,
        "options": options,
    }, ensure_ascii=False)


def _is_ascii_id(v: Any) -> bool:
    """ASCII 字符串 = 存量 id/code 透传（幂等重跑不重查目录）。"""
    return isinstance(v, str) and v.isascii() and bool(v.strip())


# ── 目录行清洗 ─────────────────────────────────────────────────

def _clean_role_rows(rows) -> List[Dict[str, Any]]:
    """角色目录行级清洗：只要 dict 且 id/code/name 齐全（脏行不进目录）。"""
    out = []
    for r in rows or []:
        if (isinstance(r, dict) and isinstance(r.get("id"), str)
                and isinstance(r.get("code"), str)
                and isinstance(r.get("name"), str) and r["name"]):
            out.append({"id": r["id"], "code": r["code"], "name": r["name"]})
    return out


def _clean_dept_rows(rows) -> List[Dict[str, Any]]:
    """部门目录行级清洗：id/name 齐全（id 归一为字符串作匹配 code）。"""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if isinstance(rid, int):
            rid = str(rid)
        if isinstance(rid, str) and isinstance(r.get("name"), str) and r["name"]:
            out.append({"id": rid, "name": r["name"]})
    return out


# ── 追问回填（按 header 反查，无第二次 LLM）─────────────────────

def _lookup_role(answer: str, candidates):
    """角色答案反查：优先 pack 自建 label「名称（code）」，兜底精确相等。"""
    for e in candidates:
        if answer == f"{e.name}（{e.code}）":
            return e
    return lookup_selected(answer, candidates)


# ── 人员搜索候选（label「姓名｜部门」唯一化）────────────────────

def _user_options(results: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """搜索结果 → 追问卡选项；label 重复时追加序号唯一化（确定性，
    resume 重放同一搜索结果时生成相同 label 集合，反查可命中）。"""
    options: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    for r in results[:_MAX_OPTIONS]:
        label = clean_text(str(r.get("label") or ""))
        if not label:
            continue
        n = seen.get(label, 0) + 1
        seen[label] = n
        if n > 1:
            label = f"{label}（{n}）"
        options.append({"label": label, "description": "人员"})
    return options


def _user_answer_id(answer: str, results, api) -> Optional[str]:
    """人员答案 → id：先按候选 label 反查（点选），查不中再按答案文本
    重新搜索（前端"其他"自由输入：更准确姓名/工号），单结果直选。

    唯一化后缀：多候选同名时 options 的 label 会加"（n）"后缀
    （_user_options），反查时先剥后缀按序号定位对应候选。
    """
    for r in results or []:
        if str(r.get("label") or "") == answer:
            return r.get("id")
    m = re.search(r"（(\d+)）$", answer)
    if m and results:
        base = answer[:m.start()]
        nth = int(m.group(1))
        same = [r for r in results if str(r.get("label") or "") == base]
        if 1 <= nth <= len(same):
            return same[nth - 1].get("id")
    if api is None:
        return None
    retry = None
    try:
        retry = api.search_users(answer)
    except Exception as e:
        logger.warning(f"search_users({answer}) failed: {e}")
        return None
    if isinstance(retry, list) and len(retry) == 1:
        return retry[0].get("id")
    return None


# ── 主入口 ─────────────────────────────────────────────────────

def resolve_bpm_permissions(
    draft: Dict[str, Any],
    roles: List[Dict[str, Any]],
    departments: List[Dict[str, Any]],
    answers: Dict[str, Any],
    api,
    notes: Optional[List[str]] = None,
) -> List[str]:
    # notes: 可写列表（调用方传 state 持久通道），降级剥除时写入用户
    # 可见提示——权限被剥只留服务端日志对用户不诚实（form 侧同款模式）
    """就地解析 draft 中的审批人/角色分支名词；返回追问卡列表（空=全部落定）。

    Args:
        draft: LLM 产物（approveRules/branchRules 就地改写：名词→id/code）。
        roles: 角色目录行 [{"id","code","name"}]（空=降级剥除）。
        departments: 部门目录行 [{"id","name"}]（空=降级剥除）。
        answers: 追问回答 {header: label}（引擎注入的 clarify_answers）。
        api: BpmModelerAPI（search_users 通道；None/无方法=人员降级）。

    Returns:
        JSON 追问卡字符串列表；每项一问、单选、附"忽略本条权限"。
    """
    questions: List[str] = []
    if not isinstance(draft, dict):
        return questions

    role_rows = _clean_role_rows(roles)
    dept_rows = _clean_dept_rows(departments)
    role_entries = entries_from_rows(role_rows)
    role_id_by_code = {r["code"]: r["id"] for r in role_rows}
    # 部门行无 code 键：id 归一为字符串作匹配 code（命中后原样回写 id）
    dept_entries = entries_from_rows(
        [{"code": r["id"], "name": r["name"]} for r in dept_rows])
    search = getattr(api, "search_users", None) if api is not None else None

    for rule in draft.get(K.APPROVE_RULES) or []:
        if not isinstance(rule, dict):
            continue
        anchor = str(rule.get(K.NODE_KEY_REF) or "")
        settings = rule.get(SETTINGS)
        if not isinstance(settings, dict):
            continue

        settings[CHECKED_ROLE_IDS] = _resolve_roles(
            settings.get(CHECKED_ROLE_IDS), anchor, CHECKED_ROLE_IDS,
            role_entries, role_id_by_code, answers, questions, notes)
        settings[CHECKED_DEP_IDS] = _resolve_deps(
            settings.get(CHECKED_DEP_IDS), anchor,
            dept_entries, answers, questions, notes)
        settings[CHECKED_USER_IDS] = _resolve_users(
            settings.get(CHECKED_USER_IDS), anchor, answers, search, questions, notes)
        for key in (CHECKED_ROLE_IDS, CHECKED_DEP_IDS, CHECKED_USER_IDS):
            if settings.get(key) == []:
                settings.pop(key, None)
        if not settings:
            rule.pop(SETTINGS, None)

    for br in draft.get(K.BRANCH_RULES) or []:
        if not isinstance(br, dict):
            continue
        anchor = str(br.get(K.EDGE_KEY_REF) or "")
        rrs = br.get(ROLE_RULES)
        if not isinstance(rrs, list):
            continue
        for rr in rrs:
            if not isinstance(rr, dict):
                continue
            rr[VALUES] = _resolve_role_values(
                rr.get(VALUES), anchor, role_entries, answers, questions, notes)
            if not rr.get(VALUES):
                rr.pop(VALUES, None)
        # values 剥空的 roleRule 条目整体剔除（无 values 的角色条件无意义）
        br[ROLE_RULES] = [rr for rr in rrs
                          if isinstance(rr, dict) and rr.get(VALUES)]
        if not br[ROLE_RULES]:
            br.pop(ROLE_RULES, None)
    return questions


# ── checkedRoleIds：中文名词 → [{roleId, depAndSupDep, condition}] ──

def _resolve_roles(raw, anchor: str, prop: str, role_entries,
                   role_id_by_code: Dict[str, str],
                   answers, questions: List[str],
                   notes: Optional[List[str]] = None) -> List:
    """角色名词 → 对象数组（roleId=目录真实 id）。幂等：对象透传。

    目录空（降级）：非 ASCII 名词剥除，不追问。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    out: List = []
    pending: List = []
    for v in raw:
        if isinstance(v, dict):
            out.append(v)          # 已解析对象透传（幂等）
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        if _is_ascii_id(v) and not any(e.code == v for e in role_entries):
            out.append(v)          # 存量 ASCII id 透传（目录外也不追问）
            continue
        if not role_entries:
            if not v.isascii():
                logger.warning(
                    f"resolve_permissions: 角色目录不可用，"
                    f"{anchor}.{prop} 中文名词「{v}」已剥除")
                if notes is not None:
                    notes.append(f"{anchor}.{prop} 的「{v}」：角色目录不可用，已按未配置处理")
            else:
                out.append(v)
            continue
        r = resolve_names([v.strip()], role_entries)
        if r.resolved:
            out.append({"roleId": role_id_by_code.get(r.resolved[0]),
                        "depAndSupDep": 0, "condition": None})
        else:
            pending.append(r.pending[0])

    still: List = []
    for pick in pending:
        header = f"__bpm_role:{anchor}:{prop}:{semantic_token(pick.semantic)}"
        ans = answers.get(header) if isinstance(answers, dict) else None
        ans = str(ans).strip() if ans is not None and str(ans).strip() else None
        if ans is None:
            still.append(pick)
            continue
        if ans == IGNORE_LABEL:
            continue               # 「条」=单名词，同组已解析值不连坐
        entry = _lookup_role(ans, pick.candidates)
        if entry is not None:
            out.append({"roleId": role_id_by_code.get(entry.code),
                        "depAndSupDep": 0, "condition": None})
        else:
            still.append(pick)     # 回答无法识别 → 再问一次

    asked: set = set()
    for pick in still:
        header = f"__bpm_role:{anchor}:{prop}:{semantic_token(pick.semantic)}"
        if header in asked:
            continue
        asked.add(header)
        questions.append(_question(
            f"审批规则「{anchor}」的角色「{pick.semantic}」无法唯一匹配，请选择：",
            header,
            [{"label": f"{e.name}（{e.code}）", "description": f"编码 {e.code}"}
             for e in pick.candidates]))
    return out + [p.semantic for p in still]


# ── checkedDepIds：中文名词 → 真实部门 id 数组 ──────────────────

def _resolve_deps(raw, anchor: str, dept_entries,
                  answers, questions: List[str],
                  notes: Optional[List[str]] = None) -> List:
    """部门名词 → id 数组（code=str(id) 命中后原样回写）。幂等：ASCII id
    透传。目录空（降级）剥中文。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    out: List = []
    pending: List = []
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            if v is not None:
                out.append(v)      # 存量 id（int 等）透传
            continue
        if _is_ascii_id(v) and not any(e.code == v for e in dept_entries):
            out.append(v)
            continue
        if not dept_entries:
            if not v.isascii():
                logger.warning(
                    f"resolve_permissions: 部门目录不可用，"
                    f"{anchor}.{CHECKED_DEP_IDS} 中文名词「{v}」已剥除")
                if notes is not None:
                    notes.append(f"{anchor} 的部门「{v}」：部门目录不可用，已按未配置处理")
            else:
                out.append(v)
            continue
        r = resolve_names([v.strip()], dept_entries)
        if r.resolved:
            out.append(r.resolved[0])   # code=str(id)，即部门真实 id
        else:
            pending.append(r.pending[0])

    still: List = []
    for pick in pending:
        header = f"__bpm_dep:{anchor}:{CHECKED_DEP_IDS}:{semantic_token(pick.semantic)}"
        ans = answers.get(header) if isinstance(answers, dict) else None
        ans = str(ans).strip() if ans is not None and str(ans).strip() else None
        if ans is None:
            still.append(pick)
            continue
        if ans == IGNORE_LABEL:
            continue
        entry = _lookup_role(ans, pick.candidates)
        if entry is not None:
            out.append(entry.code)      # 部门 id（code=str(id)）
        else:
            still.append(pick)

    asked: set = set()
    for pick in still:
        header = f"__bpm_dep:{anchor}:{CHECKED_DEP_IDS}:{semantic_token(pick.semantic)}"
        if header in asked:
            continue
        asked.add(header)
        questions.append(_question(
            f"审批规则「{anchor}」的部门「{pick.semantic}」无法唯一匹配，请选择：",
            header,
            [{"label": f"{e.name}（{e.code}）", "description": f"部门 id {e.code}"}
             for e in pick.candidates]))
    return out + [p.semantic for p in still]


# ── checkedUserIds：姓名 → api.search_users 直选/追问 ───────────

def _resolve_users(raw, anchor: str, answers, search, questions: List[str],
                  notes: Optional[List[str]] = None) -> List:
    """人员名词 → id 数组（经受控搜索）。

    - 单结果直选；零结果 → 追问"更准确的姓名或工号"；
    - 多结果 → 追问选项（label「姓名｜部门」唯一化）；
    - 搜索不可用（未配 token/失败，返回 None）→ 降级剥中文，不追问；
    - 幂等：非字符串（已解析 id）与 ASCII 值透传，不重查。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    out: List = []
    pending: List = []   # (semantic, results)
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            if v is not None:
                out.append(v)
            continue
        if v.isascii():
            out.append(v)          # 存量 ASCII id 透传（幂等）
            continue
        if not callable(search):
            logger.warning(
                f"resolve_permissions: 人员搜索不可用，"
                f"{anchor}.{CHECKED_USER_IDS} 中文名词「{v}」已剥除")
            if notes is not None:
                notes.append(f"{anchor} 的审批人「{v}」：人员搜索不可用，已按未配置处理")
            continue
        try:
            results = search(v.strip())
        except Exception as e:
            logger.warning(f"search_users({v}) failed: {e}")
            results = None
        if results is None:
            logger.warning(
                f"resolve_permissions: 人员搜索不可用，"
                f"{anchor}.{CHECKED_USER_IDS} 中文名词「{v}」已剥除")
            if notes is not None:
                notes.append(f"{anchor} 的审批人「{v}」：人员搜索不可用，已按未配置处理")
            continue
        if not isinstance(results, list):
            results = []
        if len(results) == 1:
            out.append(results[0].get("id"))
        else:
            pending.append((v.strip(), results))

    still: List = []
    for semantic, results in pending:
        header = f"__bpm_user:{anchor}:{CHECKED_USER_IDS}:{semantic_token(semantic)}"
        ans = answers.get(header) if isinstance(answers, dict) else None
        ans = str(ans).strip() if ans is not None and str(ans).strip() else None
        if ans is None:
            still.append((semantic, results))
            continue
        if ans == IGNORE_LABEL:
            continue
        uid = _user_answer_id(ans, results, _api_of(search))
        if uid is not None:
            out.append(uid)
        else:
            still.append((semantic, results))

    asked: set = set()
    for semantic, results in still:
        header = f"__bpm_user:{anchor}:{CHECKED_USER_IDS}:{semantic_token(semantic)}"
        if header in asked:
            continue
        asked.add(header)
        if results:
            questions.append(_question(
                f"审批规则「{anchor}」的人员「{semantic}」匹配到多位，请选择：",
                header, _user_options(results)))
        else:
            questions.append(_question(
                f"未找到与「{semantic}」匹配的人员，请提供更准确的姓名或工号：",
                header, []))
    return out + [s for s, _ in still]


def _api_of(search):
    """search 闭包绑定的 api 对象（自由输入重搜索用）；取不到返回 None。"""
    return getattr(search, "__self__", None)


# ── roleRules.values：中文名词 → 角色 code 数组 ─────────────────

def _resolve_role_values(raw, anchor: str, role_entries,
                         answers, questions: List[str],
                         notes: Optional[List[str]] = None) -> List:
    """角色分支 values 名词 → 角色 code 数组。幂等：ASCII code 透传。

    目录空（降级）：非 ASCII 名词剥除，不追问。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    out: List = []
    pending: List = []
    for v in raw:
        if not isinstance(v, str) or not v.strip():
            continue
        if _is_ascii_id(v) and not any(e.code == v for e in role_entries):
            out.append(v)          # 存量 code 透传（目录外不追问）
            continue
        if not role_entries:
            if not v.isascii():
                logger.warning(
                    f"resolve_permissions: 角色目录不可用，"
                    f"{anchor}.roleRules 中文名词「{v}」已剥除")
                if notes is not None:
                    notes.append(f"{anchor}.roleRules 的「{v}」：角色目录不可用，已按未配置处理")
            else:
                out.append(v)
            continue
        r = resolve_names([v.strip()], role_entries)
        if r.resolved:
            if r.resolved[0] not in out:
                out.append(r.resolved[0])
        else:
            pending.append(r.pending[0])

    still: List = []
    for pick in pending:
        header = f"__bpm_role:{anchor}:{ROLE_RULES}:{semantic_token(pick.semantic)}"
        ans = answers.get(header) if isinstance(answers, dict) else None
        ans = str(ans).strip() if ans is not None and str(ans).strip() else None
        if ans is None:
            still.append(pick)
            continue
        if ans == IGNORE_LABEL:
            continue
        entry = _lookup_role(ans, pick.candidates)
        if entry is not None:
            if entry.code not in out:
                out.append(entry.code)
        else:
            still.append(pick)

    asked: set = set()
    for pick in still:
        header = f"__bpm_role:{anchor}:{ROLE_RULES}:{semantic_token(pick.semantic)}"
        if header in asked:
            continue
        asked.add(header)
        questions.append(_question(
            f"分支「{anchor}」的角色条件「{pick.semantic}」无法唯一匹配，请选择：",
            header,
            [{"label": f"{e.name}（{e.code}）", "description": f"编码 {e.code}"}
             for e in pick.candidates]))
    return out + [p.semantic for p in still]
