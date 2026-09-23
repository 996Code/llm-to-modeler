"""脚本生成工具的公共层 —— 目录递归 / pack_params 解析 / 消息标记 / 白名单。

【模块定位】
generate_js_script 与 generate_filter_sql 的共享纯逻辑（零 LLM、零上游调用）：
- 递归字段目录：主表 → childFormFieldConfigVo → labelPages[].childFormFieldConfigVo
  （与 designer useWidgetDisplay.findInChildren / 宿主 filterVo 同构），
  行带路径前缀，fieldTitleKey 全表唯一（postprocess 递归去重保证）。
- pack_params 解析：弹框场景 designer 经 {"njmind_form": {...}} 显式定位
  （script_field / script_slot / script_conf / fields / current_sql / current_script）。
- 消息标记：designer 发送时拼 "[script:js]"/"[script:sql]" 前缀——router 据此
  确定性分流（不调 LLM），工具内剥除后取用户原文。
- SQL 白名单：系统列（TableDefaultField 事实源）按主/子表场景分流。

【上下文蒸馏原则】（设计定稿）
LLM 只见目录不见全量 JSON（仿 modify_form build_catalog 14KB→1KB 模式）：
每字段一行 `#序号 key | 标题 | 类型 | 必填 | 选项:value=label`；SELECT 选项映射
必须进目录（formData 值是 optionValue 数字码，这是脚本错误最大来源）。
"""
from typing import Dict, List, Optional, Set

import logging

from domains.njmind_form.keys import FIELDS, FIELD_KEY, FIELD_TITLE

logger = logging.getLogger(__name__)

# ── 消息标记前缀（router 确定性分流的契约，designer 侧约定相同值） ──

JS_MARK = "[script:js]"
SQL_MARK = "[script:sql]"

# ── JS 脚本 Profile（与设计器固定配置位对应，不接受任意路径） ──

JS_SCRIPT_PROFILES = {
    "field_change": {
        "context": "form",
        "prompt": "js_field_change_generate",
        "return_required": False,
        "field_namespace": "form",
        "label": "字段值变化事件",
    },
    "field_url": {
        "context": "form",
        "prompt": "js_url_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "字段外链",
    },
    "button_before": {
        "context": "button",
        "prompt": "js_button_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "按钮前置脚本",
    },
    "button_after": {
        "context": "button",
        "prompt": "js_button_generate",
        "return_required": False,
        "field_namespace": "row",
        "label": "按钮后置脚本",
    },
    "button_hidden": {
        "context": "button",
        "prompt": "js_button_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "按钮隐藏条件",
    },
    "button_disabled": {
        "context": "button",
        "prompt": "js_button_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "按钮禁用条件",
    },
    "button_url": {
        "context": "button",
        "prompt": "js_url_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "按钮跳转链接",
    },
    "button_custom": {
        "context": "button",
        "prompt": "js_button_generate",
        "return_required": False,
        "field_namespace": "row",
        "label": "按钮自定义事件",
    },
    "table_url": {
        "context": "list",
        "prompt": "js_url_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "列表链接",
    },
    "table_formatter": {
        "context": "list",
        "prompt": "js_table_formatter_generate",
        "return_required": True,
        "field_namespace": "row",
        "label": "列表格式化",
    },
    "table_visibility": {
        "context": "list_visibility",
        "prompt": "js_table_visibility_generate",
        "return_required": True,
        "field_namespace": "none",
        "label": "列表字段隐藏条件",
    },
}


def get_js_script_profile(profile_id: str) -> dict | None:
    """按固定 Profile 标识读取 JS 脚本配置。"""
    return JS_SCRIPT_PROFILES.get(profile_id)


def strip_script_mark(user_input: str) -> str:
    """剥掉路由标记前缀，返回用户原文（多个标记/前后空白都容忍）。"""
    s = (user_input or "").strip()
    for mark in (JS_MARK, SQL_MARK):
        if s.startswith(mark):
            s = s[len(mark):].strip()
    return s


def script_mark_of(user_input: str) -> Optional[str]:
    """读取消息里的脚本标记（router 分流与工具定位共用）。"""
    s = (user_input or "").strip()
    for mark in (JS_MARK, SQL_MARK):
        if s.startswith(mark):
            return mark
    return None


# ── 字段类型码 → 类型名（pack 内静态映射，零上游调用） ──
# 与 prompts/_sections/field_types.j2 同源；REMOTE 类标注值形态（ID 串）。

_TYPE_NAMES = {
    0: "TEXT", 1: "NUMBER", 2: "DATE", 3: "FILE_UPLOAD",
    4: "SELECT", 5: "MULTIPLE_SELECT", 6: "DEPARTMENT", 7: "USER",
    8: "AUTO_NUMBER", 9: "CHILD_FORM", 10: "RELATION_DATA",
    11: "BAR_CODE", 12: "SEGMENT", 13: "TAB", 14: "TEXT_SHOW",
    15: "CASCADE", 16: "RICH_TEXT", 17: "CITE_RECORD",
    18: "CONTAINER", 19: "SIGNATURE",
}

# 值为 ID 字符串（多值逗号分隔）的类型——目录里标注，prompt 的取值形态段引用。
# 与 lesscode form-utils FORM_FIELD_TYPES_WITH_LABEL_SUFFIX 对齐：DEPARTMENT(6)/
# USER(7)/RELATION_DATA(10)/CASCADE(15) 有 _label 伴生；CITE_RECORD(17) 无
# _label 伴生但值同为记录 ID 串。
_ID_VALUE_TYPES = {6, 7, 10, 15, 17}


def type_name(code) -> str:
    try:
        return _TYPE_NAMES.get(int(code), f"TYPE{code}")
    except (TypeError, ValueError):
        return "TYPE?"


def _option_summary(field: dict) -> str:
    """SELECT/MULTIPLE_SELECT 的选项摘要 `value=label,...`（截断防爆目录）。

    真实结构 optionSettings.optionFields[{optionLabel, optionValue}]；
    兼容个别模板/测试用的简形态 options。
    """
    os_ = field.get("optionSettings")
    option_fields = []
    if isinstance(os_, dict) and isinstance(os_.get("optionFields"), list):
        option_fields = os_["optionFields"]
    elif isinstance(field.get("options"), list):
        option_fields = field["options"]
    if not option_fields:
        return ""
    parts = []
    for o in option_fields[:8]:
        if isinstance(o, dict):
            parts.append(f"{o.get('optionValue')}={o.get('optionLabel')}")
        else:
            parts.append(str(o))
    suffix = "" if len(option_fields) <= 8 else ",…"
    return ",".join(parts) + suffix


def _catalog_line(field: dict, seq: int, path: str, is_child: bool) -> str:
    """单个字段的目录行（蒸馏格式，路径前缀区分主/子表/标签页归属）。"""
    key = str(field.get(FIELD_KEY, ""))
    title = str(field.get(FIELD_TITLE, ""))
    code = field.get("formFieldType")
    tname = type_name(code)
    prefix = f"{path}/" if path else ""
    parts = [f"#{seq} {prefix}{key}" if path else f"#{seq} {key}",
             title, tname]
    if field.get("isRequiredField"):
        parts.append("必填")
    if isinstance(code, (int, float)) and int(code) in _ID_VALUE_TYPES:
        parts.append("值=ID串")
    opts = _option_summary(field)
    if opts:
        parts.append(f"选项:{opts}")
    child_hint = "|子表" if is_child else ""
    return " | ".join(parts) + child_hint


def build_field_catalog(fields: List[dict], max_lines: int = 120) -> Dict[str, object]:
    """递归构建字段目录（主表 → 子表/容器 → 标签页，两层嵌套即收口）。

    嵌套事实源（designer findInChildren / 宿主 filterVo）：
      - childFormFieldConfigVo：子表单(9)/容器(18)/引用记录(17)的内嵌字段
      - labelPages[].childFormFieldConfigVo：标签页(13)每页字段（可再嵌套一层）
    运行时字段树最多两层嵌套（useFormInit 对嵌套容器只支持一级），目录递归
    与之对齐；更深的理论嵌套按同规则继续下钻，防御即可。

    Returns:
        {"text": 目录文本, "keys": 全量递归 key 集合, "top_keys": 主表顶层 key 集合}
    """
    lines: List[str] = []
    keys: Set[str] = set()
    top_keys: Set[str] = set()
    seq = 0

    def _walk(field_list: Optional[List[dict]], path: str, is_child: bool,
              depth: int = 0) -> None:
        nonlocal seq
        if not field_list or depth > 3:
            return
        for f in field_list:
            if not isinstance(f, dict):
                continue
            key = str(f.get(FIELD_KEY, ""))
            seq += 1
            if seq <= max_lines:
                lines.append(_catalog_line(f, seq, path, is_child))
            if key:
                keys.add(key)
                if not path:
                    top_keys.add(key)
            code = f.get("formFieldType")
            # 子表单/容器：子字段挂 childFormFieldConfigVo
            child = f.get("childFormFieldConfigVo")
            if isinstance(child, list) and child:
                child_path = f"{path}/{key}" if path else key
                _walk(child, child_path, True, depth + 1)
            # 标签页：每页独立字段数组
            if isinstance(code, (int, float)) and int(code) == 13:
                for tab in (f.get("labelPages") or []):
                    if isinstance(tab, dict):
                        tab_key = str(tab.get("tabId") or tab.get("tabName") or "")
                        tab_path = f"{path}/{key}:{tab_key}" if path else f"{key}:{tab_key}"
                        _walk(tab.get("childFormFieldConfigVo"), tab_path, True, depth + 1)

    _walk(fields or [], "", False)
    if seq > max_lines:
        lines.append(f"... 共 {seq} 个字段（目录截断）")
    return {"text": "\n".join(lines), "keys": keys, "top_keys": top_keys}


def build_external_field_catalog(fields: List[dict]) -> Dict[str, object]:
    """构建前端规范化字段列表的扁平目录。"""
    lines: List[str] = []
    keys: Set[str] = set()

    for seq, field in enumerate(fields, 1):
        key = str(field.get("fieldTitleKey", ""))
        title = str(field.get("fieldTitleText", ""))
        type_name_ = str(field.get("typeName", ""))
        lines.append(f"#{seq} {key} | {title} | {type_name_}")
        if key:
            keys.add(key)

    return {"text": "\n".join(lines), "keys": keys, "top_keys": set(keys)}


# ── pack_params 解析（弹框场景的显式定位；悬浮窗场景为空走 LLM 推断） ──

PACK_NAME = "njmind_form"


def script_params(state: dict) -> dict:
    """取 njmind_form 段的脚本参数（缺省空 dict，永不抛错）。"""
    params = (state.get("pack_params") or {}).get(PACK_NAME) or {}
    return params if isinstance(params, dict) else {}


def render_prompt(ctx, name: str, **vars) -> str:
    """渲染本域 prompt（prompt_loader 由 Dispatcher 注入；缺失时降级空串）。"""
    if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
        return ctx.prompt_loader.render(PACK_NAME, name, **vars)
    logger.warning(f"No prompt_loader for {name}")
    return ""


# ── JS 脚本位（与设计器 UI 同源：FieldPermissions + 子表明细两码） ──

JS_SLOTS = {
    "editFieldCode": "可编辑",
    "showFieldAddCode": "新增时可见",
    "showFieldDetailCode": "查看时可见",
    # 子表单字段本体两码（childFormViewDetailConfig / childFormEditDetailConfig
    # 的内嵌键），签名多一个 row
    "viewDetailCode": "可查看已有明细",
    "editDetailCode": "可编辑已有明细",
}

# row 场景的两个 slot（签名 ({raw, formData, userInfo, row})）
ROW_SLOTS = {"viewDetailCode", "editDetailCode"}

# row 场景 slot → 字段对象上的宿主配置键
_ROW_SLOT_HOST = {
    "viewDetailCode": "childFormViewDetailConfig",
    "editDetailCode": "childFormEditDetailConfig",
}


def slot_host_key(slot: str) -> str:
    """slot → 字段对象上的宿主键（普通三码即自身；row 两码在其宿主配置内）。"""
    return _ROW_SLOT_HOST.get(slot, slot)


def normalize_slot(slot: str) -> str:
    """LLM/前端可能回中文场景名或自由文本，归一回三个英文键之一。"""
    if slot in JS_SLOTS:
        return slot
    if not isinstance(slot, str):
        return ""
    for k, v in JS_SLOTS.items():
        if slot == v or v in slot or k in slot:
            return k
    return ""


def read_existing_script(field: Optional[dict], slot: str) -> str:
    """从画布字段对象读现有脚本（"改一下"场景的修改基线）。

    row 两码存在宿主配置对象内（childFormViewDetailConfig.viewDetailCode）。
    """
    if not field:
        return ""
    host_key = slot_host_key(slot)
    if host_key != slot:
        host = field.get(host_key) or {}
        return str(host.get(slot) or "")
    return str(field.get(slot) or "")


def find_field(fields: List[dict], key: str = "", title: str = "") -> Optional[dict]:
    """递归找字段对象（key 精确优先，title 兜底；三层嵌套全量下钻）。"""
    if not fields:
        return None

    def _find(field_list):
        for f in field_list or []:
            if not isinstance(f, dict):
                continue
            if key and str(f.get(FIELD_KEY, "")) == key:
                return f
            for tab in (f.get("labelPages") or []):
                hit = _find(tab.get("childFormFieldConfigVo") if isinstance(tab, dict) else None)
                if hit:
                    return hit
            hit = _find(f.get("childFormFieldConfigVo"))
            if hit:
                return hit
        return None

    by_key = _find(fields) if key else None
    if by_key:
        return by_key

    def _find_title(field_list):
        for f in field_list or []:
            if not isinstance(f, dict):
                continue
            if title and str(f.get(FIELD_TITLE, "")) == title:
                return f
            for tab in (f.get("labelPages") or []):
                hit = _find_title(tab.get("childFormFieldConfigVo") if isinstance(tab, dict) else None)
                if hit:
                    return hit
            hit = _find_title(f.get("childFormFieldConfigVo"))
            if hit:
                return hit
        return None

    return _find_title(fields)


# ── SQL 白名单（TableDefaultField 事实源，主/子表场景分流） ──

# 通用列（DEF_TABLE_COL_NAME）
_COMMON_COLUMNS = {
    "create_time", "update_time", "create_user_id", "update_user_id",
    "is_deleted", "data_version", "tenant_id", "enterprise_id", "create_dep_id",
    "id",
}
# 主表专属（MAIN_TABLE_COL_NAME，除 id 外的流程列）
_MAIN_COLUMNS = {
    "instance_id", "instance_status", "instance_status_str",
    "instance_task_key", "instance_task_name",
}
# 子表专属（SUB_TABLE_COL_NAME，除 id 外）
_SUB_COLUMNS = {"foreign_id", "row_order"}

# 系统占位符（extraObject 平台注入，MyBatis 参数绑定可用）
SYSTEM_PARAMS = {"NJMIND_LOGIN_USER_ID", "NJMIND_LOGIN_USER_DEP_ID"}

# njmd_* 宏全集（SqlTextOperator + PatternEnums 事实源）
NJMD_MACROS = (
    "njmd_dept_in", "njmd_dept_subin",
    "njmd_bpm_my_instance_4_todo", "njmd_bpm_my_instance_4_done",
    "njmd_bpm_my_instance_4_handled",
)

# BPM 宏（依赖 instance_id 列——子表场景不存在）
BPM_MACROS = {"njmd_bpm_my_instance_4_todo", "njmd_bpm_my_instance_4_done",
              "njmd_bpm_my_instance_4_handled"}

# 三库专有函数/写法黑名单（Oracle/MySQL/DM8 兼容：只用 ANSI 标准）
DB_SPECIFIC_BLACKLIST = (
    "to_date", "to_char", "sysdate", "now(", "curdate(", "curtime(",
    "ifnull(", "nvl(", "date_format(", "str_to_date(", "date_sub(",
    "date_add(", "timestampdiff(", "datediff(", "||",
)


def sql_whitelist(is_sub_table: bool) -> Set[str]:
    """SQL 列名白名单（系统列部分，按主/子表场景分流）。"""
    cols = set(_COMMON_COLUMNS)
    cols |= _SUB_COLUMNS if is_sub_table else _MAIN_COLUMNS
    return cols


# ── 被引用表字段目录（SQL 场景：列名空间=被引用表，非本表） ──

def target_field_columns(field: Optional[dict]) -> Dict[str, object]:
    """从引用记录/关联数据字段解析被引用表的字段列目录。

    事实源：designer 配置字段时 getTableFieldList 的结果存在
    childFormFieldConfigVo 上（CiteRecord 已证实；DataAssociation 同款
    存储为待验证假设——为空时调用方降级）。

    Returns:
        {"text": 目录文本, "keys": 列名集合, "empty": 是否无清单}
    """
    if not field:
        return {"text": "", "keys": set(), "empty": True}
    children = field.get("childFormFieldConfigVo") or []
    if not isinstance(children, list):
        children = []
    keys = {str(f.get(FIELD_KEY, "")) for f in children
            if isinstance(f, dict) and f.get(FIELD_KEY)}
    lines = []
    for i, f in enumerate(children, 1):
        if not isinstance(f, dict):
            continue
        parts = [f"#{i} {f.get(FIELD_KEY, '')}", str(f.get(FIELD_TITLE, "")),
                 type_name(f.get("formFieldType"))]
        opts = _option_summary(f)
        if opts:
            parts.append(f"选项:{opts}")
        lines.append(" | ".join(parts))
    return {"text": "\n".join(lines), "keys": keys, "empty": not children}
