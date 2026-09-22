"""GenerateFilterSqlTool - 生成过滤条件 SQL（4 个配置位，上下文驱动白名单）。

【模块定位】
属于 njmind_form 域。根据「上下文 + 用户需求 + 现有 SQL」生成 WHERE 片段，
以 data 制品返回脚本 JSON。

【4 个配置位（使用点反查定稿）】
① citeRecordsConf.conditionWhereSqlText   引用记录过滤（表单画布）
② dataAssociation.conditionWhereSqlText   关联数据过滤（表单画布）
③ dataPermissionSqlText                   列表数据权限（列表设置页，pack_params 携带）
④ listTableQueryCondition.conditionWhereSqlText 列表默认过滤/组合筛选（同③）

【场景与上下文来源】
- 场景A（①②）：弹框在表单设计器字段设置 → 画布 context.artifact（被引用表
  字段清单在字段的 childFormFieldConfigVo 上）；悬浮窗 → LLM 定位候选字段。
- 场景B（③④）：弹框在列表设置页 → designer 经 pack_params 携带
  {script_conf, fields: [字段目录], current_sql, target_desc}——列名空间
  由前端带来的字段清单驱动（低码表单=fieldTitleKey 体系；DataX=数据集字段名，
  后端不感知体系差异，照做白名单）。

【SQL 书写规则（进 prompt 的硬约束 + check 的校验依据，njmind-modeler 事实源）】
1. 只写 WHERE 片段（布尔表达式），拼进后端单表/JOIN 查询的
   `WHERE t.is_deleted=0 AND ( 片段 )`；一律裸列名（禁 t. 前缀）。
2. 列名 = 上下文字段目录 ∪ 系统列白名单（主/子表场景分流）。
3. #{key} = MyBatis 预编译参数绑定，键空间 = 本表单字段 key + 系统参数
   NJMIND_LOGIN_USER_ID / NJMIND_LOGIN_USER_DEP_ID。
4. 宏：njmd_dept_in(#{k}) / njmd_dept_subin(#{k}) / bpm 三宏
   （bpm 宏依赖 instance_id 列，子表场景禁用）。
5. 三库兼容（Oracle/MySQL/DM8）：ANSI 标准写法——日期 TIMESTAMP '...' 字面量、
   当前时间 CURRENT_TIMESTAMP、函数白名单、禁 || / CONCAT / 专有函数、
   判空一律 IS NULL（Oracle ''=NULL 陷阱）。
6. 禁 MyBatis 动态标签（<if> 等）、子查询、聚合、分号。

产物：{type:"script_artifact", scriptType:"filter_sql", script,
target:{fieldTitleKey, confKey, scene}}。
"""
import logging
import re

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised

logger = logging.getLogger(__name__)

from domains.njmind_form.keys import FIELDS, FIELD_KEY, FIELD_TITLE
from domains.njmind_form.tools._script_common import (
    PACK_NAME, script_params, strip_script_mark, build_field_catalog,
    find_field, target_field_columns, sql_whitelist, SYSTEM_PARAMS,
    NJMD_MACROS, BPM_MACROS, DB_SPECIFIC_BLACKLIST, type_name,
    render_prompt,
)

# 引用记录=17 / 关联数据=10
CITE_RECORD_TYPE = 17
RELATED_DATA_TYPE = 10

# 4 配置位 → (confKey 中文, 场景标识)
CONF_SCENES = {
    "citeRecordsConf": ("引用记录过滤", "cite"),
    "dataAssociation": ("关联数据过滤", "assoc"),
    "dataPermissionSqlText": ("列表数据权限", "perm"),
    "listTableQueryCondition": ("列表默认过滤", "list"),
}

MAX_RETRIES = 2

_RE_MACRO = re.compile(r"\b(njmd_\w+)\s*\(")
_RE_PLACEHOLDER = re.compile(r"#\{\s*([^}\s]+)\s*\}")
_RE_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(select|from|where|order\s+by|group\s+by|having|insert|update|delete"
    r"|drop|alter|create|truncate|union)\b", re.IGNORECASE)

# SQL 词法白名单（校验时不算列名的词）
_SQL_WORDS = {
    "and", "or", "not", "in", "is", "null", "like", "between", "exists",
    "case", "when", "then", "else", "end", "true", "false",
    "current_timestamp", "current_date", "current_time", "timestamp",
    "coalesce", "cast", "char", "varchar", "int", "decimal", "date",
}


class GenerateFilterSqlTool(CompositeTool):
    """根据上下文+需求+现有SQL，生成过滤条件 WHERE 片段。"""

    name = "generate_filter_sql"
    description = "生成或修改过滤条件SQL(引用记录/关联数据/列表数据权限/列表过滤)"
    when = ("用户想编写或修改过滤SQL,如'引用记录只显示未完成的单子'"
            "'数据权限只看本部门''[script:sql]'开头")

    steps = ["locate", "generate", "check"]

    pipeline_steps = [
        {"key": "locate", "label": "定位字段与列空间"},
        {"key": "generate", "label": "生成SQL"},
        {"key": "check", "label": "校验SQL"},
    ]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的SQL需求描述"},
            },
            "required": ["user_input"],
        }

    def validate_input(self, state: dict):
        # 场景A 需要画布；场景B 的上下文在 pack_params——两者都没有才拦
        has_canvas = bool((state.get("source_artifact") or {}).get(FIELDS))
        has_list_ctx = bool(script_params(state).get("fields"))
        if not has_canvas and not has_list_ctx:
            return ("生成过滤SQL需要表单画布上下文或列表字段上下文，"
                    "请在表单编辑页/列表设置页使用")
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        state.setdefault("retry_count", 0)
        state.setdefault("check_errors", [])
        self.run_pipeline(state, ctx)

        sql = state.get("sql")
        if not sql:
            summary = "SQL 生成未完成"
            if state.get("check_errors"):
                errs = "; ".join(str(e)[:60] for e in state["check_errors"][:3])
                summary = f"SQL 生成未完成（校验未通过）：{errs}"
            return ToolResult(artifact=None, artifact_type="data", summary=summary)

        conf_key = state.get("conf_key", "")
        scene_label = CONF_SCENES.get(conf_key, ("过滤", ""))[0]
        artifact = {
            "type": "script_artifact",
            "scriptType": "filter_sql",
            "script": sql,
            "target": {
                "fieldTitleKey": state.get("field_key", ""),
                "confKey": conf_key,
                "scene": CONF_SCENES.get(conf_key, ("", ""))[1],
            },
        }
        note = state.get("script_note") or ""
        summary = (f"已生成「{state.get('field_name') or state.get('field_key') or scene_label}」"
                   f"的{scene_label}SQL（{len(sql)} 字符）"
                   + (f"。{note}" if note else ""))
        return ToolResult(
            artifact=artifact,
            artifact_type="data",
            summary=summary,
            formatted=self.format_result(artifact),
        )

    def summarize_artifact(self, artifact: dict) -> str:
        t = artifact.get("target") or {}
        return f"已生成过滤SQL: {t.get('fieldTitleKey', '')} {t.get('confKey', '')}"

    def title_for(self, artifact: dict) -> str:
        t = artifact.get("target") or {}
        return f"{t.get('fieldTitleKey', '') or '列表'}过滤SQL"

    def format_result(self, artifact: dict) -> dict:
        return {
            "fieldName": artifact.get("target", {}).get("fieldTitleKey", ""),
            "scriptSlot": artifact.get("scriptType", ""),
            "lang": "sql",
            "lineCount": 1,
        }

    # ── Steps ──────────────────────────────────────────────────

    def _step_locate(self, state: dict, ctx: ToolContext) -> None:
        """定位配置位与列空间：pack_params（场景B）优先，画布候选（场景A）次之，
        LLM 推断兜底（悬浮窗）。产出 state 的列目录/键空间/现有SQL。"""
        ctx.emit("stage", "locate", "正在定位目标与列空间...")

        params = script_params(state)
        fields = (state.get("source_artifact") or {}).get(FIELDS) or []
        own_catalog = build_field_catalog(fields)
        state["own_catalog"] = own_catalog["text"]
        state["own_keys"] = own_catalog["keys"]

        # ① 场景B：列表设置页弹框——pack_params 携带全部上下文（零 LLM）
        conf_key = str(params.get("script_conf") or "").strip()
        if conf_key in ("dataPermissionSqlText", "listTableQueryCondition"):
            state["conf_key"] = conf_key
            state["field_key"] = str(params.get("script_field") or "").strip()
            state["field_name"] = str(params.get("target_desc") or "列表")
            # 前端带来的字段目录（低码 fieldTitleKey 体系或 DataX 字段名体系）
            ext_fields = params.get("fields") or []
            state["target_columns"] = self._ext_columns(ext_fields)
            state["target_catalog"] = state["target_columns"]["text"]
            state["has_target_catalog"] = not state["target_columns"]["empty"]
            state["existing_sql"] = str(params.get("current_sql") or "")
            # 列表数据源为主表场景（partTableCode JOIN 的跨表列名由前端目录标注）
            state["is_sub_table"] = bool(params.get("is_sub_table"))
            ctx.emit("stage", "locate",
                     f"目标：{state['field_name']} · "
                     f"{CONF_SCENES[conf_key][0]}（上下文 {len(ext_fields)} 字段）")
            return

        # ② 场景A：表单画布——引用记录/关联数据候选定位
        cands = self._canvas_candidates(fields)
        if not cands:
            raise ClarificationRaised([
                "当前表单里没有引用记录或关联数据类型的字段，"
                "也无法从请求中获得列表字段上下文，无法生成过滤 SQL。"
            ])

        chosen = None
        # 弹框显式定位（script_field 指向候选字段）
        pf = str(params.get("script_field") or "").strip()
        if pf:
            chosen = next((c for c in cands if c["field_key"] == pf), None)
        if chosen is None and len(cands) == 1:
            chosen = cands[0]
        if chosen is None:
            # 悬浮窗：LLM 从话术选候选
            system = render_prompt(ctx, "sql_locate")
            user_text = strip_script_mark(state.get("user_input", ""))
            user_parts = ["## 用户需求", user_text]
            clarify = state.get("clarify_answers") or {}
            if clarify:
                extra = clarify.get("text") or "；".join(
                    f"{k}: {v}" for k, v in clarify.items() if k != "text")
                if extra:
                    user_parts.append(f"（用户补充：{extra}）")
            user_parts.extend([
                "", "## 候选字段",
                "\n".join(c["text"] for c in cands), "", "请输出 JSON。"])
            parsed = ctx.llm_client.chat_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": "\n".join(user_parts)}],
                conv_id=ctx.conv_id, stage="sql_script.locate")
            if parsed.get("needsClarification"):
                raise ClarificationRaised(
                    parsed.get("clarificationQuestions")
                    or ["要为哪个引用记录/关联数据字段配置过滤SQL？"])
            ck = str(parsed.get("fieldKey", "")).strip()
            chosen = next((c for c in cands if c["field_key"] == ck), None)
            if chosen is None:
                cn = str(parsed.get("fieldName", "")).strip()
                chosen = next((c for c in cands if c["field_name"] == cn), None)
            if chosen is None:
                raise ClarificationRaised([
                    "没能定位到目标字段，请指明要为哪个引用记录/关联数据字段"
                    "配置过滤 SQL（字段 key 或名称）。"])

        state["field_key"] = chosen["field_key"]
        state["field_name"] = chosen["field_name"]
        state["conf_key"] = chosen["conf_key"]
        state["existing_sql"] = chosen["existing_sql"] or str(
            params.get("current_sql") or "")
        # 列名空间 = 被引用表字段（childFormFieldConfigVo）
        cols = target_field_columns(chosen["field"])
        state["target_columns"] = cols
        state["target_catalog"] = cols["text"]
        state["has_target_catalog"] = not cols["empty"]
        # 主/子表引用分流（partFormCode 非空 = 子表 → 无 instance_* 列）
        state["is_sub_table"] = chosen["is_sub_table"]
        ctx.emit("stage", "locate",
                 f"目标：{state['field_name']} · "
                 f"{CONF_SCENES[chosen['conf_key']][0]}"
                 f"（引用表 {chosen['form_code'] or '未知'}"
                 f"{'·子表' if chosen['is_sub_table'] else ''}）"
                 + ("（基于现有SQL修改）" if state["existing_sql"] else ""))

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        """LLM 按低码链规则产出 WHERE 片段。"""
        is_retry = bool(state.get("check_errors"))
        if is_retry:
            ctx.emit("stage", "generate",
                     f"SQL校验未过，正在修正（第 {state.get('retry_count', 0)} 次）...")
        else:
            ctx.emit("stage", "generate", "正在按平台SQL规则生成...")

        system = render_prompt(ctx, "sql_generate",
                               is_sub_table=state.get("is_sub_table", False))

        user_text = strip_script_mark(state.get("user_input", ""))
        user_parts = ["## 用户需求", user_text]
        if state.get("compressed_history"):
            user_parts.extend(["", "## 对话历史", state["compressed_history"]])
        if state.get("target_catalog"):
            user_parts.extend(["", "## SQL 列名空间（列名只能用这些+系统列）",
                               state["target_catalog"]])
        else:
            user_parts.extend([
                "", "## SQL 列名空间",
                "（上下文无业务字段清单，列名仅可用系统列；用户口述的业务列名"
                "需其确认存在于目标表）"])
        if state.get("own_catalog"):
            own_keys = "\n".join(f"- {k}" for k in sorted(state.get("own_keys") or set()))
            user_parts.extend(["", "## 本表单字段（#{} 占位符可引用这些值）",
                               own_keys or "（无）"])
        if state.get("existing_sql"):
            user_parts.extend(["", "## 现有SQL（在此基础上修改，保留仍成立的条件）",
                               f"```sql\n{state['existing_sql']}\n```"])
        user_parts.extend([
            "",
            f"目标: {state.get('field_name', '')}"
            f" (key={state.get('field_key', '') or '-'})",
            f"配置位: {state.get('conf_key', '')}",
        ])
        if is_retry:
            errs = "\n".join(f"- {e}" for e in state["check_errors"][:5])
            user_parts.extend(["", "## 上轮校验失败，请修复", errs, ""])
        user_parts.append("请输出 SQL JSON。")

        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n".join(user_parts)}],
            conv_id=ctx.conv_id, stage="sql_script.generate")

        sql = str(parsed.get("sql") or "").strip().rstrip(";").strip()
        if sql.startswith("```"):
            sql = sql.strip("` \n")
            if sql.startswith("sql"):
                sql = sql[len("sql"):].lstrip("\n")
        state["sql"] = sql
        state["check_errors"] = []
        state["script_note"] = str(parsed.get("note") or "").strip()

    def _step_check(self, state: dict, ctx: ToolContext) -> None:
        """机械校验：片段语义/列名白名单/宏/占位符/三库专有写法。"""
        ctx.emit("stage", "check", "正在校验SQL列名与占位符...")
        sql = state.get("sql") or ""
        errors = []

        # 1) 片段语义：禁止完整 SQL 关键字
        m = _FORBIDDEN_KEYWORDS.search(sql)
        if m:
            errors.append(f"只允许 WHERE 片段（布尔表达式），"
                          f"出现禁止关键字: {m.group()}")

        # 2) 三库专有写法黑名单（大小写不敏感）
        lowered = sql.lower()
        for bad in DB_SPECIFIC_BLACKLIST:
            if bad in lowered:
                errors.append(f"三库兼容：禁止专有写法 {bad}"
                              f"（用 ANSI 标准等价物）")

        # 3) 宏白名单；bpm 宏在子表场景禁用
        for name in {g.lower() for g in _RE_MACRO.findall(sql)}:
            if name not in NJMD_MACROS:
                errors.append(f"未知宏函数: {name}"
                              f"（仅支持 {'/'.join(NJMD_MACROS)}）")
            elif name in BPM_MACROS and state.get("is_sub_table"):
                errors.append(f"子表场景无流程列，宏 {name} 不可用")

        # 4) 占位符键空间：本表字段 key 或系统参数
        own = set(state.get("own_keys") or set())
        for ph in _RE_PLACEHOLDER.findall(sql):
            if ph not in own and ph not in SYSTEM_PARAMS:
                errors.append(f"占位符 #{{{ph}}} 不是本表单字段或系统参数")

        # 5) 列名白名单：目标表字段（上下文目录）∪ 系统列（主/子表分流）
        allowed = set((state.get("target_columns") or {}).get("keys") or set())
        allowed |= sql_whitelist(bool(state.get("is_sub_table")))
        has_catalog = bool(state.get("has_target_catalog"))

        scrubbed = _RE_PLACEHOLDER.sub(" ", sql)
        scrubbed = re.sub(r"'[^']*'", " ", scrubbed)
        scrubbed = re.sub(
            r"(?:TIMESTAMP|DATE|TIME)\s+'[^']*'", " ", scrubbed, flags=re.IGNORECASE)
        scrubbed = _RE_MACRO.sub(" ", scrubbed)
        scrubbed = re.sub(r"\b\d+(\.\d+)?\b", " ", scrubbed)
        idents = {w for w in _RE_IDENT.findall(scrubbed)
                  if w.lower() not in _SQL_WORDS}
        unknown_cols = {w for w in idents if w not in allowed and w != "t"}
        if unknown_cols:
            if has_catalog:
                errors.append(f"SQL 使用了白名单外的列名: "
                              f"{', '.join(sorted(unknown_cols))}")
            else:
                # 无目标清单（跨表引用/上下文缺失）：不判失败，note 提示核对
                state["script_note"] = (
                    (state.get("script_note") or "")
                    + f" 请人工核对列名是否存在于目标表: "
                      f"{', '.join(sorted(unknown_cols))}").strip()

        if not errors:
            ctx.emit("stage", "check", "校验通过 ✓")
            return

        state["retry_count"] = state.get("retry_count", 0) + 1
        state["check_errors"] = errors
        logger.warning(f"sql check failed (retry {state['retry_count']}): {errors}")
        if state["retry_count"] < MAX_RETRIES:
            self._step_generate(state, ctx)
            return self._step_check(state, ctx)
        # 达上限仍失败：丢弃产物（诚实化，语义同 JS 工具）
        state.pop("sql", None)
        ctx.emit("stage", "check",
                 f"校验失败（已达上限）："
                 f"{'; '.join(str(e) for e in errors[:2])[:60]}")

    # ── 辅助 ───────────────────────────────────────────────────

    def _canvas_candidates(self, fields: list) -> list:
        """画布场景的候选字段（引用记录 17 / 关联数据 10），带配置摘要。"""
        cands = []
        for i, f in enumerate(fields, 1):
            if not isinstance(f, dict):
                continue
            code = f.get("formFieldType")
            if not isinstance(code, (int, float)) or int(code) not in (
                    CITE_RECORD_TYPE, RELATED_DATA_TYPE):
                continue
            key = str(f.get(FIELD_KEY, ""))
            conf_key = ("citeRecordsConf" if int(code) == CITE_RECORD_TYPE
                        else "dataAssociation")
            conf = f.get(conf_key) or {}
            if not isinstance(conf, dict):
                conf = {}
            form_code = (conf.get("formCode")
                         or (conf.get("querySetting") or {}).get("formKey") or "")
            part_form = conf.get("partFormCode") or ""
            existing_sql = conf.get("conditionWhereSqlText") or ""
            desc = [f"#{i} {key}", str(f.get(FIELD_TITLE, "")),
                    "引用记录" if int(code) == CITE_RECORD_TYPE else "关联数据"]
            if form_code:
                desc.append(f"引用表:{form_code}")
            if existing_sql:
                desc.append("已有SQL")
            cands.append({
                "field_key": key,
                "field_name": str(f.get(FIELD_TITLE, "")),
                "field": f,
                "conf_key": conf_key,
                "form_code": str(form_code),
                "is_sub_table": bool(part_form),
                "existing_sql": str(existing_sql),
                "text": " | ".join(desc),
            })
        return cands

    def _ext_columns(self, ext_fields) -> dict:
        """场景B：前端携带的字段目录 → 列名空间（useScriptAI 已归一为 dict）。"""
        keys = set()
        lines = []
        for i, f in enumerate(ext_fields or [], 1):
            if not isinstance(f, dict):
                continue
            key = str(f.get(FIELD_KEY) or f.get("fieldTitleKey") or "")
            if not key:
                continue
            title = str(f.get(FIELD_TITLE) or f.get("fieldTitleText") or "")
            tname = str(f.get("typeName") or type_name(f.get("formFieldType")))
            keys.add(key)
            lines.append(f"#{i} {key} | {title} | {tname}")
        return {"text": "\n".join(lines), "keys": keys, "empty": not keys}

