"""生成配置的后处理（机械修正，不烧 LLM 重试）。

【为什么需要】
LLM 按指令输出完整配置 JSON 时，有三类高频、机械、确定性可修的偏差：
  1. 类型漂移：上游 Java schema 的开关字段是 Integer 0/1，LLM 常写 true/false
     （实测报错：Cannot deserialize Integer from Boolean）——重试 3 轮也不自愈；
  2. 字段重复：同一条指令生成的字段以相同 fieldTitleKey 出现多次（实测手机号×3）；
  3. formTitle 缺失：宿主画布配置（designer IFormConfig）本身不含 formTitle，
     上游校验 F4 要求非空。
这三类交给校验重试循环既慢（每轮一次完整 LLM 生成）又不可靠，
在「生成之后、校验之前」机械修正，一次到位。

【守门】
本模块只做类型层面/结构层面的确定性修正，不猜测业务语义。
"""
import logging
from typing import Optional, Any, Dict

logger = logging.getLogger(__name__)

# 布尔 → 上游要求的 0/1。全局转换：上游 Java schema 的开关字段全部是 Integer，
# 实测布尔值必然反序列化失败；反之没有字段需要真布尔。
def _bool_to_int(obj: Any) -> Any:
    if isinstance(obj, bool):
        return 1 if obj else 0
    if isinstance(obj, dict):
        return {k: _bool_to_int(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bool_to_int(v) for v in obj]
    return obj


# ── 值类型矫正：LLM 常把整型属性写成字符串数字，Jackson 反序列化直接 500 ──
# 白名单保守匹配：只矫正确定是整型的属性键，避免误伤真字符串字段
# （fieldTitleText 之类的键永远不会出现在这里）。
_INT_KEYS = frozenset({
    "formFieldType", "isRequiredField", "isDefaultField", "isEditField",
    "isShowFieldAdd", "isShowFieldDetail", "isShowFieldTitle",
    "isLimitOptionsAvailable", "isRecycleBin", "fieldWidth",
    "formColumnsNumber", "selectMode", "displayStyle", "multipleTag",
    "fieldFormat", "fieldDefaultValueType", "optionalRangeType",
    "selectionMethod", "formState", "dataVersion", "configEnable",
    "hasDataDifLog", "fieldButtonDrafts", "fieldButtonStage",
    "fieldButtonSubmit", "backgroundStyle", "detailFieldTitlePc",
    "fieldTitlePc", "fieldTitlePhone", "fieldTitleStyleAlignPhone",
    "fieldTitleStyleAlignment", "fieldTitleStyleWidth",
    "fieldTitleStyleWidthPhone", "optionValue", "fieldOrder",
})


def _coerce_scalar(key: str, v: Any) -> Any:
    """白名单整型键的字符串值 → int："12"→12、"true"→1、"false"→0。

    非纯数字（"12.5"、空串等）保持原样——强行转换会掩盖真实错误，
    交给上游校验报出具体字段。
    """
    if not isinstance(v, str) or key not in _INT_KEYS:
        return v
    s = v.strip()
    if s in ("true", "True"):
        return 1
    if s in ("false", "False"):
        return 0
    try:
        return int(s)
    except ValueError:
        return v


def _coerce_int_strings(obj: Any) -> Any:
    """递归对 dict 的键做白名单整型矫正（list 原样下钻）。"""
    if isinstance(obj, dict):
        return {k: _coerce_int_strings(_coerce_scalar(k, v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_int_strings(v) for v in obj]
    return obj


# 数组按 fieldTitleKey 去重（保留首个）。递归处理子表单/标签页内的字段数组。
def _dedupe_fields(obj: Any) -> Any:
    if isinstance(obj, list):
        items = [_dedupe_fields(v) for v in obj]
        # 仅当列表元素是带 fieldTitleKey 的字段对象时去重（按钮等列表不受影响）
        if items and all(isinstance(i, dict) and FIELD_KEY in i for i in items):
            seen, out = set(), []
            for i in items:
                key = i.get(FIELD_KEY)
                if key in seen:
                    logger.warning(f"postprocess: 去重重复字段 {key}")
                    continue
                seen.add(key)
                out.append(i)
            return out
        return items
    if isinstance(obj, dict):
        return {k: _dedupe_fields(v) for k, v in obj.items()}
    return obj


# 前端标记字段：宿主画布配置携带、上游 Java schema 不识别（Jackson 严格模式
# 报 Unrecognized field）。递归剥离（表单级 + 字段级 + 嵌套）。
# icon=base64 图标（占配置体积大半）、widgetType/intro=画布专用展示字段；
# 渲染回画布时由 buildWidgets 默认值合并补回，剥离无副作用（防御宿主未剥的场景）
_FRONTEND_ONLY_KEYS = {"isSaved", "icon", "widgetType", "intro"}


def _strip_frontend_fields(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_frontend_fields(v)
            for k, v in obj.items()
            if k not in _FRONTEND_ONLY_KEYS and v is not None
        }
    if isinstance(obj, list):
        return [_strip_frontend_fields(v) for v in obj]
    return obj


def postprocess_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """生成配置的统一后处理：剥前端标记与 null、布尔→0/1、数字串矫正、
    字段去重、formTitle 兜底。"""
    if not isinstance(config, dict):
        return config
    cfg = _strip_frontend_fields(config)
    cfg = _bool_to_int(cfg)
    cfg = _coerce_int_strings(cfg)
    cfg = _dedupe_fields(cfg)
    # formTitle 兜底：宿主画布配置无此字段，上游 F4 要求非空；
    # 格式约定为 $fieldKey$（取标题字段或第一个字段）
    if not cfg.get("formTitle"):
        title_key = cfg.get("titleFieldKey")
        if not title_key:
            fields = cfg.get(FIELDS) or []
            title_key = fields[0].get(FIELD_KEY, "name") if fields else "name"
        cfg["formTitle"] = f"${title_key}$"
    return cfg


# ── Schema 投影（校验专用）─────────────────────────────────────────────
# 背景：设计器画布配置携带 validate VO 不认识的字段（mainTable/queryResultCols/…），
# 直接校验必失败且重试无法自愈（字段来自宿主存量数据，不是 AI 生成的）。
# 方案：给校验一份「按上游 JSON Schema 投影」的副本——只删 schema 外的键；
# 应用到画布的 artifact 保持原样（设计器需要的字段零损失）。

def _collect_schema_keys(node: Any, out: set) -> None:
    """递归收集 schema 里所有 properties 的键名（跨 $defs/嵌套定义的全集）。"""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            out.update(props.keys())
        for v in node.values():
            _collect_schema_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_schema_keys(v, out)


def schema_projection(obj: Any, allowed: set) -> Any:
    """按允许键集合递归剪枝（深拷贝语义：返回新结构，不改原对象）。"""
    if isinstance(obj, dict):
        return {
            k: schema_projection(v, allowed)
            for k, v in obj.items()
            if k in allowed
        }
    if isinstance(obj, list):
        return [schema_projection(v, allowed) for v in obj]
    return obj


# ── 校验错误的机械处理 ────────────────────────────────────────────────
import re as _re
from domains.njmind_form.keys import FIELDS, FIELD_KEY

_UNRECOGNIZED_RE = _re.compile(r'Unrecognized field "(\w+)"')


# ── 必填项缺失 / 值域不合法的机械修复（不烧 LLM）──────────────────────
# 背景（实测）：上游 user_field/department_field 模板自身缺 selectMode，
# LLM「按模板填充」后首轮校验必挂（selectMode 为必填项），烧一轮 LLM 重试
# ~35s。这类结构错误是确定性的：值从「表内同类型字段 → 上游模板 →
# config.yaml 兜底」三级抄写即可，与剥 Unrecognized field 同一循环处理。

# D: baoxiaoren.selectMode 为必填项(字段类型=USER)
_MISSING_REQ_RE = _re.compile(
    r"([A-Za-z0-9_]+)\.([A-Za-z0-9_]+) 为必填项\(字段类型=([A-Z_]+)\)")
# D: jichuxinxi.displayStyle=0 不合法,类型SEGMENT 允许值=[1]
_INVALID_VAL_RE = _re.compile(
    r"([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)=[^,，]+[,，]类型([A-Z_]+) 允许值=\[([^\]]*)\]")


def parse_fixable_field_errors(errors: list) -> list:
    """提取机械可修的字段级错误 [(fieldKey, prop, typeName, allowed)]。

    allowed：值域错误里解析出的允许值列表（缺失类错误为 None）。
    """
    out = []
    for e in errors or []:
        msg = e.get("message", "") if isinstance(e, dict) else str(e)
        for key, prop, tname, allowed in _INVALID_VAL_RE.findall(msg):
            out.append((key, prop, tname,
                        [v.strip() for v in allowed.split(",") if v.strip()]))
        for key, prop, tname in _MISSING_REQ_RE.findall(msg):
            out.append((key, prop, tname, None))
    return out


def fill_missing_required(config: Dict[str, Any], fixables: list,
                          template_getter=None,
                          prop_defaults: Optional[Dict[int, Dict[str, Any]]] = None) -> bool:
    """按三级来源补全/修正字段属性，返回是否有修改。

    值来源优先级（前一级没有才看下一级）：
      1. 表内同类型字段——最符合本表语义（如 selectMode 全表一致）；
      2. 上游字段模板（template_getter(code) → dict | None）；
      3. config.yaml 的 required_prop_defaults（数据兜底，不在代码里硬编码）；
      4. 值域错误解析出的允许值列表第一个。
    四级都没有 → 不动（留给 LLM 重试路径），绝不猜值。
    """
    if not fixables:
        return False
    fields = config.get(FIELDS) or []
    by_key = {str(f.get(FIELD_KEY)): f for f in fields if f.get(FIELD_KEY)}
    prop_defaults = prop_defaults or {}
    changed = False

    def _source_value(type_code, prop, allowed, exclude_key):
        def _ok(v):
            # 值域错误：抄来的值必须落在允许域内，否则视为无效来源
            if allowed is None:
                return True
            return str(v) in [str(a) for a in allowed]

        # ① 表内同类型字段（跳过出错字段自身——尤其值域错误时自身就是错值源头）
        for f in fields:
            if (f.get("formFieldType") == type_code and prop in f
                    and str(f.get(FIELD_KEY)) != exclude_key
                    and _ok(f[prop])):
                return f[prop]
        # ② 上游模板
        if template_getter is not None:
            try:
                tmpl = template_getter(type_code)
            except Exception:
                tmpl = None
            if isinstance(tmpl, dict) and prop in tmpl and _ok(tmpl[prop]):
                return tmpl[prop]
        # ③ config.yaml 兜底
        if prop in (prop_defaults.get(type_code) or {}) and \
                _ok(prop_defaults[type_code][prop]):
            return prop_defaults[type_code][prop]
        # ④ 允许值第一个（值域错误兜底）
        if allowed:
            try:
                v = allowed[0]
                return int(v) if _re.fullmatch(r"-?\d+", v) else v
            except Exception:
                return None
        return None

    for key, prop, tname, allowed in fixables:
        f = by_key.get(key)
        if f is None:
            continue
        type_code = f.get("formFieldType")
        if type_code is None:
            continue
        val = _source_value(type_code, prop, allowed, exclude_key=key)
        if val is None:
            continue
        if f.get(prop) != val:
            f[prop] = val
            changed = True
    return changed


def parse_unrecognized_fields(errors: list) -> set:
    """从校验错误里提取所有 Unrecognized field 名（机械可修，无需 LLM）。"""
    names = set()
    for e in errors or []:
        msg = e.get("message", "") if isinstance(e, dict) else str(e)
        names.update(_UNRECOGNIZED_RE.findall(msg))
    return names


def strip_keys(obj: Any, keys: set) -> Any:
    """递归剔除指定键名（返回新结构）。"""
    if isinstance(obj, dict):
        return {k: strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [strip_keys(v, keys) for v in obj]
    return obj


def normalize_error(e) -> str:
    """错误归一化为可比对字符串（差分校验用）。"""
    return e.get("message", "") if isinstance(e, dict) else str(e)


# ── 校验错误归一化（修复 prompt 专用）────────────────────────────────────
# 背景：上游 validate 请求失败（500/超时/连接异常）时 Fail-Closed 会把
# httpx 异常文本塞进 errors——这类文本对 LLM 没有任何修复指向，直接喂进
# 修复 prompt 只会烧光重试次数（真实痛点：值类型不符导致上游 Jackson
# 反序列化 500，LLM 拿着 "Server error '500' for url ..." 无从下手）。
_TRANSPORT_ERROR_MARKERS = (
    "validation request failed", "internal server error",
    "connect error", "connection refused", "timeout", "timed out",
)


def normalize_validation_errors(errors: list, max_items: int = 6) -> list:
    """校验错误 → 修复 prompt 用的编号指令列表。

    - 传输/序列化失败（异常文本特征）：聚合替换为一条可执行的类型修复清单
      ——值类型不符是这类失败的主因，给出明确的检查项比原始异常文本有效；
    - 业务校验错误：按原意编号保留（截断单条长度）；
    - 超出 max_items 的部分聚合为一条计数提示，避免 prompt 膨胀。
    """
    if not errors:
        return []
    business, has_transport = [], False
    for e in errors:
        msg = normalize_error(e).strip()
        if any(m in msg for m in _TRANSPORT_ERROR_MARKERS):
            has_transport = True
            continue
        business.append(msg[:200])
    out = []
    if has_transport:
        out.append(
            "【类型错误】上游无法解析配置 JSON（多为字段值类型不符）。逐项检查并修复："
            "① 布尔/枚举属性输出整数 0 或 1，禁止 true/false；"
            "② 数字类属性（fieldWidth、formColumnsNumber、selectMode、displayStyle、"
            "optionValue、isRequiredField 等）必须是整数，禁止加引号写成字符串；"
            "③ 必填项禁止输出 null，给合理默认值或省略该属性；"
            "④ 不要输出上游不认识的属性"
        )
    shown = business[:max_items]
    out.extend(f"{i}. {m}" for i, m in enumerate(shown, start=1))
    rest = len(business) - len(shown)
    if rest > 0:
        out.append(f"（另有 {rest} 条同类校验错误，一并修复）")
    return out
