"""CloneFormTool - 复制已有表单的工具。

用户说"复制请假表单"或"基于XXX表单创建一个副本"时,
从源表单复制配置,修改 formCode/formName,创建新表单。
"""
import logging
from typing import Any, Dict, Optional

from sdk.tool import Tool, ToolResult, ToolContext

logger = logging.getLogger(__name__)


class CloneFormTool(Tool):
    """复制已有表单配置。"""

    name = "clone_form"
    description = "基于已有表单创建副本"
    when = "用户想复制表单,如'复制请假表单'、'基于XXX创建一个副本'、'克隆表单'"

    # ── 安全声明 ──
    is_destructive = True  # 会创建新表单
    is_read_only = False
    is_concurrency_safe = False
    
    # ── 插件化元数据 ──
    requires_existing_artifact = False  # 不需要当前会话有配置,而是去查询源表单

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的复制指令"},
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行复制:LLM 提取源 formCode → 查询 → 修改标识 → 校验 → 创建。"""
        user_input = state.get("user_input", "")
        
        # Step 1: LLM 提取源 formCode 和新表单名称
        ctx.emit("stage", "extract_clone_info", "AI 正在分析复制需求...")
        clone_info = self._extract_clone_info(user_input, ctx)
        
        if not clone_info or not clone_info.get("source_form_code"):
            return ToolResult(
                error_for_llm="无法从用户消息中提取源表单标识",
                summary="复制失败:未提供源表单标识",
            )
        
        source_form_code = clone_info["source_form_code"]
        new_form_name = clone_info.get("new_form_name", "")
        new_form_code = clone_info.get("new_form_code", "")
        
        # Step 2: 查询源表单
        ctx.emit("stage", "fetch_source", f"正在查询源表单 {source_form_code}...")
        source_config = ctx.asset_client.get_form(source_form_code)
        
        if not source_config:
            return ToolResult(
                error_for_llm=f"源表单 {source_form_code} 不存在或查询失败",
                summary=f"复制失败:源表单 {source_form_code} 不存在",
            )
        
        # Step 3: 修改标识
        ctx.emit("stage", "modify_identity", "正在生成新表单标识...")
        new_config = self._modify_identity(source_config, new_form_name, new_form_code)
        
        # Step 4: 校验
        ctx.emit("stage", "validate", "正在校验新表单配置...")
        validation_result = ctx.asset_client.validate_artifact(new_config, mode="create")
        
        if not validation_result.get("valid"):
            errors = validation_result.get("errors", [])
            error_msgs = [e.get("message", str(e)) for e in errors[:3]]
            return ToolResult(
                error_for_llm=f"校验失败: {'; '.join(error_msgs)}",
                summary=f"复制失败:校验不通过",
            )
        
        # Step 5: 创建新表单
        ctx.emit("stage", "create_new", "正在创建新表单...")
        create_result = ctx.asset_client.persist_artifact(new_config, mode="create")
        
        if not create_result or not create_result.get("success"):
            return ToolResult(
                error_for_llm="创建新表单失败",
                summary="复制失败:创建新表单失败",
            )
        
        # Step 6: 返回结果
        final_form_name = new_config.get("formName", "")
        final_form_code = new_config.get("formCode", "")
        field_count = len(new_config.get("formFieldConfigVos", []))
        
        ctx.emit("stage", "clone_done", f"复制成功 ✓ 新表单 {final_form_code}")
        
        return ToolResult(
            artifact=new_config,
            summary=f"已复制表单「{final_form_name}」,共 {field_count} 个字段",
            extra={
                "formatted": self.format_result(new_config),
            },
        )

    def _extract_clone_info(self, user_input: str, ctx: ToolContext) -> Optional[dict]:
        """用 LLM 从用户消息中提取复制信息。"""
        system_prompt = """你是表单复制信息提取器。从用户消息中提取:
1. source_form_code: 源表单的 formCode
2. new_form_name: 新表单的名称(可选,如果用户没指定则留空)
3. new_form_code: 新表单的 formCode(可选,如果用户没指定则留空)

formCode 通常是英文或拼音组成的标识符。

示例:
- "复制请假表单" → {"source_form_code": "qingjia_sqb", "new_form_name": "", "new_form_code": ""}
- "基于请假表单创建一个年假申请副本" → {"source_form_code": "qingjia_sqb", "new_form_name": "年假申请表", "new_form_code": "nianjia_sqb"}

输出格式: {"source_form_code": "...", "new_form_name": "...", "new_form_code": "..."}
只输出 JSON,不要解释。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        try:
            result = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
            return {
                "source_form_code": result.get("source_form_code", "").strip(),
                "new_form_name": result.get("new_form_name", "").strip(),
                "new_form_code": result.get("new_form_code", "").strip(),
            }
        except Exception as e:
            logger.warning(f"clone info extraction failed: {e}")
            return None

    def _modify_identity(self, source_config: dict, new_form_name: str, new_form_code: str) -> dict:
        """修改表单标识,生成新表单配置。"""
        import copy
        new_config = copy.deepcopy(source_config)
        
        # 生成新的 formCode (如果用户没指定)
        if not new_form_code:
            old_code = new_config.get("formCode", "")
            new_form_code = f"{old_code}_copy"
        
        # 生成新的 formName (如果用户没指定)
        if not new_form_name:
            old_name = new_config.get("formName", "")
            new_form_name = f"{old_name}(副本)"
        
        # 更新标识
        new_config["formCode"] = new_form_code
        new_config["formName"] = new_form_name
        
        # 清除可能存在的 ID 字段(让上游生成新的)
        for key in ["id", "createTime", "updateTime", "createBy", "updateBy"]:
            new_config.pop(key, None)
        
        return new_config

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用。"""
        form_name = artifact.get("formName", "")
        form_code = artifact.get("formCode", "")
        fields = artifact.get("formFieldConfigVos", [])
        return f"复制的表单: {form_name} ({form_code}), {len(fields)} 个字段"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用。"""
        return f"复制: {artifact.get('formName', '表单')}"

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从制品提取前端需要的字段。"""
        fields = artifact.get("formFieldConfigVos", [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "表单复制"),
        }
