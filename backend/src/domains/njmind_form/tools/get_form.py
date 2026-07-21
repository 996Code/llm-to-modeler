"""GetFormTool - 查询已有表单配置的工具。

用户说"查看请假表单"或"显示XXX表单的配置"时,
从用户消息中提取 formCode,调用上游 API 查询并返回完整配置。
"""
import logging
from typing import Any, Dict, Optional

from sdk.tool import Tool, ToolResult, ToolContext

logger = logging.getLogger(__name__)


class GetFormTool(Tool):
    """查询已有表单配置。"""

    name = "get_form"
    description = "根据 formCode 查询已有表单配置"
    when = "用户想查看已有表单,如'查看请假表单'、'显示XXX表单的配置'、'获取表单详情'"

    # ── 安全声明 ──
    is_destructive = False
    is_read_only = True
    is_concurrency_safe = True
    
    # ── 插件化元数据 ──
    requires_existing_artifact = False  # 不需要已有配置,而是去查询

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的查询指令"},
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行查询:LLM 提取 formCode → 调用 API → 返回结果。"""
        user_input = state.get("user_input", "")
        
        # Step 1: LLM 提取 formCode
        ctx.emit("stage", "extract_form_code", "AI 正在识别表单标识...")
        form_code = self._extract_form_code(user_input, ctx)
        
        if not form_code:
            return ToolResult(
                error_for_llm="无法从用户消息中提取表单标识(formCode)",
                summary="查询失败:未提供表单标识",
            )
        
        # Step 2: 调用 API 查询
        ctx.emit("stage", "fetch_form", f"正在查询表单 {form_code}...")
        form_config = ctx.asset_client.get_form(form_code)
        
        if not form_config:
            return ToolResult(
                error_for_llm=f"表单 {form_code} 不存在或查询失败",
                summary=f"查询失败:表单 {form_code} 不存在",
            )
        
        # Step 3: 返回结果
        form_name = form_config.get("formName", form_code)
        field_count = len(form_config.get("formFieldConfigVos", []))
        
        ctx.emit("stage", "fetch_done", f"查询成功 ✓ 共 {field_count} 个字段")
        
        return ToolResult(
            artifact=form_config,
            summary=f"已查询到表单「{form_name}」,共 {field_count} 个字段",
            extra={
                "formatted": self.format_result(form_config),
            },
        )

    def _extract_form_code(self, user_input: str, ctx: ToolContext) -> Optional[str]:
        """用 LLM 从用户消息中提取 formCode。"""
        system_prompt = """你是表单标识提取器。从用户消息中提取 formCode(表单唯一标识)。

formCode 通常是英文或拼音组成的标识符,如:
- "qingjia_sqb" (请假申请表)
- "leave_apply" (请假申请)
- "employee_info" (员工信息表)
- "customer_form" (客户表单)

如果用户消息中包含明确的 formCode,直接提取。
如果用户只说了表单名称(如"请假表单"),尝试推断可能的 formCode。
如果无法提取,返回空字符串。

输出格式: {"formCode": "提取的标识"}
只输出 JSON,不要解释。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        try:
            result = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
            return result.get("formCode", "").strip()
        except Exception as e:
            logger.warning(f"formCode extraction failed: {e}")
            return None

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
        return f"查询的表单: {form_name} ({form_code}), 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用。"""
        return f"查询: {artifact.get('formName', '表单')}"

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从制品提取前端需要的字段。"""
        fields = artifact.get("formFieldConfigVos", [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "表单查询"),
        }
