"""ModifyFormTool - 修改已有表单配置的复合工具(3 步管线)。

【模块定位】
属于 njmind_form 域。当用户说"加一个手机号字段"、"删除 xxx"、"把 xxx 改成必填"
时,引擎选中本工具。与 GetFormTool(单步查询)不同,本工具是**复合工具
(CompositeTool)**,内部有 3 步串行管线,并支持校验失败后自动重试。

【3 步管线】
  fetch_guide → modify → validate
    1. fetch_guide: 从上游拉"配置指南"(字段类型映射等元数据)
    2. modify:      两相式 —— 增量主路径(plan_ops→apply_ops,默认) +
                    全量重生成兜底(大重构/增量失败时升格,见 _step_modify)
    3. validate:    差分校验,失败则带错误信息重跑 modify(最多 MAX_RETRIES 次)

【增量主路径(Claude Code Edit 同源)】
  LLM 只输出操作指令集(update/add/remove_field 等,几百字节),
  _incremental_ops.apply_ops 确定性合并 —— prompt 从全量 16-20KB 降到 ~3KB,
  modify 步 ~33s → ~3-8s。锚点失败带清单重试,超限自动升格全量,永不劣于旧路径。

【Java 类比】
  - CompositeTool ≈ Spring 里带 ``@Transactional`` 的编排型 Service,
    内部串联多个子步骤;Tool 则是单步 Service
  - 3 步管线 + 重试 ≈ Activiti / Camunda 的 BPMN 流程,
    或 Spring Batch 的 Step 序列
  - run_pipeline ≈ 模板方法模式 (Template Method):
    基类定义执行框架,子类实现各 ``_step_xxx`` 钩子

【关键概念:source_artifact vs artifact】
  - source_artifact: 用户传入的"原始配置"(修改前的版本)
  - artifact:        工具产出的"当前配置"(修改后的版本,可能经过多次重试)
  两者分开,便于审计"改了什么"以及回滚。
"""
import logging
from typing import Any, Dict, Optional

# CompositeTool:复合工具基类,提供 run_pipeline 模板方法
# ClarificationRaised:需要追问时抛的异常(让 graph interrupt)
from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_form.tools._postprocess import (
    postprocess_config, _collect_schema_keys, schema_projection,
    parse_unrecognized_fields, strip_keys, normalize_error,
    parse_fixable_field_errors, fill_missing_required,
)
from domains.njmind_form.tools._incremental_ops import (
    build_catalog, apply_ops, format_failures, restore_untouched,
)
# 模板 stem 推导表与 create 管线同源（add_field 骨架 fallback 拉上游模板用）
from domains.njmind_form.tools._config_loader import load_prop_defaults, field_template_stem

logger = logging.getLogger(__name__)

# schema 允许键集合（懒加载缓存）：校验投影用。上游 schema 是 validate VO 的
# 权威字段清单——设计器画布配置携带的 VO 外字段（mainTable/queryResultCols 等）
# 只在投影副本里剔除，artifact 本体保持原样（画布渲染/保存需要它们）。
_ALLOWED_KEYS: set | None = None


def _get_allowed_keys(ctx) -> set:
    global _ALLOWED_KEYS
    if _ALLOWED_KEYS is not None:
        return _ALLOWED_KEYS
    keys: set = set()
    for name in ("form-config", "form-field-config"):
        try:
            schema = ctx.asset_client.get_schema(name) or {}
            _collect_schema_keys(schema, keys)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"load schema {name} failed: {e}")
    _ALLOWED_KEYS = keys or {"formFieldConfigVos"}  # 兜底：至少别把字段列表删了
    return _ALLOWED_KEYS



# 校验失败后的最大重试次数(防止死循环)
MAX_RETRIES = 3

# 增量指令的尝试上限(首次 + 2 次锚点/校验修正)。超限升格全量模式。
OPS_MAX_ATTEMPTS = 3


class ModifyFormTool(CompositeTool):
    """根据自然语言指令修改已有 njmind 表单配置。

    【职责】
      1. 从 state["source_artifact"](已有配置)出发
      2. 跑 3 步管线:fetch_guide → modify → validate
      3. 校验失败时自动带错误信息重跑 modify(最多 MAX_RETRIES 次)
      4. 返回修改后的 artifact + summary

    【Java 类比】
      ``class ModifyFormTool extends CompositeTool``,
      相当于一个带事务、带重试、带多步编排的 Service。
      ``run_pipeline`` 是基类提供的模板方法,自动按 steps 顺序调
      ``_step_fetch_guide / _step_modify / _step_validate``。

    【破坏性标记】
      引擎会据此要求确认 / 记审计日志。
    """

    # ── 工具元数据 ──
    name = "modify_form"
    description = "修改已有表单配置(加/删/改字段)"
    when = "用户想修改已有表单,如'加一个手机号字段'、'删除xxx'、'把xxx改成必填'"

    # ── 安全声明 ──
    # 修改操作:有破坏性、非只读、非并发安全(并发改同一表单会冲突)

    # ── 插件化元数据 ──
    # modify 必须有已有配置:不能凭空改,得先有 source_artifact
    # (通常先跑 GetFormTool 拿到配置,再跑本工具修改)

    # steps 是 CompositeTool 的核心声明:定义管线的步骤顺序
    # 基类 run_pipeline 会按此顺序调用 _step_fetch_guide / _step_modify / _step_validate
    # （"modify" 步内部两相：增量主路径 plan_ops→apply_ops，全量重生成兜底）
    steps = ["fetch_guide", "modify", "validate"]

    # Pipeline 步骤定义(用于前端动态渲染进度条)
    # key 对应 SSE stage 事件名（_step_modify 内部 emit plan_ops / apply_ops），
    # 前端按 stage 前缀匹配高亮，全量兜底时复用 apply_ops 节点（stage 名不变）
    pipeline_steps = [
        {"key": "fetch_guide", "label": "获取指南"},
        {"key": "plan_ops", "label": "规划指令"},
        {"key": "apply_ops", "label": "应用修改"},
        {"key": "validate", "label": "校验结果"},
    ]

    def input_schema(self) -> dict:
        """输入 schema:user_input + source_artifact 都必填。

        Returns:
            JSON Schema dict,source_artifact 是 object 类型(完整表单配置)。
        """
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的修改指令"},
                "source_artifact": {"type": "object", "description": "已有的表单配置"},
            },
            "required": ["user_input", "source_artifact"],
        }

    def validate_input(self, state: dict) -> Optional[str]:
        """语义校验:modify 必须有 source_artifact。

        【与 input_schema 的区别】
          input_schema 校验"字段是否存在 + 类型对不对"(结构校验);
          validate_input 校验"业务上是否合理"(语义校验)。
          类比 Java:Bean Validation (@NotNull) vs 业务校验逻辑。

        Args:
            state: 工作流状态

        Returns:
            错误信息字符串(校验失败时),或 None(校验通过)。
        """
        if not state.get("source_artifact"):
            return "修改表单需要已有配置(source_artifact),但当前没有"
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行 3 步管线。

        【流程】
          1. 初始化 retry_count / validation_errors(防止 KeyError)
          2. 调基类 run_pipeline 跑完 3 步(内部可能多次重试)
          3. 从 state["artifact"] 取最终结果,组装 ToolResult

        Args:
            state: 工作流状态(会被管线修改:写入 artifact / guide / validation_errors)
            ctx:   执行上下文

        Returns:
            ToolResult:成功带 artifact;失败(extra.validation_errors 非空)仍返回,
            但 summary 会反映"未完成"。
        """
        # setdefault:不存在才设默认值,存在不动(类比 Java 的 computeIfAbsent)
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])

        # 核心:跑管线。基类会按 self.steps 顺序调各 _step_xxx 方法
        self.run_pipeline(state, ctx)

        # 管线跑完后,artifact 是最终配置(可能经过了多次重试修改)
        artifact = state.get("artifact")
        validation_errors = state.get("validation_errors", [])
        if artifact:
            form_name = artifact.get("formName", "")
            field_count = len(artifact.get("formFieldConfigVos", []))
            if validation_errors:
                # 诚实化：校验未通过就不是"已修改"，避免误导用户点应用
                errs = "; ".join(
                    e.get("message", str(e))[:60] for e in validation_errors[:3]
                )
                summary = f"「{form_name}」修改后未通过上游校验（共 {len(validation_errors)} 处）：{errs}"
            else:
                summary = f"已修改「{form_name}」,共 {field_count} 个字段"
                # 增量模式：附上实际应用的指令摘要，让用户看到具体改了什么
                applied = state.get("_ops_applied") or []
                if applied:
                    summary += f"（{'；'.join(applied[:4])}）"
        else:
            # 没产出 artifact:可能是 modify 步骤异常 / validate 全部失败
            summary = "表单修改未完成"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            extra={
                # 校验错误列表(供前端展示 / LLM 解释失败原因)
                "validation_errors": state.get("validation_errors", []),
                "formatted": self.format_result(artifact) if artifact else {},
            },
        )

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:把 artifact 压成一行摘要。

        与 GetFormTool.summarize_artifact 几乎一样,只是前缀改成"当前表单"
        (因为这是修改后的最新版本)。

        Args:
            artifact: 修改后的表单配置

        Returns:
            单行摘要字符串。
        """
        form_name = artifact.get("formName", "")
        form_code = artifact.get("formCode", "")
        fields = artifact.get("formFieldConfigVos", [])
        field_summary = ", ".join(
            f.get("fieldTitleText", "") for f in fields[:10]
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"当前表单: {form_name} ({form_code}), 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从 artifact 生成会话标题。

        Args:
            artifact: 表单配置

        Returns:
            表单名(没有则"新对话")。
        """
        return artifact.get("formName", "新对话")

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从 artifact 提取前端渲染字段(钩子方法)。

        与 GetFormTool.format_result 一致,把完整配置转成精简视图模型。

        Args:
            artifact: 完整表单配置

        Returns:
            含 fieldCount / formName / formCode / title 的精简 dict。
        """
        fields = artifact.get("formFieldConfigVos", [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "新对话"),
        }

    # ── Steps(管线的三个具体步骤) ──────────────────────────

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        """Step 1: 获取配置指南。

        【作用】
          从上游拉"配置指南"——通常是字段类型映射、必填规则等元数据。
          后续 modify 步骤会把 guide 喂给 LLM,让它知道"有哪些合法字段类型"。

        Args:
            state: 会写入 state["guide"]
            ctx:   提供 asset_client + emit
        """
        ctx.emit("stage", "fetch_guide", "正在从上游获取配置指南...")
        state["guide"] = ctx.asset_client.get_guide()

    def _step_modify(self, state: dict, ctx: ToolContext) -> None:
        """Step 2: 修改配置 —— 增量主路径 + 全量兜底（两相式）。

        【增量主路径】（默认，Claude Code Edit 同源思路）
          build_catalog(纯代码) → plan_ops(LLM 只输出指令集,几百字节)
          → apply_ops(纯代码确定性合并) → postprocess(复用)
          prompt 从全量 16-20KB 降到 ~3KB(目录 1-2KB + 协议说明)。

        【升格全量的三条路径】
          1. LLM 输出 {"op":"full_rewrite"}（大重构指令表达不了,主动请求）
          2. 指令锚点失败重试超限(OPS_MAX_ATTEMPTS)
          3. LLM 输出不可解析
          升格后 state["_mode"]="full"，本会话后续重试都走全量（不再反复尝试）。

        【校验失败重试】
          _step_validate 失败时回调本方法：仍走增量（带着校验错误重新 plan，
          base=上次产物），增量再次失败才升全量 —— 快路径优先。

        Args:
            state: 读 user_input / source_artifact / artifact / guide /
                   compressed_history / validation_errors / _ops_failures
                   写 artifact / validation_errors / _mode / _ops_applied
            ctx:   提供 emit / llm_client / prompt_loader / asset_client
        """
        if state.get("_mode") != "full":
            if self._modify_incremental(state, ctx):
                return
            # 增量未成功：升格全量。stage 复用 apply_ops 保持前端进度条高亮正确
            state["_mode"] = "full"
            ctx.emit("stage", "apply_ops", "修改较复杂，正在全量重新生成配置...")

        self._modify_full(state, ctx)

    # ── 增量主路径 ─────────────────────────────────────────────

    def _modify_incremental(self, state: dict, ctx: ToolContext) -> bool:
        """增量两相：plan_ops ⇄ apply_ops 循环（失败带清单重试）。

        Returns:
            True = 成功产出 state["artifact"]；False = 应升格全量。
        """
        # 重试(校验失败)基于上次产物（离正确最近的版本）；首次基于原始配置
        is_retry = bool(state.get("validation_errors"))
        base = state.get("artifact") if is_retry else state.get("source_artifact")
        if not base:
            logger.error("ModifyFormTool: no base config for incremental modify")
            return False

        # 类型名映射（目录展示用，guide.fieldTypes 是事实源）
        guide = state.get("guide") or {}
        type_names = {}
        for t in guide.get("fieldTypes", []):
            if t.get("code") is not None:
                type_names[int(t["code"])] = t.get("name", str(t["code"]))

        failures = state.get("_ops_failures") or []
        validation_errors = state.get("validation_errors", []) if is_retry else []

        for attempt in range(OPS_MAX_ATTEMPTS):
            if attempt == 0 and not failures:
                ctx.emit("stage", "plan_ops", "AI 正在规划修改指令...")
            else:
                ctx.emit("stage", "plan_ops",
                         f"正在修正修改指令（第 {attempt} 次）...")

            catalog = build_catalog(base, type_names)
            ops = self._plan_ops(state, ctx, catalog, failures, validation_errors)
            if not ops:
                return False  # 输出不可解析 → 升全量
            rw = next((o for o in ops
                       if isinstance(o, dict) and o.get("op") == "full_rewrite"), None)
            if rw is not None:
                # 升格原因记日志（可观测：哪类指令超出了指令集表达力）
                logger.info(f"incremental -> full_rewrite: {rw.get('reason', '(无理由)')}")
                return False  # LLM 主动请求全量

            ctx.emit("stage", "apply_ops", f"正在应用 {len(ops)} 条修改指令...")
            ar = apply_ops(base, ops,
                           template_loader=self._make_template_loader(state, ctx))
            if ar.ok:
                # postprocess（归一化新内容）→ 还原未触碰字段（见函数 docstring：
                # 否则 B 字段的 null/布尔会被归一化，diff 视图显示"误伤"）
                state["artifact"] = restore_untouched(
                    postprocess_config(ar.new_config), base, ar)
                state["validation_errors"] = []
                state["_ops_failures"] = []
                # 应用的指令摘要（execute 组装 summary 用，给用户增量透明度）
                state["_ops_applied"] = ar.applied
                return True

            failures = ar.failures
            state["_ops_failures"] = failures

        logger.warning(f"incremental ops exhausted: {failures}")
        return False

    def _plan_ops(self, state: dict, ctx: ToolContext,
                  catalog, failures: list, validation_errors: list):
        """调 LLM 产出指令集。失败/空输出返回 None（触发升全量）。"""
        system_prompt = self._render_prompt(
            ctx, "modify_ops",
            catalog=catalog.text,
            guide=state.get("guide") or {},
        )

        user_parts = []
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])
        user_parts.extend(["## 修改指令", state.get("user_input", ""), ""])

        if validation_errors:
            # 校验失败重试：错误带进指令规划（多半是 patch 值/新增字段结构问题）
            error_msgs = [
                e.get("message", str(e))
                for e in validation_errors[:5]
            ]
            user_parts.extend([
                "## 上一轮产物校验失败，请在指令中修复",
                "\n".join(f"- {m}" for m in error_msgs), "",
            ])
        if failures:
            # 锚点失败重试：失败清单 + 合法锚点（同 Claude Code 报错重读闭环）
            user_parts.extend(["## 上一轮指令执行失败", format_failures(failures, catalog.keys), ""])

        user_parts.append("请输出指令集 JSON。")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]
        try:
            parsed = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
        except Exception as e:
            logger.warning(f"plan_ops LLM failed: {e}")
            return None
        # 容错：期望 {"ops":[...]}，LLM 直接给数组也接受
        if isinstance(parsed, dict):
            ops = parsed.get("ops")
        elif isinstance(parsed, list):
            ops = parsed
        else:
            ops = None
        return ops if isinstance(ops, list) and ops else None

    def _make_template_loader(self, state: dict, ctx: ToolContext):
        """add_field 骨架 fallback 的模板加载器（同类型克隆优先，很少走到）。

        stem 推导与 create 管线 _step_fetch_templates 完全同源：
        guide.fieldTypes 名称小写，config.yaml 例外覆盖。
        """
        guide = state.get("guide") or {}
        ft_map = {}
        for t in guide.get("fieldTypes", []):
            if t.get("code") is not None:
                ft_map[int(t["code"])] = t

        def loader(type_code):
            ftype = ft_map.get(int(type_code)) if isinstance(type_code, (int, float)) else None
            if not ftype:
                return None
            type_name = ftype.get("name", str(type_code))
            stem = field_template_stem(int(type_code), type_name)
            try:
                return ctx.asset_client.get_template(f"{stem}_field")
            except Exception as e:
                logger.warning(f"load field template {stem} failed: {e}")
                return None

        return loader

    # ── 全量兜底路径（原 modify 逻辑，大重构/增量失败时启用）─────

    def _modify_full(self, state: dict, ctx: ToolContext) -> None:
        """全量重生成：LLM 吃完整配置、回吐完整配置（~33s，兜底用）。

        【两种模式】
          - 首次/锚点升格:基于 source_artifact(用户原始配置)
          - 校验失败重试:基于 artifact(上一次的产出,带校验错误一起再喂)
        """
        is_retry = bool(state.get("validation_errors"))
        # 全量路径清掉增量残留的指令摘要（summary 必须与实际产物一致）
        state.pop("_ops_applied", None)

        # stage 名用 apply_ops：前端 pipeline 定义只有 fetch_guide/plan_ops/apply_ops/validate
        # 四个 key，emit "modify" 会导致 findIndex 全不匹配 → 进度条整条清空回灰
        if is_retry:
            ctx.emit("stage", "apply_ops", f"校验失败，正在全量修复（第 {state.get('retry_count', 0)} 次重试）...")
        else:
            ctx.emit("stage", "apply_ops", "AI 正在根据指令全量重新生成配置...")

        # 基础配置:retry 用 artifact(上次的产出),首次用 source_artifact(原始)
        # 为什么重试用 artifact?因为 artifact 是"离正确最近的版本",
        # 带着校验错误让 LLM 修补,比从原始重改效率高
        base_config = state.get("artifact") if is_retry else state.get("source_artifact")
        if not base_config:
            # 兜底:理论上不会走到,防 NPE
            logger.error("ModifyFormTool: no base config to modify!")
            state["artifact"] = state.get("source_artifact")
            return

        # 渲染 system prompt:把指南 + 字段类型模板拼进去
        # 通过 ctx.prompt_loader 加载模板(类比 Spring 的 TemplateEngine)
        system_prompt = self._render_prompt(
            ctx, "modify",
            config=base_config,
            guide=state.get("guide") or {},
        )

        # 构建 user message:对话历史 + 修改指令 [+ 校验错误]
        user_parts = []
        # 压缩历史让 LLM 知道上下文(用户之前可能提过相关需求)
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])

        if is_retry:
            # 重试模式:把校验错误明示给 LLM,让它针对性修复
            # 只取前 5 条错误,避免 prompt 过长
            error_msgs = [
                e.get("message", str(e))
                for e in state.get("validation_errors", [])[:5]
            ]
            # 注意：不在 user 消息里重复转储完整配置——system 的「当前配置」
            # 已包含（大表单重复一份 = prompt 直接翻倍，141K 字符的元凶之一）
            user_parts.extend([
                "## 原始修改指令",
                state.get("user_input", ""),
                "",
                "## 校验失败，请修复（基于 system 中的当前配置）",
                "\n".join(f"- {m}" for m in error_msgs),
                "",
                "请修复后输出完整配置。",
            ])
        else:
            # 首次模式:只给指令,让 LLM 自由修改
            user_parts.extend([
                "## 修改指令",
                state.get("user_input", ""),
                "",
                "请根据指令修改上面的配置，输出修改后的完整 JSON。",
            ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

        # 调 LLM 产出新配置(JSON),chat_json 自动解析成 dict
        config = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
        # 机械后处理（布尔→0/1、字段去重、formTitle 兜底）：确定性偏差不烧 LLM 重试
        config = postprocess_config(config)
        # 【硬约束·按延续性判断】formCode/formConfigId 是表单的数据库标识——
        # 是否保留由**确定性数据规则**决定（不靠 LLM reason）：
        #   产物字段 key 与基线有交集 = 同一表单的延续（重排/大改）→ 保留标识
        #     （真实事故：全量重写时 LLM 漏写 formCode，产物直接丢了表单标识）；
        #   零交集 = 换主题式重做（如"把这张表单改成请假单"）→ 新表单语义，
        #     不回填（强盖旧标识会产出"半新半旧"配置：formCode 是旧表单、
        #     字段 key 全新——已存数据列全部错位）。
        base_keys = {str(f.get("fieldTitleKey"))
                     for f in (base_config.get("formFieldConfigVos") or [])
                     if f.get("fieldTitleKey")}
        new_keys = {str(f.get("fieldTitleKey"))
                    for f in (config.get("formFieldConfigVos") or [])
                    if f.get("fieldTitleKey")}
        is_continuation = bool(base_keys & new_keys) or not new_keys
        if is_continuation:
            if base_config.get("formCode"):
                config["formCode"] = base_config["formCode"]
            if base_config.get("formConfigId"):
                config["formConfigId"] = base_config["formConfigId"]
        else:
            # 换主题重做：按新表单处理——清掉 LLM 可能残留的旧数据 ID
            # （上游 CREATE 模式要求 formConfigId 为空，F6 校验）
            config.pop("formConfigId", None)
            logger.info("full_rewrite 零字段交集：按新表单处理（不保留旧标识）")
        # 写回 state,供 validate 步骤使用
        state["artifact"] = config
        # 重试场景下清空上轮的错误(本轮还没校验,先清空)
        state["validation_errors"] = []

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        """差分校验：AI 只为「新增的问题」负责。

        背景（真实链路实测）：上游 validate 比 designer 保存口径更严——
        原始配置本身就带违规（如 selectMode=null 却必填、VO 外字段），
        schema 与 VO 还有漂移（icon/relevanceFormId 在 schema 不在 VO）。
        若要求 AI 结果绝对通过，等于要求它修复宿主存量问题——重试烧尽也做不到。

        机制：
          1. 原始配置（source_artifact）先验一次，得「存量违规清单」（含未知字段
             本地剥离循环），缓存在 state，重试间复用；
          2. AI 结果同样投影+剥离未知字段后校验；
          3. 只把「新增违规」（存量清单里没有的）算失败交给 LLM 重试；
             全部为存量 → 视为通过（AI 没有让配置变得更糟）。
          4. 未知字段错误（Unrecognized field）从不进 LLM——机械剥离后本地重验。
        """
        ctx.emit("stage", "validate", "正在提交到上游平台进行校验...")
        artifact = state.get("artifact")
        if not artifact:
            state["validation_errors"] = [{"message": "No configuration to validate"}]
            ctx.emit("stage", "validate_fail", "校验失败：无配置可校验")
            return

        import copy as _copy
        allowed = _get_allowed_keys(ctx)
        mode = "update"

        def _validate_clean(cfg) -> dict:
            """投影 → 机械修复循环（剥未知字段 + 补必填/修值域，≤4 轮）→ 结果。

            「必填项缺失/值域不合法」与 Unrecognized field 同属确定性错误：
            值从 表内同类型字段→上游模板→config 兜底 三级抄写，绝不烧 LLM
            （上游 user_field/department_field 模板自带缺 selectMode 的坑，
            按模板生成的产物首轮必挂——此前每单都要多烧一轮 35s 的重试）。
            """
            proj = schema_projection(_copy.deepcopy(cfg), allowed)
            for _ in range(4):
                result = ctx.asset_client.validate_artifact(proj, mode=mode)
                unk = parse_unrecognized_fields(result.get("errors", []))
                fixable = parse_fixable_field_errors(result.get("errors", []))
                if not unk and not fixable:
                    return result
                if unk:
                    # 剥未知键只作用于投影副本（VO 外字段画布渲染/保存需要）
                    proj = strip_keys(proj, unk)
                if fixable:
                    # 补缺失/修错值：本体 + 投影同步补——本体缺 selectMode 的话
                    # 画布上人员/部门选择也渲染不正常，修复对本体是纯收益
                    fill_missing_required(
                        cfg, fixable,
                        template_getter=self._make_template_loader(state, ctx),
                        prop_defaults=load_prop_defaults(),
                    )
                    fill_missing_required(
                        proj, fixable,
                        template_getter=self._make_template_loader(state, ctx),
                        prop_defaults=load_prop_defaults(),
                    )
            return result

        # 原始配置的存量违规（每请求只算一次，重试复用）
        if "_orig_error_set" not in state:
            source = state.get("source_artifact") or {}
            if source:
                r_orig = _validate_clean(source)
                state["_orig_error_set"] = {
                    normalize_error(e) for e in r_orig.get("errors", [])
                }
            else:
                state["_orig_error_set"] = set()

        result = _validate_clean(artifact)
        warnings = result.get("warnings", [])
        raw_errors = result.get("errors", [])

        # 差分：只保留 AI 新引入的错误
        new_errors = [
            e for e in raw_errors
            if normalize_error(e) not in state["_orig_error_set"]
        ]

        if not new_errors:
            state["validation_errors"] = []
            if warnings:
                ctx.emit("stage", "validate_pass", f"校验通过 ✓（{len(warnings)} 个警告）")
            else:
                ctx.emit("stage", "validate_pass", "校验通过 ✓")
            return

        # 有新增错误 → 交给 LLM 重试（错误带"新增"标注，避免 LLM 去修存量问题）
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = new_errors
        if state["retry_count"] < MAX_RETRIES:
            error_msgs = [normalize_error(e)[:80] for e in new_errors[:3]]
            ctx.emit("stage", "validate_retry",
                     f"校验失败：{'；'.join(error_msgs)}，正在重试（第 {state['retry_count']} 次）...")
            self._step_modify(state, ctx)
            return self._step_validate(state, ctx)
        else:
            error_msgs = [normalize_error(e)[:80] for e in new_errors[:3]]
            ctx.emit("stage", "validate_fail",
                     f"校验失败（已达最大重试次数）：{'；'.join(error_msgs)}")


    # ── 辅助方法 ───────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        """通过 ctx.prompt_loader 渲染模板。

        【设计】
          prompt 模板不硬编码在代码里,而是放在外部文件(prompt_loader 加载),
          方便产品 / 运营调整 prompt 不用改代码。
          类比 Java:Spring 的 ``@Value`` + PropertyPlaceholder / Thymeleaf。

        Args:
            ctx:  执行上下文,提供 prompt_loader
            name: 模板名(如 "modify")
            **vars: 模板变量

        Returns:
            渲染后的 prompt 字符串;若没有 prompt_loader 则返回空串(降级)。
        """
        # hasattr 检查:防御性编程,有些 ctx 可能没注入 prompt_loader
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            # render(domain, template_name, **vars):按域 + 名加载模板并填充变量
            return ctx.prompt_loader.render("njmind_form", name, **vars)
        # 降级:没 loader 就返回空 prompt(可能导致 LLM 表现差,但不会崩)
        logger.warning(f"No prompt_loader, returning empty for {name}")
        return ""
