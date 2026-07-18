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

logger = logging.getLogger(__name__)

# 加载类型映射(从 config.yaml)
_TYPE_TO_TEMPLATE, _TYPE_NAMES = load_type_mappings()

MAX_RETRIES = 3


class CreateFormTool(CompositeTool):
    """根据自然语言需求生成 njmind 表单配置。"""

    name = "create_form"
    description = "根据自然语言需求生成 njmind 表单配置"
    when = "用户想新建表单时,如'创建一个请假表'、'新建客户信息表'"

    # 写操作:不可并发
    is_destructive = True
    is_read_only = False
    is_concurrency_safe = False

    steps = ["fetch_guide", "list_assets", "parse_fields",
             "fetch_templates", "generate", "validate"]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的自然语言需求"}
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行 6 步管线。retry 在 _step_validate 内部处理。"""
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])

        self.run_pipeline(state, ctx)

        artifact = state.get("artifact")
        if artifact:
            form_name = artifact.get("formName", "")
            field_count = len(artifact.get("formFieldConfigVos", []))
            summary = f"已生成「{form_name}」,共 {field_count} 个字段"
        else:
            summary = "表单生成未完成"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            extra={"validation_errors": state.get("validation_errors", [])},
        )

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿。"""
        form_name = artifact.get("formName", "")
        form_code = artifact.get("formCode", "")
        fields = artifact.get("formFieldConfigVos", [])
        field_summary = ", ".join(
            f.get("fieldTitleText", "") for f in fields[:10]
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"当前表单: {form_name} ({form_code}), 字段: {field_summary}"

    # ── Steps ──────────────────────────────────────────────────

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        """获取配置指南。"""
        state["guide"] = ctx.asset_client.get_guide()

    def _step_list_assets(self, state: dict, ctx: ToolContext) -> None:
        """列出可用模板和 Schema 文件名。"""
        state["template_names"] = ctx.asset_client.list_templates()

    def _step_parse_fields(self, state: dict, ctx: ToolContext) -> None:
        """LLM 解析自然语言 -> 结构化字段列表。"""
        user_input = state.get("user_input", "")
        compressed_history = state.get("compressed_history", "")
        guide = state.get("guide") or {}

        # 渲染 prompt
        system_prompt = self._render_prompt(ctx, "parse", guide=guide)
        user_msg = self._build_parse_user_message(user_input, compressed_history)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        parsed = ctx.llm_client.chat_json(messages)

        # 检查是否需要追问
        if parsed.get("needsClarification"):
            questions = parsed.get("clarificationQuestions", [])
            raise ClarificationRaised(questions)

        # 解析字段
        state["form_name"] = parsed.get("formName", "新表单")
        state["form_code"] = parsed.get("formCode", "new_form")

        raw_fields = parsed.get("fields", [])
        parsed_fields = []
        for f in raw_fields:
            type_code = f.get("fieldType", 0)
            parsed_fields.append(ParsedField(
                fieldTitleText=f.get("fieldTitleText", ""),
                fieldTitleKey=f.get("fieldTitleKey", ""),
                formFieldType=type_code,
                fieldTypeName=f.get("fieldTypeName", _TYPE_NAMES.get(type_code, "TEXT")),
                description=f.get("description", ""),
                options=f.get("options"),
            ))
        state["parsed_fields"] = parsed_fields

    def _step_fetch_templates(self, state: dict, ctx: ToolContext) -> None:
        """获取表单模板 + 按字段类型获取字段模板。"""
        # 表单模板
        state["form_template"] = ctx.asset_client.get_template("simple_form")

        # 字段模板(按类型去重)
        needed_types = set()
        for f in state.get("parsed_fields", []):
            needed_types.add(f.formFieldType)

        field_templates = {}
        for type_code in needed_types:
            template_stem = _TYPE_TO_TEMPLATE.get(type_code, "text")
            tmpl = ctx.asset_client.get_template(f"{template_stem}_field")
            if tmpl:
                type_name = _TYPE_NAMES.get(type_code, "TEXT")
                field_templates[type_name] = tmpl
        state["field_templates"] = field_templates

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        """LLM 基于模板组装完整 FormConfig。"""
        is_retry = bool(state.get("validation_errors"))

        # 渲染 prompt
        system_prompt = self._render_prompt(
            ctx, "generate",
            form_template=state.get("form_template") or {},
            field_templates=state.get("field_templates") or {},
        )

        # 构建 user message
        fields_data = {
            "formName": state.get("form_name", ""),
            "formCode": state.get("form_code", ""),
            "fields": [
                {
                    "fieldTitleText": f.fieldTitleText,
                    "fieldTitleKey": f.fieldTitleKey,
                    "fieldType": f.formFieldType,
                    "fieldTypeName": f.fieldTypeName,
                    **({"options": f.options} if f.options else {}),
                }
                for f in state.get("parsed_fields", [])
            ],
        }

        user_parts = []
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])

        if is_retry and state.get("artifact"):
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

        config = ctx.llm_client.chat_json(messages)
        state["artifact"] = config
        state["validation_errors"] = []  # 重置

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        """提交上游校验。失败时工具内部 retry(重跑 generate)。"""
        artifact = state.get("artifact")
        if not artifact:
            state["validation_errors"] = [{"message": "No configuration to validate"}]
            return

        result = ctx.asset_client.validate_artifact(artifact, mode="create")

        if result.get("valid"):
            state["validation_errors"] = []
            return

        # 校验失败 -> 工具内部 retry
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = result.get("errors", [])

        if state["retry_count"] < MAX_RETRIES:
            ctx.emit("stage", "validate_retry",
                     message=f"校验失败,第 {state['retry_count']} 次重试")
            self._step_generate(state, ctx)  # 重跑前序 step
            return self._step_validate(state, ctx)  # 递归再校验
        # 超过 max_retries -> 错误留在 state,execute 返回时带 extra

    # ── 辅助方法 ───────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        """通过 ctx.prompt_loader 渲染模板,兜底用 PromptBuilder。"""
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render("njmind_form", name, **vars)
        # 兜底:用旧 PromptBuilder(阶段 3 过渡期)
        from src.llm.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        if name == "parse":
            return pb.build_parse_prompt(guide=vars.get("guide"))
        elif name == "generate":
            return pb.build_generate_prompt(
                form_template=vars.get("form_template") or {},
                field_templates=vars.get("field_templates") or {},
            )
        return ""

    def _build_parse_user_message(self, user_input: str, compressed_history: str) -> str:
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 当前用户需求", user_input, "", "请分析并输出 JSON。"])
        return "\n".join(parts)
