"""增量修改指令集 - 目录构建 + 确定性合并器（零 LLM）。

【模块定位】
ModifyFormTool 增量主路径的纯逻辑层，对应 Claude Code 编辑大文件的思路：
LLM 只输出「操作指令集」（锚点定位 + 最小 patch），由本模块**确定性**应用到
配置上。LLM 不再回吐 14KB 全量配置，prompt 也不塞全量 JSON——只给字段目录。

两个入口:
- build_catalog:  配置 → 字段目录文本（~1KB，进 prompt）+ 锚点索引
- apply_ops:      配置 + 指令集 → 新配置（任一锚点失败整批不应用 = 原子性）

【为什么锚点用 key/title 而不是数组下标】
JSON Patch 的 /formFieldConfigVos/3/... 靠下标定位，LLM 数错一位就改错字段
（业界实测对 LLM 不友好）。fieldTitleKey 被 postprocess 去重保证唯一，
是内容级锚点——同 Claude Code Edit 的 old_string「内容即地址」。

【Java 类比】
apply_ops ≈ 一个事务方法：先在副本上试算全部指令（savepoint），
任一条失败就整体回滚返回失败清单；全部成功才提交新配置。
"""
import copy
import json

from domains.njmind_form.keys import (
    FIELDS, FIELD_KEY, FIELD_TITLE, FIELD_TYPE, FIELD_REQUIRED,
    OPTION_SETTINGS,
)
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# add_field 骨架补全时要从克隆源剥掉的「身份属性」——新字段不能继承旧字段的
# key / 标题 / 默认值（默认值继承会带着旧业务语义，是脏数据）
_IDENTITY_KEYS = {FIELD_KEY, FIELD_TITLE, "fieldDefaultValue"}

# 目录里展示的表单级属性（LLM 改表单结构的入口）。只放「用户可能口头提到」的，
# 不放 formConfigId/dataVersion 这类系统字段（改了也无意义还危险）
_FORM_PROPS_IN_CATALOG = (
    "formName", "formTitle", "formColumnsNumber", "formState", "serverKey",
)

# update_form 允许 patch 的顶层键 = 目录展示键 + 字段数组本身（整体替换场景）
_FORM_PATCH_ALLOWED = set(_FORM_PROPS_IN_CATALOG) | {FIELDS}


@dataclass
class CatalogResult:
    """build_catalog 的产出。

    text:   目录文本（直接进 prompt，每字段一行 + 表单属性 + 按钮清单）
    keys:   全部合法 fieldTitleKey 列表（锚点失败时喂回 LLM 的「可用锚点」）
    """
    text: str
    keys: List[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    """apply_ops 的产出。

    ok / new_config:  全部指令成功时才有值（原子性：部分成功不算成功）
    failures:         失败清单（带指令序号与原因，格式化后直接喂回 LLM 重试）
    full_rewrite:     LLM 主动请求升格全量模式（大重构指令表达不了）
    applied:          成功应用的指令摘要（给 SSE 文案 / 审计用）
    """
    ok: bool = False
    new_config: Optional[Dict[str, Any]] = None
    failures: List[str] = field(default_factory=list)
    full_rewrite: bool = False
    applied: List[str] = field(default_factory=list)
    # 指令实际触碰的字段 key（update 目标/add 新增/remove 删除）。
    # postprocess 归一化只允许影响这些字段——其余从基线原样还原（见
    # restore_untouched），保证「用户未提及的字段逐字节不变」
    touched_keys: set = field(default_factory=set)
    # 指令触碰的顶层键（update_form 的 patch 键 / 按钮所在数组）
    touched_form_keys: set = field(default_factory=set)


def build_catalog(config: Dict[str, Any],
                  type_names: Optional[Dict[int, str]] = None) -> CatalogResult:
    """把完整配置压缩成「字段目录」文本（prompt 用，~14KB → ~1KB）。

    每个字段一行：#序号 | key | 标题 | 类型 | 必填 | options 摘要。
    options 摘要是单轮增量协议的关键：LLM 不看字段完整对象也要能执行
    「追加型」指令（"在现有选项里加一个 D"）——目录直接给选项值列表。
    """
    type_names = type_names or {}
    lines: List[str] = ["## 字段目录（key 是唯一定位锚点，指令里必须原样抄写）"]
    keys: List[str] = []

    fields = config.get(FIELDS) or []
    for i, f in enumerate(fields, 1):
        key = str(f.get(FIELD_KEY, ""))
        title = str(f.get(FIELD_TITLE, ""))
        code = f.get(FIELD_TYPE)
        tname = type_names.get(code, str(code if code is not None else "?"))
        required = "必填" if f.get(FIELD_REQUIRED) else "可选"
        opts = ""
        # 选项摘要：SELECT/MULTIPLE_SELECT 的可选值（截断防爆目录）。
        # 真实结构是 optionSettings.optionFields[{optionLabel, optionValue}]
        # （实测自用户真实配置）——不是 options 字段，摘要取错键 LLM 就看不到现状
        option_fields = []
        os_ = f.get(OPTION_SETTINGS)
        if isinstance(os_, dict) and isinstance(os_.get("optionFields"), list):
            option_fields = os_["optionFields"]
        elif isinstance(f.get("options"), list):  # 兼容个别模板/测试用的简形态
            option_fields = f["options"]
        if option_fields:
            # 给完整 optionSettings（紧凑 JSON）：update_field 的 patch 是浅合并，
            # optionSettings 会被整体替换——LLM 必须能抄到兄弟键才不会丢配置
            opts = f" | 选项:{json.dumps(os_, ensure_ascii=False, separators=(',', ':'))[:400]}"
        lines.append(f"#{i} {key} | {title} | {tname} | {required}{opts}")
        if key:
            keys.append(key)

    # 表单级属性（用户口头会提的）
    props = {p: config.get(p) for p in _FORM_PROPS_IN_CATALOG if config.get(p) is not None}
    if props:
        lines.append(f"表单属性: {json.dumps(props, ensure_ascii=False)}")

    # 按钮清单（update_button 的锚点域）
    tops = [b.get("buttonName", "") for b in config.get("topButtons") or []]
    bottoms = [b.get("buttonName", "") for b in config.get("bottomButtons") or []]
    if tops or bottoms:
        lines.append(f"按钮: top={json.dumps(tops, ensure_ascii=False)} "
                     f"bottom={json.dumps(bottoms, ensure_ascii=False)}")

    return CatalogResult(text="\n".join(lines), keys=keys)


def _find_field(fields: List[Dict], key: str = "", title: str = "") -> Optional[int]:
    """双锚定位：key 精确匹配优先，找不到降级 title 精确匹配。

    LLM 抄 key 可能音译错（用户口头说中文名），fieldTitleText 更接近
    用户语言，做第二道保险。返回下标或 None。
    """
    if key:
        for i, f in enumerate(fields):
            if str(f.get(FIELD_KEY, "")) == key:
                return i
    if title:
        for i, f in enumerate(fields):
            if str(f.get(FIELD_TITLE, "")) == title:
                return i
    return None


def _norm_type_code(v):
    """类型码归一：LLM 偶发把类型码写成字符串 "4"，统一转 int。

    不可转（None/乱串）原样返回——same_type 比对不中会走模板回退，
    失败信息如实反馈给 LLM 修正指令。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return v


def _skeleton_for(fields: List[Dict], new_field: Dict,
                  template_loader: Optional[Callable[[int], Optional[Dict]]]) -> Tuple[Optional[Dict], str]:
    """add_field 的骨架补全：LLM 只给 4-5 个关键属性，其余结构从骨架继承。

    优先「同类型克隆」——同一表单里同 formFieldType 的现有字段结构与画布
    现状最一致（含该类型在本表单的惯用配置），且零网络调用；
    无同类型时才调 template_loader（上游字段模板，create 管线同源）。
    """
    # 类型码归一并写回 field：字符串码既会误报「模板不可用」烧光增量重试，
    # 也会进产物导致上游序列化失败
    code = _norm_type_code(new_field.get(FIELD_TYPE))
    new_field[FIELD_TYPE] = code
    same_type = next((f for f in fields if f.get(FIELD_TYPE) == code), None)
    if same_type is not None:
        skeleton = copy.deepcopy(same_type)
        for k in _IDENTITY_KEYS:
            skeleton.pop(k, None)
        return skeleton, f"克隆同类型字段({same_type.get('fieldTitleKey')})"

    if template_loader is not None:
        tmpl = template_loader(code)
        if tmpl:
            return copy.deepcopy(tmpl), "上游字段模板"
    return None, f"类型 {code} 无同类型字段可克隆，且模板不可用"


def apply_ops(config: Dict[str, Any], ops: List[Dict[str, Any]],
              template_loader: Optional[Callable[[int], Optional[Dict]]] = None,
              ) -> ApplyResult:
    """把指令集确定性应用到配置（原子性：任一失败整批不应用）。

    【事务语义】
    在 deepcopy 副本上按序试算全部指令；任何一条锚点失败/结构非法，
    立即放弃整批（部分应用会产生「半成品状态」，难推理难回滚——
    指令通常 1~3 条，整批重发的成本远小于半状态的一致性风险）。

    【指令集】（六种，领域定义在本 pack，引擎不感知）
    update_field / add_field / remove_field / update_form / update_button / full_rewrite
    """
    result = ApplyResult()
    if not isinstance(ops, list) or not ops:
        result.failures = ["指令集为空或格式错误（期望 {\"ops\": [...]}）"]
        return result

    # 副本上试算：成功才把副本作为新配置返回，原配置永不被就地修改
    draft = copy.deepcopy(config)
    draft_fields = draft.get(FIELDS)
    if not isinstance(draft_fields, list):
        result.failures = ["配置缺少 formFieldConfigVos 字段数组，无法增量修改"]
        return result

    for idx, op in enumerate(ops, 1):
        kind = op.get("op") if isinstance(op, dict) else None

        if kind == "full_rewrite":
            result.full_rewrite = True
            result.ok = False
            return result

        elif kind == "update_field":
            pos = _find_field(draft_fields, key=str(op.get("key", "")),
                              title=str(op.get("title", "")))
            if pos is None:
                result.failures.append(
                    f"#{idx} update_field 锚点未命中(key={op.get('key')!r} "
                    f"title={op.get('title')!r})，请从目录原样抄写 key")
                continue
            patch = op.get("patch")
            if not isinstance(patch, dict) or not patch:
                result.failures.append(f"#{idx} update_field 缺少 patch 对象")
                continue
            # 【硬约束】fieldTitleKey 是数据库标识（与已存数据关联），禁止修改——
            # prompt 有约束但 LLM 可能不听，代码层静默剥离兜底（改字段名只应改
            # fieldTitleText，key 不动）
            if FIELD_KEY in patch and patch[FIELD_KEY] != op.get("key"):
                result.applied.append(
                    f"ignored key change on {op.get('key')!r} (fieldTitleKey 禁止修改)")
                patch = {k: v for k, v in patch.items() if k != FIELD_KEY}
                if not patch:
                    continue
            target = draft_fields[pos]
            target.update(copy.deepcopy(patch))
            result.touched_keys.add(str(target.get(FIELD_KEY, "")))
            result.applied.append(
                f"update {target.get('fieldTitleKey')}: {','.join(map(str, patch.keys()))}")

        elif kind == "add_field":
            new_field = op.get("field")
            if not isinstance(new_field, dict) or not new_field.get(FIELD_KEY):
                result.failures.append(f"#{idx} add_field 缺少 field.fieldTitleKey")
                continue
            nkey = str(new_field[FIELD_KEY])
            if _find_field(draft_fields, key=nkey) is not None:
                result.failures.append(f"#{idx} add_field key={nkey!r} 与现有字段重复")
                continue
            skeleton, src = _skeleton_for(draft_fields, new_field, template_loader)
            if skeleton is None:
                result.failures.append(f"#{idx} add_field {src}")
                continue
            skeleton.update(copy.deepcopy(new_field))
            # 定位插入点：after/before 锚点；缺省追加到末尾
            anchor_key = op.get("after") or op.get("before")
            pos = len(draft_fields)
            if anchor_key:
                p = _find_field(draft_fields, key=str(anchor_key),
                                title=str(op.get("anchor_title", "")))
                if p is None:
                    result.failures.append(
                        f"#{idx} add_field 插入位置锚点未命中({anchor_key!r})")
                    continue
                pos = p + 1 if op.get("after") else p
            draft_fields.insert(pos, skeleton)
            result.touched_keys.add(nkey)
            result.applied.append(f"add {nkey}({src})")

        elif kind == "remove_field":
            pos = _find_field(draft_fields, key=str(op.get("key", "")),
                              title=str(op.get("title", "")))
            if pos is None:
                result.failures.append(
                    f"#{idx} remove_field 锚点未命中(key={op.get('key')!r})")
                continue
            removed = draft_fields.pop(pos)
            result.touched_keys.add(str(removed.get(FIELD_KEY, "")))
            result.applied.append(f"remove {removed.get('fieldTitleKey')}")

        elif kind == "update_form":
            patch = op.get("patch")
            if not isinstance(patch, dict) or not patch:
                result.failures.append(f"#{idx} update_form 缺少 patch 对象")
                continue
            illegal = set(patch.keys()) - _FORM_PATCH_ALLOWED
            if illegal:
                result.failures.append(
                    f"#{idx} update_form 不允许修改系统属性: {sorted(illegal)}")
                continue
            draft.update(copy.deepcopy(patch))
            result.touched_form_keys.update(str(k) for k in patch.keys())
            result.applied.append(f"update_form: {','.join(map(str, patch.keys()))}")

        elif kind == "update_button":
            name = str(op.get("name", ""))
            patch = op.get("patch")
            btn = None
            btn_col = ""
            for col in ("topButtons", "bottomButtons"):
                for b in draft.get(col) or []:
                    if str(b.get("buttonName", "")) == name:
                        btn = b
                        btn_col = col
                        break
                if btn is not None:
                    break
            if btn is None:
                result.failures.append(f"#{idx} update_button 按钮不存在(name={name!r})")
                continue
            if not isinstance(patch, dict) or not patch:
                result.failures.append(f"#{idx} update_button 缺少 patch 对象")
                continue
            btn.update(copy.deepcopy(patch))
            result.touched_form_keys.add(btn_col)
            result.applied.append(f"update_button {name}")

        else:
            result.failures.append(f"#{idx} 未知指令类型: {kind!r}")

    # 原子性：有失败就整批丢弃（draft 不外泄）
    if result.failures:
        result.ok = False
        result.new_config = None
        return result

    result.ok = True
    result.new_config = draft
    return result


def restore_untouched(new_cfg: Dict[str, Any], base_cfg: Dict[str, Any],
                      ar: "ApplyResult") -> Dict[str, Any]:
    """把未被指令触碰的字段/顶层键从基线原样还原。

    背景（真实反馈）：postprocess 是全配置归一化（剥 null、布尔转 0/1、剥
    前端字段）——用户没提到的 B 字段只要带 null/布尔/intro，产物里也会变，
    diff 视图出现红绿行，看起来就是"改 A 误伤 B"。
    本函数在 postprocess 之后跑：未触碰字段用基线原对象（深拷贝）替换，
    未触碰顶层键同理 —— 未提及内容逐字节不变，变更视图零噪音。
    """
    out = copy.deepcopy(new_cfg)
    base_fields = {
        str(f.get(FIELD_KEY)): f
        for f in base_cfg.get(FIELDS) or []
        if f.get(FIELD_KEY)
    }
    fields = out.get(FIELDS)
    if isinstance(fields, list):
        for i, f in enumerate(fields):
            k = str(f.get(FIELD_KEY, ""))
            # 只还原「基线里存在且未被指令触碰」的字段；add 的新字段不在此列
            if k and k not in ar.touched_keys and k in base_fields:
                fields[i] = copy.deepcopy(base_fields[k])
    for key, base_val in base_cfg.items():
        if key == FIELDS:
            continue
        if key in ar.touched_form_keys:
            continue
        out[key] = copy.deepcopy(base_val)
    return out


def format_failures(failures: List[str], catalog_keys: List[str]) -> str:
    """失败清单 → 喂回 LLM 的重试提示（附全部合法锚点，同 Claude Code
    old_string 未命中时「报错 + 重读文件」的闭环）。"""
    keys_line = ",".join(catalog_keys[:40])
    return ("以下指令执行失败（锚点必须从目录原样抄写），请修正后重新输出完整指令集：\n"
            + "\n".join(f"- {f}" for f in failures)
            + f"\n合法 key 清单：{keys_line}")
