"""CreateFormTool - 创建表单的复合工具(6 步管线)。

把现有 nodes.py 的 6 步 CREATE 管线搬进 CompositeTool:
  fetch_guide -> list_assets -> parse_fields -> fetch_templates -> generate -> validate

所有 njmind 业务字段名只出现在本文件内,Engine 从不访问。
"""
import json
import logging
from typing import Any, Dict

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_form.models import ParsedField
from domains.njmind_form.tools._config_loader import load_type_mappings
from domains.njmind_form.tools._config_loader import load_prop_defaults, field_template_stem
from domains.njmind_form.tools._postprocess import (
    parse_fixable_field_errors, fill_missing_required,
    postprocess_config, _collect_schema_keys, schema_projection,
    parse_unrecognized_fields, strip_keys,
)

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



# 类型码 → 模板 stem 的例外覆盖表（config.yaml 读取）。
# 全量类型表以上游 guide.json 的 fieldTypes 为唯一事实源（见 _step_fetch_templates），
# 这里只保留「推导规则（name.lower()）不适用」的例外。
_TYPE_TO_TEMPLATE, _TYPE_NAMES = load_type_mappings()



MAX_RETRIES = 3


class CreateFormTool(CompositeTool):
    """根据自然语言需求生成 njmind 表单配置。"""

    name = "create_form"
    description = "根据自然语言需求生成 njmind 表单配置"
    when = "用户想新建表单时,如'创建一个请假表'、'新建客户信息表'"

    # ── 安全声明 ──
    
    # ── 插件化元数据 ──

    steps = ["fetch_guide", "list_assets", "parse_fields",
             "fetch_templates", "generate", "validate"]
    
    # Pipeline 步骤定义(用于前端动态渲染)
    pipeline_steps = [
        {"key": "fetch_guide", "label": "获取指南"},
        {"key": "list_assets", "label": "加载模板"},
        {"key": "parse_fields", "label": "解析字段"},
        {"key": "fetch_templates", "label": "匹配模板"},
        {"key": "generate", "label": "生成配置"},
        {"key": "validate", "label": "校验结果"},
    ]

    def input_schema(self) -> dict:
        # JSON Schema 描述输入结构,类比 Java 的 DTO + @Valid 注解
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的自然语言需求"}
            },
            "required": ["user_input"],  # user_input 必填
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行 6 步管线。retry 在 _step_validate 内部处理。"""
        # 插件自检（意图识别的最后防线）：画布非空（含未保存草稿——只要画布上
        # 有内容，用户说"加/删/改"就应该是修改而非从零创建）。此处不强行改道
        # （工具间跳转不在框架内），而是在首步前给用户一句明确提示，让用户
        # 发现路线错了可以撤销；真正的路由正确性由意图识别的规则 2 保证。

        # 初始化计数器与错误列表(setdefault 幂等,避免重跑时清空已有值)
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])

        # 跑 6 步管线:fetch_guide → list_assets → parse_fields
        #          → fetch_templates → generate → validate
        self.run_pipeline(state, ctx)

        artifact = state.get("artifact")
        validation_errors = state.get("validation_errors", [])
        if artifact:
            # 成功:从制品提取表单名和字段数,拼人类可读摘要
            form_name = artifact.get("formName", "")
            field_count = len(artifact.get("formFieldConfigVos", []))
            if validation_errors:
                # 诚实化：校验未通过就不是"已生成"
                errs = "; ".join(
                    e.get("message", str(e))[:60] for e in validation_errors[:3]
                )
                summary = f"「{form_name}」生成后未通过上游校验（共 {len(validation_errors)} 处）：{errs}"
            else:
                summary = f"已生成「{form_name}」,共 {field_count} 个字段"
        else:
            # 失败:未生成制品(可能重试耗尽)
            summary = "表单生成未完成"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            extra={
                # 校验错误透传给 handle_result,前端据此决定是否禁用保存按钮
                "validation_errors": state.get("validation_errors", []),
                # format_result 提取前端要的字段(钩子化,避免 Engine 读制品内部)
                "formatted": self.format_result(artifact) if artifact else {},
            },
        )

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿。"""
        # 压缩后 LLM 会忘掉细节,这里把表单关键信息复灌回去
        form_name = artifact.get("formName", "")  # 表单中文名
        form_code = artifact.get("formCode", "")  # 表单编码(英文标识)
        fields = artifact.get("formFieldConfigVos", [])  # 字段列表
        # 只取前 10 个字段名拼摘要,避免补偿文本过长浪费 token
        field_summary = ", ".join(
            f.get("fieldTitleText", "") for f in fields[:10]
        )
        if len(fields) > 10:
            # 超过 10 个:用省略号 + 总数提示,类比 Java 的 truncate
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"当前表单: {form_name} ({form_code}), 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从制品生成标题。"""
        # 用表单名作为对话标题,缺省"新对话"
        return artifact.get("formName", "新对话")

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从制品提取前端需要的字段(钩子化,避免 Engine 读制品内部)。"""
        fields = artifact.get("formFieldConfigVos", [])  # 字段列表
        # 只提取前端展示需要的字段(钩子化:Engine 不直接读 njmind 业务字段)
        return {
            "fieldCount": len(fields),  # 字段总数(前端显示)
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "新对话"),  # 给对话列表用
        }

    # ── Steps ──────────────────────────────────────────────────

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        """获取配置指南。"""
        # emit 推进度事件给前端(类比 Java 的进度回调)
        ctx.emit("stage", "fetch_guide", "正在从上游获取配置指南...")
        # 从上游拉配置指南(字段说明、约束规则等),存进 state 供后续步骤用
        state["guide"] = ctx.asset_client.get_guide()

    def _step_list_assets(self, state: dict, ctx: ToolContext) -> None:
        """列出可用模板和 Schema 文件名。"""
        ctx.emit("stage", "list_assets", "正在获取可用模板和 Schema 列表...")
        # 拉可用模板名列表(本阶段只取文件名,内容在 fetch_templates 步骤按需拉)
        templates = ctx.asset_client.list_templates()
        state["template_names"] = templates
        # 推完成事件:前端据此把"加载模板"标记为已完成
        ctx.emit("stage", "list_assets_done", f"发现 {len(templates)} 个可用模板")

    def _step_parse_fields(self, state: dict, ctx: ToolContext) -> None:
        """LLM 解析自然语言 -> 结构化字段列表。"""
        ctx.emit("stage", "parse_fields", "AI 正在解析您的自然语言需求...")
        user_input = state.get("user_input", "")
        compressed_history = state.get("compressed_history", "")
        # 追问恢复：interrupt 后引擎把用户回答注入 tool_state["clarify_answers"] 并重跑本工具。
        # 不合并进输入的话，重跑仍是原始模糊描述 → 再次追问 → 死循环。
        clarify = state.get("clarify_answers") or {}
        if clarify:
            extra = clarify.get("text") or "；".join(
                f"{k}: {v}" for k, v in clarify.items() if k != "text"
            )
            if extra:
                user_input = f"{user_input}\n（用户补充回答：{extra}）"
        guide = state.get("guide") or {}  # 上一步拉的配置指南

        # 渲染 prompt:通过 prompt_loader 从模板文件加载,注入 guide 变量
        system_prompt = self._render_prompt(ctx, "parse", guide=guide)
        user_msg = self._build_parse_user_message(user_input, compressed_history)

        # 组装 LLM 消息(对标 ChatGPT 的 messages 数组)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        # 调 LLM(要求返回 JSON),conv_id 用于多轮上下文追踪
        parsed = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)

        # 检查是否需要追问:LLM 判断信息不足时,抛 ClarificationRaised
        # 该异常会被 execute_tool_node 捕获,转成 interrupt 挂起等用户回答
        if parsed.get("needsClarification"):
            questions = parsed.get("clarificationQuestions", [])
            raise ClarificationRaised(questions)

        # 解析表单元信息:表单名 + 表单编码
        state["form_name"] = parsed.get("formName", "新表单")
        state["form_code"] = parsed.get("formCode", "new_form")

        # 把 LLM 返回的原始字段 dict 转成强类型 ParsedField 列表
        raw_fields = parsed.get("fields", [])
        parsed_fields = []
        for f in raw_fields:
            type_code = f.get("fieldType", 0)  # 字段类型编码(数字)
            parsed_fields.append(ParsedField(
                fieldTitleText=f.get("fieldTitleText", ""),  # 字段中文名
                fieldTitleKey=f.get("fieldTitleKey", ""),  # 字段 key(英文标识)
                formFieldType=type_code,
                # 类型名兜底:LLM 没给就从配置映射表查,再不行默认 TEXT
                fieldTypeName=f.get("fieldTypeName", _TYPE_NAMES.get(type_code, "TEXT")),
                description=f.get("description", ""),
                options=f.get("options"),  # 选项(单选/多选用)
            ))
        state["parsed_fields"] = parsed_fields
        ctx.emit("stage", "parse_fields_done", f"已解析出 {len(parsed_fields)} 个字段：{state['form_name']}")

    def _step_fetch_templates(self, state: dict, ctx: ToolContext) -> None:
        """获取表单模板 + 按字段类型获取字段模板（类型→模板映射以 guide.json 为事实源）。"""
        ctx.emit("stage", "fetch_templates", "正在匹配字段模板...")
        # 表单模板:整体表单的骨架(所有表单共用一个 simple_form 模板)
        state["form_template"] = ctx.asset_client.get_template("simple_form")

        # 字段模板(按类型去重):收集本表单用到的所有字段类型,避免重复拉取
        needed_types = set()
        for f in state.get("parsed_fields", []):
            needed_types.add(f.formFieldType)

        # 以 guide.json 的 fieldTypes 为唯一事实源，运行时构建 code→(模板stem, 类型名) 映射。
        # 类型名 → 模板 stem 默认按 name.lower() 推导（TEXT → text_field）；
        # 个别不一致的类型名由 config.yaml 的 type_template_overrides 覆盖。
        # 上游无模板的类型（条码/标签页/文本段/签名）→ fail-closed，禁止静默回退 text。
        guide = state.get("guide") or {}
        ft_map = {}
        for t in guide.get("fieldTypes", []):
            code = t.get("code")
            if code is not None:
                ft_map[int(code)] = t

        # 按类型拉对应字段模板,用类型名做 key 方便后续模板渲染
        field_templates = {}
        missing = []
        for type_code in needed_types:
            ftype = ft_map.get(int(type_code))
            if not ftype:
                missing.append(str(type_code))
                continue
            type_name = ftype.get("name", str(type_code))
            # 默认 stem = name.lower()；例外覆盖表（config.yaml）优先
            stem = field_template_stem(type_code, type_name)
            tmpl = ctx.asset_client.get_template(f"{stem}_field")
            if not tmpl:
                missing.append(type_name)
                continue
            field_templates[type_name] = tmpl

        # fail-closed：出现无模板的类型时抛追问，绝不静默回退 text
        # （错误生成 → 校验重试死循环烧 token，比明确追问更糟）
        if missing:
            raise ClarificationRaised(
                [f"字段类型「{', '.join(missing)}」暂不支持 AI 生成，请改用文本/数字/单选等基础类型"]
            )

        state["field_templates"] = field_templates
        ctx.emit("stage", "fetch_templates_done", f"已加载 {len(field_templates)} 种字段类型模板")

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        """LLM 基于模板组装完整 FormConfig。"""
        # 是否重试:validation_errors 非空说明上一轮校验失败,本轮要修复
        is_retry = bool(state.get("validation_errors"))

        if is_retry:
            # 重试模式:告诉前端在修复错误,并显示第几次重试
            ctx.emit("stage", "generate_retry", f"校验失败，正在修复并重新生成（第 {state.get('retry_count', 0)} 次重试）...")
        else:
            ctx.emit("stage", "generate", "AI 正在基于模板组装完整表单配置...")

        # 渲染 prompt:注入表单骨架模板 + 各字段类型模板
        # LLM 按模板填充,而非自由发挥,保证产出结构稳定
        system_prompt = self._render_prompt(
            ctx, "generate",
            form_template=state.get("form_template") or {},
            field_templates=state.get("field_templates") or {},
        )

        # 构建 user message:把解析出的字段信息序列化给 LLM
        fields_data = {
            "formName": state.get("form_name", ""),
            "formCode": state.get("form_code", ""),
            "fields": [
                {
                    "fieldTitleText": f.fieldTitleText,
                    "fieldTitleKey": f.fieldTitleKey,
                    "fieldType": f.formFieldType,
                    "fieldTypeName": f.fieldTypeName,
                    # 有 options 才加(单选/多选),避免空字段干扰
                    **({"options": f.options} if f.options else {}),
                }
                for f in state.get("parsed_fields", [])
            ],
        }

        user_parts = []
        # 有历史才追加(多轮场景下帮 LLM 理解上下文)
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])

        if is_retry and state.get("artifact"):
            # 重试路径:把校验错误 + 当前配置给 LLM,让它针对性修复
            # 只取前 5 条错误,避免 prompt 过长
            error_msgs = [
                e.get("message", str(e))
                for e in state.get("validation_errors", [])[:5]
            ]
            user_parts.extend([
                "## 校验失败，请修复",
                "\n".join(f"- {m}" for m in error_msgs),
                "",
                "## 当前配置",
                f"```json\n{json.dumps(state['artifact'], ensure_ascii=False)}\n```",
                "请修复后输出完整配置。",
            ])
        else:
            # 首次生成:给字段信息,让 LLM 按模板组装
            user_parts.extend([
                "## 字段信息",
                f"```json\n{json.dumps(fields_data, ensure_ascii=False, indent=2)}\n```",
                "",
                "请根据以上字段信息和模板，组装完整的表单配置 JSON。",
            ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

        # 调 LLM 生成完整配置,存为 artifact
        config = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
        # 机械后处理（布尔→0/1、字段去重、formTitle 兜底）：确定性偏差不烧 LLM 重试
        config = postprocess_config(config)
        state["artifact"] = config
        state["validation_errors"] = []  # 重置错误:新生成的配置要先校验才算数

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        """提交上游校验（含未知字段本地剥离循环）。失败时工具内部 retry。"""
        ctx.emit("stage", "validate", "正在提交到上游平台进行校验...")
        artifact = state.get("artifact")
        if not artifact:
            state["validation_errors"] = [{"message": "No configuration to validate"}]
            ctx.emit("stage", "validate_fail", "校验失败：无配置可校验")
            return

        import copy as _copy
        allowed = _get_allowed_keys(ctx)
        # 投影 + 机械修复循环（≤4 轮，不烧 LLM）：
        #   剥未知字段（Unrecognized）+ 补必填/修值域（上游 user_field 等
        #   模板自带缺 selectMode，按模板生成的产物首轮必挂——本地三级抄值修复）
        proj = schema_projection(_copy.deepcopy(artifact), allowed)
        result = ctx.asset_client.validate_artifact(proj, mode="create")
        for _ in range(4):
            unk = parse_unrecognized_fields(result.get("errors", []))
            fixable = parse_fixable_field_errors(result.get("errors", []))
            if not unk and not fixable:
                break
            if unk:
                proj = strip_keys(proj, unk)
            if fixable:
                # 模板来源：fetch_templates 步已拉好的 field_templates（类型名→模板），
                # 免网络调用；缺的类型由 config 兜底表接住。
                # 本体 + 投影同步补：本体缺 selectMode 画布上人员/部门选择渲染不正常
                _guide = state.get("guide") or {}
                _name_by_code = {
                    int(t["code"]): t.get("name")
                    for t in _guide.get("fieldTypes", []) if t.get("code") is not None
                }
                _ft = state.get("field_templates") or {}
                def _tmpl_loader(code):
                    nm = _name_by_code.get(int(code)) if isinstance(code, (int, float)) else None
                    return _ft.get(nm)
                fill_missing_required(artifact, fixable,
                                      template_getter=_tmpl_loader,
                                      prop_defaults=load_prop_defaults())
                fill_missing_required(proj, fixable,
                                      template_getter=_tmpl_loader,
                                      prop_defaults=load_prop_defaults())
            result = ctx.asset_client.validate_artifact(proj, mode="create")

        errors = result.get("errors", [])
        warnings = result.get("warnings", [])

        if result.get("valid") or not errors:
            state["validation_errors"] = []
            if warnings:
                ctx.emit("stage", "validate_pass", f"校验通过 ✓（{len(warnings)} 个警告）")
            else:
                ctx.emit("stage", "validate_pass", "校验通过 ✓")
            return

        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = errors

        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Validation failed (retry {state['retry_count']}): {result}")

        if state["retry_count"] < MAX_RETRIES:
            error_msgs = [e.get("message", str(e))[:3] if isinstance(e, dict) else str(e)[:3] for e in state["validation_errors"][:3]]
            error_msgs = [e.get("message", str(e))[:60] if isinstance(e, dict) else str(e)[:60] for e in state["validation_errors"][:3]]
            ctx.emit("stage", "validate_retry",
                     f"校验失败：{'；'.join(error_msgs)}，正在重试（第 {state['retry_count']} 次）...")
            self._step_generate(state, ctx)
            return self._step_validate(state, ctx)
        else:
            error_msgs = [e.get("message", str(e))[:60] if isinstance(e, dict) else str(e)[:60] for e in state["validation_errors"][:3]]
            ctx.emit("stage", "validate_fail",
                     f"校验失败（已达最大重试次数）：{'；'.join(error_msgs)}")


    # ── 辅助方法 ───────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        """通过 ctx.prompt_loader 渲染模板。"""
        # 有 prompt_loader 才渲染(注入 vars 变量到模板)
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render("njmind_form", name, **vars)
        # 无 prompt_loader 时返回空(正常路径不应走到这)
        logger.warning(f"No prompt_loader, returning empty for {name}")
        return ""

    def _build_parse_user_message(self, user_input: str, compressed_history: str) -> str:
        # 拼接 user message:有历史加历史段,再加当前需求
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 当前用户需求", user_input, "", "请分析并输出 JSON。"])
        return "\n".join(parts)
