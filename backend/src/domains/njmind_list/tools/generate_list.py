"""GenerateListTool - 列表配置生成工具（六步管线，B5 实装）。

fetch_guide → fetch_existing(宿主 CONFIG 脱敏) → parse_intents
→ fetch_templates → generate → validate_and_finalize。

承接义务（B5 接线裁决）：
- F1 身份域 13 字段宿主注入（inject_host_identity），缺三元组 fail-closed；
- 确认门先于 validate：collect_unknown_fields 非空 → 结构化追问（带候选）；
  resume 时 apply_field_answers 确定性回填，回填后重跑 collect 判空再 validate；
- SQL 子管线在 validate 前：三个 SQL 位的 @sql: 占位（及 LLM 违规写的真
  SQL 文本）一律走 njmind_form sql_generate 子管线重生成，check_sql_fragment
  校验，失败重试 ≤2，仍失败该位回退中性不过滤形态 + warnings（不阻断整单）；
- validate 循环：fail-closed + mechanical_repair（catalog 必传）≤4 轮 +
  remaining 非空重生成 ≤MAX_RETRIES（绝不静默失败）；
- 首次 validate 前无条件 BOOL 归一（normalize_switch_ints）：真实端点
  FastJSON 对 true 宽松强转 1 会首验即过，归一不能只靠错误驱动修复；
- validate 全过后才 merge_buttons（validate 检 LLM 产物纯度，继承的宿主
  脚本本就合法，不再二次 validate）。
"""
import copy
import json
import logging
from typing import Any, Dict, Iterator, List, Tuple

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_list import service_locator
from domains.njmind_list import keys as K
from domains.njmind_list.tools._pipeline_common import (
    inject_host_identity, collect_unknown_fields, apply_field_answers,
    merge_buttons, mechanical_repair, normalize_switch_ints,
    MAX_MECHANICAL_ROUNDS,
    LIST_TABLE_QUERY_CONDITION, COMBINE_CONFIG, CONDITION, TABLE_CODE,
    BUTTON_CONFIG_LIST, BUTTON_EVENT_CONFIG, EVENT_SCRIPT,
)
# SQL 契约单一事实源（B1，跨 pack 复用，勿在本文件复制规则）
from domains.njmind_form.tools._sql_contract import (
    check_sql_fragment, xml_escape_sql,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3        # validate 失败重生成上限（绝不静默）
MAX_SQL_RETRIES = 2    # 单个 SQL 位校验失败重试上限

PACK_NAME = "njmind_list"

# keys/_pipeline_common 之外的保存形态键（本模块私有）
CONDITION_WHERE_SQL_TEXT = "conditionWhereSqlText"
DATA_PERMISSION_SQL_TEXT = "dataPermissionSqlText"
PART_TABLE_CODE = "partTableCode"
MAIN_TABLE_FIELD = "mainTableField"

# 数据源确认门的追问头（__ 前缀：apply_field_answers/意图解析均跳过 __ 键，
# 不与字段回填答案空间冲突）
SELECT_TABLE_HEADER = "__select_table"
MAX_TABLE_OPTIONS = 50

# fetch_existing 脱敏：脚本值整段剔除（脚本由宿主继承，不进 prompt）
_BUTTON_SCRIPT_KEYS = ("formatterScript", "hiddenScript", "beforeNotifyScript",
                       "notifyScript", "disableScript", "eventScript")

# SQL 位降级的中性不过滤形态（空 SQL 文本 + conditionMode=20 会 STRUCTURE_INVALID，
# 直接置空串反而阻断整单——回退到 generate.j2 认可的"不过滤"档形态）
SQL_SLOT_NEUTRAL = {"conditionMode": 10, "queryCondition": {"logic": 10}}


def host_context_from(state: dict) -> Dict[str, Any]:
    """从工具 state 提取宿主上下文（列表设计器 INIT artifact）。

    双通道（C-1 修复）：
    1. pack_params.njmind_list = {config, fields}——引擎 pack_params 通道
       （API 旁路/脚本注入形态，与 njmind_form script_params 同构）；
    2. 回退 source_artifact（designer INIT/GET_CONTEXT → engine context.artifact
       → tool_state.source_artifact，BPM 同款制品通道）——UI 聊天路径走这条：
       designer 每条消息只组 context.artifact={config, fields}，从不构造
       pack_params（final review C-1：单通道取值导致 UI 路径 100% fail-closed）。

    artifact 防御性解包：designer 形态 {config, fields}；若 artifact 整个
    就是 config 本体（含 tableShowFields 等保存形态键），按 config 解。
    """
    params = (state.get("pack_params") or {}).get(PACK_NAME) or {}
    if not isinstance(params, dict):
        params = {}
    config = params.get(K.CONFIG)
    fields = params.get(K.FIELDS)
    if not isinstance(config, dict):
        config = None
    if not isinstance(fields, list):
        fields = None
    if config is None or fields is None:
        art = state.get("source_artifact")
        if isinstance(art, dict):
            art_cfg = art.get(K.CONFIG)
            # 防御：artifact 本体即 config（含 tableConfig 等保存形态键）→ 整体当 config
            if not isinstance(art_cfg, dict) and (
                    K.TABLE_CONFIG in art or K.LIST_CODE in art):
                art_cfg = art
            if isinstance(art_cfg, dict):
                config = config or art_cfg
                art_fields = art.get(K.FIELDS)
                if isinstance(art_fields, list):
                    fields = fields if fields is not None else art_fields
    return {
        "config": config or {},
        "fields": fields or [],
    }


def _main_table_field(host_cfg: Dict[str, Any]) -> List[str]:
    """mainTableField 兼容 list/逗号串两种形态，归一为非空字段列表。"""
    raw = host_cfg.get(MAIN_TABLE_FIELD)
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def _fields_columns(fields: List[Any]) -> Dict[str, Any]:
    """宿主 FIELDS 目录 → check_sql_fragment 的 target_columns 形态。

    {"keys": set(fieldKey/fieldTitleKey), "text": 拼行, "empty": bool}；
    own_keys 与 target_columns 同源（本列表数据源字段即 #{} 占位符键空间）。
    """
    keys = set()
    lines = []
    for i, f in enumerate(fields or [], 1):
        if not isinstance(f, dict):
            continue
        key = str(f.get("fieldKey") or f.get("fieldTitleKey") or "")
        if not key:
            continue
        title = str(f.get("fieldTitle") or f.get("fieldTitleText") or "")
        keys.add(key)
        lines.append(f"#{i} {key} | {title}")
    return {"keys": keys, "text": "\n".join(lines), "empty": not keys}


def _clean_sql(sql: str) -> str:
    """LLM SQL 产物归一（同 generate_filter_sql：去围栏/分号/空白）。"""
    sql = str(sql or "").strip().rstrip(";").strip()
    if sql.startswith("```"):
        sql = sql.strip("` \n")
        if sql.startswith("sql"):
            sql = sql[len("sql"):].lstrip("\n")
    return sql.strip()


class GenerateListTool(CompositeTool):
    """根据自然语言需求生成完整列表配置（ListConfigVo 保存形态）。"""

    name = "generate_list"
    description = "根据自然语言需求生成完整列表配置(列/筛选/按钮/分页);已有列表时按新描述整体重造并回显覆盖"
    when = "任何列表生成/修改/重做需求:'做个设备台账列表'、'加一列'、'加导出按钮'、'只看今年数据'"

    steps = ["fetch_guide", "fetch_existing", "parse_intents",
             "fetch_templates", "generate", "validate"]
    pipeline_steps = [
        {"key": "fetch_guide", "label": "获取列表指南"},
        {"key": "fetch_existing", "label": "读取当前列表"},
        {"key": "parse_intents", "label": "解析列表意图"},
        {"key": "fetch_templates", "label": "匹配列表模板"},
        {"key": "generate", "label": "生成列表草稿"},
        {"key": "validate", "label": "校验与修复"},
    ]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的自然语言列表需求"}
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])
        self.run_pipeline(state, ctx)

        artifact = state.get("artifact")
        errors = state.get("validation_errors", [])
        if artifact:
            cols = len((artifact.get("config") or {}).get(K.TABLE_CONFIG, {})
                       .get(K.TABLE_SHOW_FIELDS) or [])
            warns = artifact.get("warnings") or []
            summary = (f"已生成列表「{artifact.get('hostMeta', {}).get('listName', '')}」"
                       f"({cols} 列),校验通过"
                       + (f",附 {len(warns)} 个提示" if warns else ""))
        else:
            errs = "; ".join(
                e.get("message", str(e))[:60] for e in errors[:3]) if errors else "未知原因"
            summary = f"列表配置生成未完成:{errs}"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            valid=(not errors) if artifact else None,
            validation_errors=errors or None,
            formatted=self.format_result(artifact) if artifact else {},
        )

    def summarize_artifact(self, artifact: dict) -> str:
        meta = artifact.get("hostMeta") or {}
        cfg = artifact.get("config") or {}
        cols = len((cfg.get(K.TABLE_CONFIG) or {}).get(K.TABLE_SHOW_FIELDS) or [])
        return f"列表「{meta.get('listName', '')}」,{cols} 列"

    def title_for(self, artifact: dict) -> str:
        return (artifact.get("hostMeta") or {}).get("listName", "新列表")

    def format_result(self, artifact: dict) -> dict:
        cfg = artifact.get("config") or {}
        table = cfg.get(K.TABLE_CONFIG) or {}
        return {
            "listName": (artifact.get("hostMeta") or {}).get("listName", ""),
            "columnCount": len(table.get(K.TABLE_SHOW_FIELDS) or []),
            "rowButtonCount": len(table.get(K.ROW_BUTTONS) or []),
            "warningCount": len(artifact.get("warnings") or []),
            "title": (artifact.get("hostMeta") or {}).get("listName", "新列表"),
        }

    # ── Steps ──────────────────────────────────────────────────

    @staticmethod
    def _safe_list_tables(api) -> list:
        """数据源清单（上游失败/非 dict 条目归一为空表，不阻断追问文案路径）。"""
        try:
            tables = api.list_tables() or []
        except Exception as e:
            logger.warning(f"list tables failed: {e}")
            return []
        return [t for t in tables if isinstance(t, dict)]

    def _apply_selected_table(self, state: dict, host_cfg: Dict[str, Any],
                              picked: str) -> None:
        """resume：__select_table 点选 label 反查表清单得 code，就地写入宿主 config。

        host_cfg 非空时与 state 内层 config 是同对象引用（pack_params 或
        source_artifact 之一），原地改写后续步骤（fetch_existing/validate）自然
        用上；config 整个为空时写 pack_params 通道兜底（host_context_from
        双通道合流后同样可见）。
        """
        api = service_locator.get_api()
        tables = self._safe_list_tables(api)
        hit = next((t for t in tables
                    if t.get("name") == picked or t.get("code") == picked), None)
        if not hit:
            return
        code = str(hit.get("code"))
        if host_cfg:
            host_cfg[TABLE_CODE] = code
            # 页面数据源是 serverKey+tableCode 二元组：选中表带 serverKey 且宿主
            # 未配置时一并回填（已配置且不同则保留宿主值——服务源归属由页面维护）
            picked_server_key = str(hit.get("serverKey") or "")
            if picked_server_key and not host_cfg.get(K.SERVER_KEY):
                host_cfg[K.SERVER_KEY] = picked_server_key
        else:
            state.setdefault("pack_params", {}).setdefault(PACK_NAME, {})["config"] = \
                {TABLE_CODE: code}

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "fetch_guide", "正在获取列表配置指南...")
        host_cfg = host_context_from(state)["config"]
        api = service_locator.get_api()

        # 数据源确认门（tableCode 为空时不再直接 fail-closed）：
        # resume 先消费 __select_table 点选（__ 前缀键不进 apply_field_answers），
        # 仍为空且有表清单 → 结构化追问让用户点选数据表
        clarify = state.get("clarify_answers") or {}
        picked = (clarify.get(SELECT_TABLE_HEADER)
                  if isinstance(clarify, dict) else None)
        if picked and not host_cfg.get(TABLE_CODE):
            self._apply_selected_table(state, host_cfg, str(picked))
            host_cfg = host_context_from(state)["config"]
        if not host_cfg.get(TABLE_CODE):
            tables = self._safe_list_tables(api)
            if tables:
                raise ClarificationRaised([
                    json.dumps({
                        "question": "当前列表未绑定数据源,请选择数据表",
                        "header": SELECT_TABLE_HEADER,
                        "options": [
                            {"label": t.get("name") or t.get("code"),
                             "description": "服务源: %s | %s" % (
                                 t.get("serverKey") or "-",
                                 "低码表" if t.get("tableType") == 1 else str(t.get("tableType") or "-"))}
                            for t in tables[:MAX_TABLE_OPTIONS]
                        ],
                        "multi_select": False,
                    }, ensure_ascii=False)])
            # 表清单空/上游失败 → 维持原 fail-closed（无 tableCode 调 guide 也只会
            # 得到空目录，不浪费上游调用）
            raise ClarificationRaised([
                "获取列表字段目录失败(上游服务不可用或数据源未配置字段),"
                "请先在页面设置中选择数据源,或在列表设计器内打开 AI 助手后重试"])

        guide = api.get_guide(
            host_cfg.get(TABLE_CODE) or "",
            host_cfg.get(PART_TABLE_CODE) or "",
            _main_table_field(host_cfg),
        ) or {}
        catalog = guide.get("fieldCatalog") or []
        if not catalog:
            # fail-closed：字段目录是列/筛选白名单唯一事实源（BPM 同款）
            raise ClarificationRaised([
                "获取列表字段目录失败(上游服务不可用或数据源未配置字段),"
                "请先在页面设置中选择数据源,或在列表设计器内打开 AI 助手后重试"])
        state["guide"] = guide
        state["field_catalog"] = catalog   # 原始形态（upstream validate 请求体用）
        # collect/apply/mechanical_repair 的目录形态：{value, label, fieldType}
        state["catalog"] = [
            {"value": f.get("fieldKey"), "label": f.get("fieldTitle"),
             "fieldType": f.get("fieldType")}
            for f in catalog if isinstance(f, dict) and f.get("fieldKey")]

    def _step_fetch_existing(self, state: dict, ctx: ToolContext) -> None:
        """读取宿主当前配置作为参考上下文（整体重造语义，非最小变更基线）。

        脱敏（BPM 脱 id 同款）：按钮 buttonId 换 button_host_N 语义占位
        （防 LLM 照抄随机 id），脚本值整段剔除（脚本零生成，不进 prompt）。
        """
        ctx.emit("stage", "fetch_existing", "正在读取当前列表配置...")
        host = host_context_from(state)
        state["host_config"] = host["config"] or {}
        state["own_fields"] = host["fields"] or []
        state["existing_config"] = (self._sanitize_host_config(host["config"])
                                    if host["config"] else None)

    def _step_parse_intents(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "parse_intents", "AI 正在解析列表需求...")
        user_input = state.get("user_input", "")
        clarify = state.get("clarify_answers") or {}
        if clarify:
            extra = clarify.get("text") or "；".join(
                f"{k}: {v}" for k, v in clarify.items()
                if k != "text" and not k.startswith("__"))
            if extra:
                user_input = f"{user_input}\n（用户补充回答：{extra}）"

        system_prompt = self._render_prompt(
            ctx, "parse", guide=state.get("guide") or {})
        user_msg = self._build_user_message(user_input,
                                            state.get("compressed_history", ""))
        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_msg}],
            conv_id=ctx.conv_id, stage="generate_list.parse")

        if parsed.get("needsClarification"):
            raise ClarificationRaised(parsed.get("clarificationQuestions") or
                                      ["请补充列表配置信息"])
        state["intents"] = parsed
        ctx.emit("stage", "parse_intents_done",
                 f"已解析列表意图:{parsed.get('listSummary', '')}")

    def _step_fetch_templates(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "fetch_templates", "正在匹配列表模板...")
        api = service_locator.get_api()
        state["column_templates"] = api.get_template("column-templates.json") or []
        state["button_templates"] = api.get_template("button-templates.json") or []
        ctx.emit("stage", "fetch_templates_done", "已加载列表模板")

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        is_retry = bool(state.get("validation_errors"))
        if is_retry:
            ctx.emit("stage", "generate_retry",
                     f"校验失败,正在修复重新生成(第 {state.get('retry_count', 0)} 次重试)...")
        else:
            ctx.emit("stage", "generate", "AI 正在组装列表配置...")

        system_prompt = self._render_prompt(
            ctx, "generate",
            guide=state.get("guide") or {},
            column_templates=state.get("column_templates") or [],
            button_templates=state.get("button_templates") or [])
        user_parts = []
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])
        if state.get("existing_config"):
            user_parts.extend([
                "## 当前列表配置(仅供理解业务语义;输出必须是全新配置,"
                "禁止照抄 button_host_N 等参考占位 buttonId)",
                "```json\n"
                + json.dumps(state["existing_config"], ensure_ascii=False)
                + "\n```",
                "",
            ])
        if is_retry and state.get("raw_draft"):
            error_lines = [e.get("message", str(e)) if isinstance(e, dict) else str(e)
                           for e in state.get("validation_errors", [])[:8]]
            user_parts.extend([
                "## 校验失败,请修复", "\n".join(error_lines), "",
                "## 当前草稿",
                f"```json\n{json.dumps(state['raw_draft'], ensure_ascii=False)}\n```",
                "修复以上问题后输出完整列表配置 JSON(紧凑,无围栏)。",
            ])
        else:
            user_parts.extend([
                "## 列表意图",
                f"```json\n"
                + json.dumps(state.get("intents") or {},
                             ensure_ascii=False, indent=2) + "\n```",
                "",
                "请根据意图和模板,组装完整的列表配置 JSON"
                "(ListConfigVo 保存形态,只含生成域,身份域 13 字段不写)。",
            ])
        draft = ctx.llm_client.chat_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": "\n".join(user_parts)}],
            conv_id=ctx.conv_id, stage="generate_list.generate")
        state["raw_draft"] = draft
        state["validation_errors"] = []

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "validate", "正在校验列表配置...")
        host_cfg = state.get("host_config") or {}

        # ① F1 身份注入：LLM 自造身份一律剥离（err 即 fail-closed）
        draft, err = inject_host_identity(state.get("raw_draft") or {}, host_cfg)
        if err:
            state["validation_errors"] = [{"message": err}]
            ctx.emit("stage", "validate_fail", err)
            return

        # ② 确认门（先于 validate）：resume 先确定性回填点选，再重跑 collect
        # 判空；仍有目录外字段 → 结构化追问（带候选，BPM 同款 json.dumps）
        catalog = state.get("catalog") or []
        clarify = state.get("clarify_answers") or {}
        if isinstance(clarify, dict):
            field_answers = {k: v for k, v in clarify.items()
                             if k not in ("text",) and not k.startswith("__")}
            if field_answers:
                apply_field_answers(draft, field_answers, catalog)
        unknown = collect_unknown_fields(draft, catalog)
        if unknown:
            raise ClarificationRaised([
                json.dumps({
                    "question": f"配置里引用的字段「{u['missing']}」"
                                f"不在数据源目录中,实际想用哪个字段?",
                    # header = 回传答案键：前端点选按 {[q.header]: opt.label}
                    # 键控回传，apply_field_answers 以完整缺失键精确匹配——
                    # 必须用全量 missing（BPM 同款），截断会让回填失配重复
                    # 追问；chip 显示截断交给前端样式处理
                    "header": u["missing"],
                    "options": [
                        {"label": c["label"], "description": c["description"]}
                        for c in u["candidates"]
                    ],
                }, ensure_ascii=False)
                for u in unknown
            ])

        # ③ SQL 子管线（validate 前）：@sql: 占位及 LLM 违规真 SQL 一律重生成
        warnings: List[str] = []
        self._run_sql_subpipeline(draft, state, ctx, warnings)

        # ④ validate 循环：fail-closed + 机械修复（catalog 必传）≤4 轮
        # （validate 收深拷贝快照：merge 在 validate 后就地改写同一 nested
        # 对象，不快照会让"校验时纯度"被事后篡改模糊掉）
        api = service_locator.get_api()
        field_catalog = state.get("field_catalog") or []
        # 首验前无条件 BOOL 归一（I-1）：真实 A2 端点 FastJSON 对 JSON true
        # 宽松强转 Integer 1——首验即过、永不报 BOOL_FIELD_NOT_INT，等
        # mechanical_repair（错误驱动）才归一会让 boolean 穿透进 artifact，
        # 前端应用后走 Jackson 保存流整包拒绝
        normalize_switch_ints(draft)
        result = api.validate(copy.deepcopy(draft), field_catalog)
        errors = result.get("errors") or []
        passed = result.get("pass") is True and not errors
        for _ in range(MAX_MECHANICAL_ROUNDS):
            if passed:
                break
            draft, errors = mechanical_repair(draft, errors, catalog=catalog)
            result = api.validate(copy.deepcopy(draft), field_catalog)
            errors = result.get("errors") or []
            passed = result.get("pass") is True and not errors

        if passed:
            # ⑤ validate 全过后才 merge_buttons：validate 检 LLM 产物纯度,
            # 继承的宿主脚本本就合法;merge 只注入 buttonId/脚本/permissionCode
            # 不改结构,不再二次 validate
            merge_buttons(draft, host_cfg)
            for w in result.get("warnings") or []:
                warnings.append(w.get("message", str(w))
                                if isinstance(w, dict) else str(w))
            state["validation_errors"] = []
            state["artifact"] = {
                "type": "list-config",
                "config": draft,
                "warnings": warnings,
                "hostMeta": {
                    "listCode": draft.get(K.LIST_CODE) or "",
                    "listName": draft.get(K.LIST_NAME) or "",
                },
            }
            ctx.emit("stage", "validate_pass",
                     f"列表校验通过 ✓" + (f"({len(warnings)} 个提示)"
                                          if warnings else ""))
            return

        # ⑥ remaining 非空 → 重生成 ≤MAX_RETRIES（错误回填附当前草稿，BPM 同款）
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = errors
        state["raw_draft"] = draft
        msgs = [e.get("message", str(e))[:60] if isinstance(e, dict) else str(e)[:60]
                for e in errors[:3]]
        if state["retry_count"] <= MAX_RETRIES:
            ctx.emit("stage", "validate_retry",
                     f"校验失败:{'; '.join(msgs)},"
                     f"正在重新生成(第 {state['retry_count']} 次)...")
            self._step_generate(state, ctx)
            return self._step_validate(state, ctx)
        ctx.emit("stage", "validate_fail",
                 f"校验失败(已达最大重试):{'; '.join(msgs)}")

    # ── SQL 子管线 ─────────────────────────────────────────────

    def _run_sql_subpipeline(self, draft: Dict[str, Any], state: dict,
                             ctx: ToolContext, warnings: List[str]) -> None:
        host_cfg = state.get("host_config") or {}
        is_sub = bool(host_cfg.get(PART_TABLE_CODE))
        cols = _fields_columns(state.get("own_fields") or [])
        slots = list(self._iter_sql_slots(draft))
        if not slots:
            return
        ctx.emit("stage", "generate_sql",
                 f"正在生成 {len(slots)} 处过滤SQL(子管线)...")
        user_input = state.get("user_input", "")
        for container, key, label in slots:
            self._gen_sql_for_slot(container, key, label, user_input,
                                   is_sub, cols, ctx, warnings)

    @staticmethod
    def _iter_sql_slots(draft: Dict[str, Any]) -> Iterator[Tuple[dict, str, str]]:
        """产出三个 SQL 位的 (容器, 键, 中文标签)；值非空即占位。

        值以 @sql: 开头是意图占位（desc=其后文本）；LLM 违规写真 SQL 文本
        （非空且无前缀）也一律当占位重生成替换。
        """
        table = (draft or {}).get(K.TABLE_CONFIG) or {}
        lqc = table.get(LIST_TABLE_QUERY_CONDITION)
        if isinstance(lqc, dict) and lqc.get(CONDITION_WHERE_SQL_TEXT):
            yield lqc, CONDITION_WHERE_SQL_TEXT, "默认过滤"
        for combine in (draft or {}).get(COMBINE_CONFIG) or []:
            for c in (combine or {}).get(CONDITION) or []:
                lqc2 = (c or {}).get(LIST_TABLE_QUERY_CONDITION)
                if isinstance(lqc2, dict) and lqc2.get(CONDITION_WHERE_SQL_TEXT):
                    yield (lqc2, CONDITION_WHERE_SQL_TEXT,
                           f"组合筛选「{combine.get('name', '')}·{c.get('name', '')}」")
        if table.get(DATA_PERMISSION_SQL_TEXT):
            yield table, DATA_PERMISSION_SQL_TEXT, "数据权限"

    def _gen_sql_for_slot(self, container: dict, key: str, label: str,
                          user_input: str, is_sub: bool, cols: dict,
                          ctx: ToolContext, warnings: List[str]) -> None:
        raw = str(container.get(key) or "")
        desc = raw[5:].strip() if raw.startswith("@sql:") else raw.strip()
        own_keys = cols.get("keys") or set()
        errors: List[str] = []
        attempt = 0
        while True:
            sql, note = self._chat_sql(ctx, desc, label, user_input,
                                       is_sub, cols, own_keys, errors)
            sql = _clean_sql(sql)
            notes: List[str] = []
            errs = check_sql_fragment(
                sql,
                target_columns=cols,
                own_keys=own_keys,
                is_sub_table=is_sub,
                user_text=f"{user_input} {desc}".strip(),
                notes=notes,
            )
            for n in notes:
                warnings.append(f"{label}SQL: {n}")
            if not errs:
                if note:
                    warnings.append(f"{label}SQL备注: {note}")
                container[key] = xml_escape_sql(sql)
                return
            attempt += 1
            errors = errs
            logger.warning(f"list sql check failed ({label} retry {attempt}): {errs}")
            if attempt > MAX_SQL_RETRIES:
                # 置空不阻断：LQC 位回退中性不过滤形态（空 SQL 文本 + SQL 模式
                # 会 STRUCTURE_INVALID），数据权限位是可选纯文本直接置空串
                if key == DATA_PERMISSION_SQL_TEXT:
                    container[key] = ""
                else:
                    container.clear()
                    container.update(copy.deepcopy(SQL_SLOT_NEUTRAL))
                warnings.append(
                    f"{label}的过滤SQL未通过校验,已置空(可稍后在列表设置页"
                    f"手工配置): {errs[0][:80]}")
                return

    def _chat_sql(self, ctx: ToolContext, desc: str, label: str,
                  user_input: str, is_sub: bool, cols: dict, own_keys: set,
                  errors: List[str]) -> Tuple[str, str]:
        """SQL 子管线 LLM 调用：system 用 njmind_form sql_generate（跨 pack）。"""
        system = ""
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            system = ctx.prompt_loader.render(
                "njmind_form", "sql_generate", is_sub_table=is_sub)
        parts = ["## 过滤语义", f"{desc}（来自{label}）", ""]
        if cols.get("text"):
            parts.extend(["## SQL 列名空间（列名只能用这些+系统列）",
                          cols["text"], ""])
        else:
            parts.extend(["## SQL 列名空间",
                          "（上下文无业务字段清单，列名仅可用系统列）", ""])
        if own_keys:
            parts.extend([
                "## 本表单字段（#{} 占位符可引用这些值）",
                "\n".join(f"- {k}" for k in sorted(own_keys)), ""])
        if user_input:
            parts.extend(["## 用户原话", user_input, ""])
        if errors:
            parts.extend(["## 上轮校验失败，请修复",
                          "\n".join(f"- {e}" for e in errors[:5]), ""])
        parts.append("请输出 SQL JSON（{sql, note}）。")
        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n".join(parts)}],
            conv_id=ctx.conv_id, stage="generate_list.sql")
        return str(parsed.get("sql") or ""), str(parsed.get("note") or "").strip()

    # ── 辅助 ──────────────────────────────────────────────────

    @staticmethod
    def _sanitize_host_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """宿主配置脱敏副本：buttonId → button_host_N、脚本整段剔除。"""
        ref = copy.deepcopy(config or {})
        seq = 0

        def _do_buttons(buttons) -> None:
            nonlocal seq
            for b in buttons or []:
                if not isinstance(b, dict):
                    continue
                if b.get("buttonId"):
                    seq += 1
                    b["buttonId"] = f"button_host_{seq}"
                for k in _BUTTON_SCRIPT_KEYS:
                    b.pop(k, None)
                ev = b.get(BUTTON_EVENT_CONFIG)
                if isinstance(ev, dict):
                    ev.pop(EVENT_SCRIPT, None)
                _do_buttons(b.get("dropdownItems"))

        _do_buttons((ref.get(K.TABLE_CONFIG) or {}).get(K.ROW_BUTTONS))
        _do_buttons((ref.get(K.BUTTON_GROUP_CONFIG) or {}).get(BUTTON_CONFIG_LIST))
        return ref

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render(PACK_NAME, name, **vars)
        logger.warning(f"No prompt_loader for {name}")
        return ""

    @staticmethod
    def _build_user_message(user_input: str, compressed_history: str) -> str:
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 当前列表需求", user_input, "", "请分析并输出 JSON。"])
        return "\n".join(parts)
