"""ModifyFormTool - 修改已有表单的复合工具(3 步管线)。

把现有 nodes.py 的 3 步 MODIFY 管线搬进 CompositeTool:
  fetch_guide -> modify -> validate

从 state["source_artifact"](已有配置)出发,保留原有字段。
"""
import json
import logging
from typing import Any, Dict

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_form.tools._config_loader import load_type_mappings

logger = logging.getLogger(__name__)

_TYPE_TO_TEMPLATE, _TYPE_NAMES = load_type_mappings()

MAX_RETRIES = 3


class ModifyFormTool(CompositeTool):
    """根据自然语言指令修改已有 njmind 表单配置。"""

    name = "modify_form"
    description = "修改已有表单配置(加/删/改字段)"
    when = "用户想修改已有表单,如'加一个手机号字段'、'删除xxx'、'把xxx改成必填'"

    is_destructive = True
    is_read_only = False
    is_concurrency_safe = False

    steps = ["fetch_guide", "modify", "validate"]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的修改指令"},
                "source_artifact": {"type": "object", "description": "已有的表单配置"},
            },
            "required": ["user_input", "source_artifact"],
        }

    def validate_input(self, state: dict) -> str | None:
        """语义校验:modify 必须有 source_artifact。"""
        if not state.get("source_artifact"):
            return "修改表单需要已有配置(source_artifact),但当前没有"
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行 3 步管线。"""
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])

        self.run_pipeline(state, ctx)

        artifact = state.get("artifact")
        if artifact:
            form_name = artifact.get("formName", "")
            field_count = len(artifact.get("formFieldConfigVos", []))
            summary = f"已修改「{form_name}」,共 {field_count} 个字段"
        else:
            summary = "表单修改未完成"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            extra={"validation_errors": state.get("validation_errors", [])},
        )

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用。"""
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

    def _step_modify(self, state: dict, ctx: ToolContext) -> None:
        """LLM 基于指令修改现有 FormConfig。"""
        is_retry = bool(state.get("validation_errors"))

        # 基础配置:retry 用 artifact,首次用 source_artifact
        base_config = state.get("artifact") if is_retry else state.get("source_artifact")
        if not base_config:
            logger.error("ModifyFormTool: no base config to modify!")
            state["artifact"] = state.get("source_artifact")
            return

        # 渲染 prompt
        system_prompt = self._render_prompt(
            ctx, "modify",
            config=base_config,
            guide=state.get("guide") or {},
        )

        # 构建 user message
        user_parts = []
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])

        if is_retry:
            error_msgs = [
                e.get("message", str(e))
                for e in state.get("validation_errors", [])[:5]
            ]
            user_parts.extend([
                "## 原始修改指令",
                state.get("user_input", ""),
                "",
                "## 校验失败，请修复",
                "\n".join(f"- {m}" for m in error_msgs),
                "",
                "## 当前配置",
                f"```json\n{json.dumps(base_config, ensure_ascii=False)}\n```",
                "请修复后输出完整配置。",
            ])
        else:
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

        config = ctx.llm_client.chat_json(messages)
        state["artifact"] = config
        state["validation_errors"] = []

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        """提交上游校验。失败时工具内部 retry(重跑 modify)。"""
        artifact = state.get("artifact")
        if not artifact:
            state["validation_errors"] = [{"message": "No configuration to validate"}]
            return

        result = ctx.asset_client.validate_artifact(artifact, mode="update")

        if result.get("valid"):
            state["validation_errors"] = []
            return

        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = result.get("errors", [])

        if state["retry_count"] < MAX_RETRIES:
            ctx.emit("stage", "validate_retry",
                     message=f"校验失败,第 {state['retry_count']} 次重试")
            self._step_modify(state, ctx)
            return self._step_validate(state, ctx)

    # ── 辅助方法 ───────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        """通过 ctx.prompt_loader 渲染模板,兜底用 PromptBuilder。"""
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render("njmind_form", name, **vars)
        from src.llm.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        if name == "modify":
            return pb.build_modify_prompt(
                config=vars.get("config") or {},
                guide=vars.get("guide"),
            )
        return ""
