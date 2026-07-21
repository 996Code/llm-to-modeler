"""ImageFormTool - 图片识别生成表单的工具。

用户上传图片(如手绘草图、截图),通过多模态 LLM 分析图片中的表单样式,
生成对应的表单配置。
"""
import base64
import logging
from typing import Any, Dict, Optional

from sdk.tool import Tool, ToolResult, ToolContext

logger = logging.getLogger(__name__)


class ImageFormTool(Tool):
    """通过图片识别生成表单配置。"""

    name = "image_form"
    description = "根据图片(手绘草图/截图)生成表单配置"
    when = "用户上传图片并想生成表单,如'根据这张图生成表单'、'把这个草图变成表单'"

    # ── 安全声明 ──
    is_destructive = False  # 只生成配置,不直接创建
    is_read_only = True
    is_concurrency_safe = True
    
    # ── 插件化元数据 ──
    requires_existing_artifact = False

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的描述(可选)"},
                "image_url": {"type": "string", "description": "图片 URL"},
                "image_base64": {"type": "string", "description": "Base64 编码的图片"},
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行图片识别:分析图片 → 提取字段 → 生成配置 → 校验。"""
        user_input = state.get("user_input", "")
        image_url = state.get("image_url")
        image_base64 = state.get("image_base64")
        
        if not image_url and not image_base64:
            return ToolResult(
                error_for_llm="未提供图片,请上传图片或提供图片 URL",
                summary="生成失败:未提供图片",
            )
        
        # Step 1: 多模态 LLM 分析图片
        ctx.emit("stage", "analyze_image", "AI 正在分析图片中的表单样式...")
        analysis = self._analyze_image(user_input, image_url, image_base64, ctx)
        
        if not analysis:
            return ToolResult(
                error_for_llm="图片分析失败,无法识别表单内容",
                summary="生成失败:图片分析失败",
            )
        
        # Step 2: 生成表单配置
        ctx.emit("stage", "generate_config", "正在生成表单配置...")
        form_config = self._generate_config(analysis, ctx)
        
        if not form_config:
            return ToolResult(
                error_for_llm="配置生成失败",
                summary="生成失败:配置生成失败",
            )
        
        # Step 3: 校验
        ctx.emit("stage", "validate", "正在校验表单配置...")
        validation_result = ctx.asset_client.validate_artifact(form_config, mode="create")
        
        if not validation_result.get("valid"):
            errors = validation_result.get("errors", [])
            error_msgs = [e.get("message", str(e)) for e in errors[:3]]
            ctx.emit("stage", "validate_fail", f"校验失败: {'; '.join(error_msgs)}")
            # 校验失败但仍然返回配置,让用户看到并决定
            return ToolResult(
                artifact=form_config,
                summary=f"已生成配置,但校验失败: {'; '.join(error_msgs)}",
                extra={
                    "validation_errors": errors,
                    "formatted": self.format_result(form_config),
                },
            )
        
        # Step 4: 返回结果
        form_name = form_config.get("formName", "图片识别表单")
        field_count = len(form_config.get("formFieldConfigVos", []))
        
        ctx.emit("stage", "generate_done", f"生成成功 ✓ 共 {field_count} 个字段")
        
        return ToolResult(
            artifact=form_config,
            summary=f"已根据图片生成表单「{form_name}」,共 {field_count} 个字段",
            extra={
                "formatted": self.format_result(form_config),
            },
        )

    def _analyze_image(
        self,
        user_input: str,
        image_url: Optional[str],
        image_base64: Optional[str],
        ctx: ToolContext,
    ) -> Optional[dict]:
        """用多模态 LLM 分析图片,提取表单信息。"""
        system_prompt = """你是表单图片分析器。分析图片中的表单样式,提取以下信息:

1. formName: 表单名称(从图片标题或内容推断)
2. fields: 字段列表,每个字段包含:
   - fieldTitleText: 字段标题(中文)
   - fieldType: 字段类型(0:单行文本, 1:多行文本, 2:数字, 3:日期, 4:下拉选择, 5:单选, 6:多选, 7:人员选择)
   - isRequired: 是否必填(布尔值)
   - options: 选项列表(仅下拉/单选/多选需要)

分析规则:
- 从图片中的文字、布局、控件样式推断字段类型
- 如果有"姓名"、"电话"等,推断为单行文本
- 如果有"日期"、"时间"等,推断为日期类型
- 如果有"是/否"、"男/女"等,推断为单选
- 如果有多个选项并列,推断为多选或下拉
- 如果有星号(*)标记,推断为必填

输出格式:
{
  "formName": "表单名称",
  "fields": [
    {
      "fieldTitleText": "字段标题",
      "fieldType": 0,
      "isRequired": true,
      "options": ["选项1", "选项2"]  // 仅下拉/单选/多选需要
    }
  ]
}

只输出 JSON,不要解释。"""

        # 构建多模态消息
        messages = [
            {"role": "system", "content": system_prompt},
        ]
        
        # 添加用户文本(如果有)
        if user_input:
            messages.append({"role": "user", "content": user_input})
        
        # 添加图片
        if image_url:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析这张图片中的表单:"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            })
        elif image_base64:
            # 假设是 PNG,如果不是可以后续调整
            data_url = f"data:image/png;base64,{image_base64}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析这张图片中的表单:"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            })
        
        try:
            # 调用多模态 LLM
            result = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
            return result
        except Exception as e:
            logger.warning(f"image analysis failed: {e}")
            return None

    def _generate_config(self, analysis: dict, ctx: ToolContext) -> Optional[dict]:
        """根据分析结果生成表单配置。"""
        # 获取指南和模板
        guide = ctx.asset_client.get_guide()
        templates = ctx.asset_client.list_templates()
        
        # 构建配置生成 prompt
        system_prompt = """你是表单配置生成器。根据分析结果生成符合 njmind 低码平台规范的表单配置 JSON。

配置结构:
{
  "formCode": "表单标识(英文/拼音)",
  "formName": "表单名称",
  "formFieldConfigVos": [
    {
      "fieldTitleText": "字段标题",
      "formFieldType": 0,  // 字段类型
      "isRequired": 1,     // 1=必填, 0=非必填
      "optionSettings": [  // 仅下拉/单选/多选需要
        {"optionName": "选项1", "optionValue": "1"}
      ]
    }
  ]
}

字段类型映射:
- 0: 单行文本
- 1: 多行文本
- 2: 数字
- 3: 日期
- 4: 下拉选择
- 5: 单选
- 6: 多选
- 7: 人员选择

生成规则:
- formCode 使用拼音或英文,如 "qingjia_sqb"
- 每个字段必须有 fieldTitleText 和 formFieldType
- 必填字段 isRequired=1
- 下拉/单选/多选必须有 optionSettings

只输出 JSON,不要解释。"""

        user_content = f"""分析结果:
{analysis}

请根据以上分析生成完整的表单配置 JSON。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            config = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
            return config
        except Exception as e:
            logger.warning(f"config generation failed: {e}")
            return None

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用。"""
        form_name = artifact.get("formName", "")
        fields = artifact.get("formFieldConfigVos", [])
        field_summary = ", ".join(
            f.get("fieldTitleText", "") for f in fields[:10]
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"图片生成的表单: {form_name}, 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用。"""
        return f"图片生成: {artifact.get('formName', '表单')}"

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从制品提取前端需要的字段。"""
        fields = artifact.get("formFieldConfigVos", [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "图片生成表单"),
        }
