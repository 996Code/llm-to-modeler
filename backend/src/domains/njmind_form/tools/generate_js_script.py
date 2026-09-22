"""GenerateJsScriptTool - 生成/修改字段权限 JS 规则脚本（5 个 slot）。

【模块定位】
属于 njmind_form 域。根据「画布表单配置 + 用户需求 + 现有脚本」生成字段
权限 JS 脚本，以 data 制品返回脚本 JSON。

【slot 矩阵（使用点反查定稿）】
普通字段（含子表子字段）三码：editFieldCode / showFieldAddCode / showFieldDetailCode
  签名 ({ raw, formData, userInfo }) => boolean | void
子表单字段本体两码：viewDetailCode / editDetailCode（宿主键
  childFormViewDetailConfig / childFormEditDetailConfig 内嵌）
  签名 ({ raw, formData, userInfo, row }) => boolean | void（row=当前判断的子表行）

【脚本契约（前端事实源：helpConfig.ts + permission.ts + scriptContext.ts）】
- 返回严格 === false 才阻止；true/undefined/其他值放行；异常=放行
- new Function('return ' + script)() 求值；必须是函数表达式
- 与权限开关/角色/审批节点 AND 共生——脚本只写"动态条件"层

【场景化可用上下文（运行时使用点差异，prompt 按 slot/场景注入）】
- 通用：raw=字段配置；userInfo={id,name,username,jobNumber,roles[{id,name,code}],
  ancestorsDept}
- formData 键空间：主表顶层字段 key；值形态按目录标注（SELECT=optionValue 数字码、
  USER/DEPARTMENT=ID 串多值逗号分隔、显示值走 `key_label`）
- 详情/审批页（showFieldDetailCode/editFieldCode）：formData 额外含
  flowInfo（当前审批任务）/isFirstUserTask（是否发起节点）/id/dataVersion
- 子表行内（editFieldCode 的子表子字段 / row 两码）：formData 合并当前行
  （row 键优先），row.字段key 可读本行值
- 新增页（showFieldAddCode）：仅主表顶层键，无行概念无 flowInfo

【管线】locate → generate → check（check 失败带原因重试 ≤MAX_RETRIES）。
产物：{type: "script_artifact", scriptType: "js_permission", script,
target: {fieldTitleKey, slot, hostKey}}。
"""
import logging
import re

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised

logger = logging.getLogger(__name__)

from domains.njmind_form.keys import FIELDS, FIELD_KEY, FIELD_TITLE
from domains.njmind_form.tools._script_common import (
    JS_SLOTS, ROW_SLOTS, PACK_NAME, script_params, strip_script_mark,
    build_field_catalog, normalize_slot, read_existing_script, find_field,
    slot_host_key, render_prompt,
)

MAX_RETRIES = 2

# formData 引用提取（含下标取值形态；_label 后缀是显示值豁免）
_RE_FORMDATA_REF = re.compile(
    r"formData(?:\.(\w+)|\[\s*['\"](\w+)(?:_label)?['\"]\s*\])")
_RE_ROW_REF = re.compile(r"\brow\.(\w+)")

# 结构化校验的括号对（字符串/模板字面量内部豁免）
_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _structural_check(script: str) -> list:
    """JS 结构完整性检查（无 JS 引擎依赖的折衷）：
    1) 括号配平（剥掉字符串字面量后逐字符配对）；
    2) 必须含箭头函数（契约形态）；
    3) 截断启发（以运算符/逗号结尾）。
    """
    errors = []
    if not script:
        return ["脚本为空"]

    # 剥字符串字面量（单/双引号与模板串，忽略转义后续字符的配平影响）
    scrubbed = re.sub(r"'(?:\\.|[^'\\])*'", "''", script)
    scrubbed = re.sub(r'"(?:\\.|[^"\\])*"', '""', scrubbed)
    scrubbed = re.sub(r"`(?:\\.|[^`\\])*`", "``", scrubbed)
    scrubbed = re.sub(r"//[^\n]*", "", scrubbed)      # 行注释
    scrubbed = re.sub(r"/\*.*?\*/", "", scrubbed, flags=re.S)  # 块注释

    stack = []
    in_str = None
    for ch in scrubbed:
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
        elif ch in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[ch])
        elif ch in (")", "]", "}"):
            if not stack or stack[-1] != ch:
                errors.append(f"括号不配对（多余的 {ch}）")
                break
            stack.pop()
    if stack and not errors:
        errors.append(f"括号未闭合（缺少 {''.join(reversed(stack))}）")

    if "=>" not in script:
        errors.append("未检测到箭头函数（契约要求函数表达式）")

    if re.search(r"[+\-*/,=?:]\s*$", script.strip()) or \
            re.search(r"(&&|\|\|)\s*$", script.strip()):
        errors.append("疑似输出被截断（以运算符结尾）")

    return errors


class GenerateJsScriptTool(CompositeTool):
    """根据表单配置+需求+现有脚本，生成最新字段权限 JS 脚本。"""

    name = "generate_js_script"
    description = "生成或修改字段权限 JS 规则脚本(可编辑/显隐/子表明细权限)"
    when = ("用户想编写或修改字段权限脚本,如'写段脚本让金额只有财务能编辑'"
            "'新增时隐藏成本字段''查看时薪资只有本人可见'、'[script:js]'开头")

    steps = ["locate", "generate", "check"]

    pipeline_steps = [
        {"key": "locate", "label": "定位字段与脚本位"},
        {"key": "generate", "label": "生成脚本"},
        {"key": "check", "label": "校验脚本"},
    ]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的脚本需求描述"},
            },
            "required": ["user_input"],
        }

    def validate_input(self, state: dict):
        # 画布事实源 fail-closed：脚本生成强依赖字段目录（key 反查校验）
        if not (state.get("source_artifact") or {}).get(FIELDS):
            return ("生成脚本需要表单配置(画布上下文)，"
                    "请在表单编辑页打开 AI 助手后使用")
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        state.setdefault("retry_count", 0)
        state.setdefault("check_errors", [])
        self.run_pipeline(state, ctx)

        script = state.get("script")
        if not script:
            summary = "脚本生成未完成"
            if state.get("check_errors"):
                errs = "; ".join(str(e)[:60] for e in state["check_errors"][:3])
                summary = f"脚本生成未完成（校验未通过）：{errs}"
            return ToolResult(artifact=None, artifact_type="data", summary=summary)

        slot = state.get("script_slot", "")
        host_key = slot_host_key(slot)
        artifact = {
            "type": "script_artifact",
            "scriptType": "js_permission",
            "script": script,
            "target": {
                "fieldTitleKey": state.get("field_key", ""),
                "slot": slot,
                # 弹框回写定位：普通三码写字段对象自身；row 两码写宿主配置内
                "hostKey": host_key,
            },
        }
        note = state.get("script_note") or ""
        slot_label = JS_SLOTS.get(slot, slot)
        summary = (f"已生成「{state.get('field_name') or state.get('field_key', '')}」"
                   f"的{slot_label}脚本（{len(script.splitlines())} 行）"
                   + (f"。{note}" if note else ""))
        return ToolResult(
            artifact=artifact,
            artifact_type="data",
            summary=summary,
            formatted=self.format_result(artifact),
        )

    def summarize_artifact(self, artifact: dict) -> str:
        t = artifact.get("target") or {}
        return f"已生成权限脚本: {t.get('fieldTitleKey', '')} {t.get('slot', '')}"

    def title_for(self, artifact: dict) -> str:
        t = artifact.get("target") or {}
        return f"{t.get('fieldTitleKey', '')}权限脚本"

    def format_result(self, artifact: dict) -> dict:
        script = artifact.get("script", "")
        return {
            "fieldName": artifact.get("target", {}).get("fieldTitleKey", ""),
            "scriptSlot": JS_SLOTS.get(artifact.get("target", {}).get("slot", ""),
                                       artifact.get("scriptType", "")),
            "lang": "javascript",
            "lineCount": len(script.splitlines()) if script else 0,
        }

    # ── Steps ──────────────────────────────────────────────────

    def _step_locate(self, state: dict, ctx: ToolContext) -> None:
        """定位目标字段与 slot：pack_params 显式定位优先，LLM 推断兜底。"""
        ctx.emit("stage", "locate", "正在解析表单字段与现有脚本...")

        artifact = state.get("source_artifact") or {}
        fields = artifact.get(FIELDS) or []
        catalog = build_field_catalog(fields)
        state["field_catalog"] = catalog["text"]
        state["known_keys"] = catalog["keys"]
        state["top_keys"] = catalog["top_keys"]

        # 剥路由标记后的用户原文（标记只服务于 router 分流）
        user_text = strip_script_mark(state.get("user_input", ""))

        # ① 弹框场景：pack_params 显式定位（零 LLM）
        params = script_params(state)
        field_key = str(params.get("script_field") or "").strip()
        slot = normalize_slot(str(params.get("script_slot") or ""))
        existing = str(params.get("current_script") or "")
        target = find_field(fields, key=field_key) if field_key else None
        if target is not None and not existing:
            existing = read_existing_script(target, slot or "editFieldCode")
        if target is not None:
            state["field_key"] = str(target.get(FIELD_KEY, ""))
            state["field_name"] = str(target.get(FIELD_TITLE, ""))
            state["script_slot"] = slot or "editFieldCode"
            state["existing_script"] = existing
            ctx.emit("stage", "locate",
                     f"目标：{state['field_name']} · "
                     f"{JS_SLOTS.get(state['script_slot'], '')}"
                     + ("（基于现有脚本修改）" if existing else ""))
            return

        # ② 悬浮窗场景：LLM 从话术定位字段与 slot
        system = render_prompt(ctx, "js_locate")
        user_parts = ["## 用户需求", user_text]
        if state.get("compressed_history"):
            user_parts.extend(["", "## 对话历史", state["compressed_history"]])
        if catalog["text"]:
            user_parts.extend(["", "## 字段目录", catalog["text"]])
        clarify = state.get("clarify_answers") or {}
        if clarify:
            extra = clarify.get("text") or "；".join(
                f"{k}: {v}" for k, v in clarify.items() if k != "text")
            if extra:
                user_parts.append(f"（用户补充：{extra}）")
        user_parts.append("请输出 JSON。")

        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n".join(user_parts)}],
            conv_id=ctx.conv_id, stage="js_script.locate")

        if parsed.get("needsClarification"):
            raise ClarificationRaised(parsed.get("clarificationQuestions") or
                                      ["要为哪个字段编写什么权限的脚本？"])

        field_key = str(parsed.get("fieldKey", "")).strip()
        slot = normalize_slot(str(parsed.get("scriptSlot", "")))
        target = find_field(fields, key=field_key,
                            title=str(parsed.get("fieldName", "")).strip())
        if target is None:
            raise ClarificationRaised([
                f"没能定位到字段「{parsed.get('fieldName') or field_key}」，"
                "请指明字段 key 或名称"])
        state["field_key"] = str(target.get(FIELD_KEY, ""))
        state["field_name"] = str(target.get(FIELD_TITLE, ""))
        state["script_slot"] = slot or "editFieldCode"
        state["existing_script"] = read_existing_script(
            target, state["script_slot"])
        ctx.emit("stage", "locate",
                 f"目标：{state['field_name']} · "
                 f"{JS_SLOTS.get(state['script_slot'], '')}"
                 + ("（基于现有脚本修改）" if state["existing_script"] else ""))

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        """LLM 按契约产出最新完整脚本。"""
        is_retry = bool(state.get("check_errors"))
        if is_retry:
            ctx.emit("stage", "generate",
                     f"脚本校验未过，正在修正（第 {state.get('retry_count', 0)} 次）...")
        else:
            ctx.emit("stage", "generate", "正在按平台脚本契约生成...")

        slot = state.get("script_slot", "")
        system = render_prompt(ctx, "js_generate", slot=slot)

        user_text = strip_script_mark(state.get("user_input", ""))
        user_parts = ["## 用户需求", user_text]
        if state.get("compressed_history"):
            user_parts.extend(["", "## 对话历史", state["compressed_history"]])
        if state.get("field_catalog"):
            user_parts.extend(["", "## 表单字段目录", state["field_catalog"]])
        if state.get("existing_script"):
            user_parts.extend(["", "## 现有脚本（在此基础上修改，保留仍成立的逻辑）",
                               f"```javascript\n{state['existing_script']}\n```"])
        user_parts.extend([
            "",
            f"目标字段: {state.get('field_name', '')}"
            f" (key={state.get('field_key', '')})",
            f"脚本位: {JS_SLOTS.get(slot, slot)} ({slot})",
        ])
        if is_retry:
            errs = "\n".join(f"- {e}" for e in state["check_errors"][:5])
            user_parts.extend(["", "## 上轮校验失败，请修复", errs, ""])
        user_parts.append("请输出完整脚本 JSON。")

        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n".join(user_parts)}],
            conv_id=ctx.conv_id, stage="js_script.generate")

        script = str(parsed.get("script") or "").strip()
        if script.startswith("```"):
            script = script.strip("` \n")
            if script.startswith("javascript"):
                script = script[len("javascript"):].lstrip("\n")
        state["script"] = script
        state["check_errors"] = []
        state["script_note"] = str(parsed.get("note") or "").strip()

    def _step_check(self, state: dict, ctx: ToolContext) -> None:
        """机械校验：结构完整性、签名形态、引用 key 反查。失败重跑 generate。

        关于 JS 语法：后端无 JS 引擎依赖（node/esprima 不可假设存在），Python
        compile() 无法解析 JS（=> / === 均非法）——改做结构化校验：括号配平、
        箭头函数存在、明显截断。LLM 最常见失败是截断/括号不闭合，语义级语法
        由用户编辑器的 CodeMirror 高亮兜底（返回前已肉眼可见）。
        """
        ctx.emit("stage", "check", "正在校验脚本结构与字段引用...")
        script = state.get("script") or ""
        errors = []

        # 1) 结构完整性：括号配平（忽略字符串字面量内部）+ 箭头函数存在
        errors.extend(_structural_check(script))

        # 2) 签名形态：必须是函数表达式（前端 new Function('return '+script) 求值）
        s = script.lstrip()
        if not (s.startswith("(") or s.startswith("async") or s.startswith("function")):
            errors.append("脚本必须是函数表达式，"
                          "如 ({ raw, formData, userInfo }) => {...}")

        # 3) formData/row 引用 key 反查（场景化键空间）
        known = set(state.get("known_keys") or set())
        top = set(state.get("top_keys") or set())
        slot = state.get("script_slot", "")
        is_row_slot = slot in ROW_SLOTS

        fd_refs = set()
        for m in _RE_FORMDATA_REF.finditer(script):
            fd_refs.add(m.group(1) or m.group(2) or "")
        fd_refs.discard("")
        # _label 后缀豁免（显示值键 = 业务 key + _label）
        fd_refs = {re.sub(r"_label$", "", r) for r in fd_refs}
        bad_fd = {k for k in fd_refs
                  if k not in known and k not in ("userInfo", "flowInfo",
                                                  "isFirstUserTask", "id",
                                                  "dataVersion")}
        if bad_fd:
            errors.append(f"脚本引用了表单中不存在的字段 key: "
                          f"{', '.join(sorted(bad_fd))}")

        # row 场景：row.引用必须在键空间内（row=子表行，行字段=子字段 key）
        if is_row_slot:
            row_refs = {m.group(1) for m in _RE_ROW_REF.finditer(script)}
            bad_row = {k for k in row_refs if k and k not in known}
            if bad_row:
                errors.append(f"row 引用了不存在的字段 key: "
                              f"{', '.join(sorted(bad_row))}")
        elif _RE_ROW_REF.search(script):
            # 运行时三码场景 context.row = 字段配置对象（scriptContext.ts），
            # 不是数据行——脚本用 row 取值必踩语义陷阱，直接拦截
            errors.append("该脚本位签名没有数据行 row（此场景 row 是字段配置对象），"
                          "请用 formData 取值")

        if not errors:
            ctx.emit("stage", "check", "校验通过 ✓")
            return

        state["retry_count"] = state.get("retry_count", 0) + 1
        state["check_errors"] = errors
        logger.warning(f"js script check failed "
                       f"(retry {state['retry_count']}): {errors}")
        if state["retry_count"] < MAX_RETRIES:
            self._step_generate(state, ctx)
            return self._step_check(state, ctx)
        # 达上限仍失败：丢弃产物（诚实化——未通过校验就不是"已生成"，
        # 避免用户把残缺脚本应用回设计器）
        state.pop("script", None)
        ctx.emit("stage", "check",
                 f"校验失败（已达上限）："
                 f"{'; '.join(str(e) for e in errors[:2])[:60]}")
