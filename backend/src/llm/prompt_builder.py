"""
Prompt Builder

Two LLM prompts:
1. parse_fields: NL → structured field list (formName, formCode, fields[])
2. generate: structured field list + templates → complete FormConfig JSON

The assembly (deep copy template + replace) happens in Python, not LLM.
LLM only does understanding (parse) — code does the deterministic assembly.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FIELD_TYPE_TABLE = """| 代码 | 类型 | 适用场景 |
|------|------|---------|
| 0 | TEXT | 姓名、手机号、邮箱、地址、编号、身份证 |
| 1 | NUMBER | 金额、数量、年龄、百分比、时长 |
| 2 | DATE | 日期、时间 |
| 3 | FILE_UPLOAD | 附件、文件、图片 |
| 4 | SELECT | 下拉单选（需提供选项） |
| 5 | MULTIPLE_SELECT | 多选、标签 |
| 6 | DEPARTMENT | 部门选择 |
| 7 | USER | 人员、审批人、负责人 |
| 8 | AUTO_NUMBER | 自动编号、流水号 |
| 9 | CHILD_FORM | 子表单、明细表 |
| 12 | SEGMENT | 分组、分隔 |
| 16 | RICH_TEXT | 富文本、长内容 |"""


class PromptBuilder:
    """Builds prompts for the LLM calls."""

    # ── Intent classification ──────────────────────────────────

    def build_intent_prompt(self) -> str:
        """
        System prompt for classify_intent step (静态规则)。
        对标 chat-bi 的 classify_intent：判断用户意图后路由到不同管线。
        """
        return "\n".join([
            "你是低代码平台的意图识别器。判断用户消息的意图，只返回 JSON。",
            "",
            "3 种意图：",
            '- "create": 创建新表单。如"创建一个请假表"、"新建一个客户信息表"、"做一个表单"。',
            '- "modify": 修改已有表单。如"加一个字段"、"删除xxx"、"把xxx改成必填"、"修改一下"。'
            "只有当已有表单配置时才可用。",
            '- "general": 闲聊/打招呼，不涉及表单操作。如"你好"、"谢谢"、"你是谁"。',
            "",
            "核心规则：",
            '1. 如果没有已有表单配置（has_existing_config=false），用户说"修改/加字段"也是 create（因为没东西可改）。',
            '2. 如果用户意图不明确，默认 create（让后续 parse_fields 节点决定是否追问）。',
            "3. 只返回 JSON，不要解释。",
            "",
            '输出格式：{"intent": "create|modify|general", "reason": "简短理由"}',
        ])

    def build_intent_user_message(
        self,
        user_input: str,
        compressed_history: str = "",
        has_existing_config: bool = False,
    ) -> str:
        """
        Build user message for classify_intent step.
        历史上下文 + 是否有已有配置 + 当前消息，注入到 user message。
        """
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])

        parts.extend([
            f"## 是否有已有表单配置：{'是' if has_existing_config else '否'}",
            "",
            "## 用户消息",
            user_input,
            "",
            "请判断意图并输出 JSON。",
        ])
        return "\n".join(parts)

    def build_parse_prompt(
        self,
        guide: Optional[Dict] = None,
    ) -> str:
        """
        System prompt for parse_fields step (静态规则，不含历史上下文)。
        LLM extracts structured field info from natural language.

        对标 chat-bi 的设计：
        - system prompt 只放静态规则
        - 历史上下文通过 user message 注入（build_parse_user_message）
        - 追问场景：需求模糊时追问，但用户回复"你定"时基于表单类型推断

        Args:
            guide: Guide configuration from upstream
            compressed_history: Compressed conversation history (if any)
        """
        parts = [
            "你是低代码平台的表单需求分析器。",
            "",
            "## 任务",
            "分析用户的自然语言描述，判断需求是否清晰，然后提取表单名称和字段列表。",
            "",
            "## 需求清晰度判断",
            "以下情况视为需求清晰（needsClarification=false）：",
            "- 用户明确列出了字段名称和类型",
            "- 用户给出了表单名称和大致用途（如'请假申请表'），即使没列具体字段",
            "- 用户在追问中回复了补充信息（如'姓名、部门、手机号'）",
            "- 用户回复模糊（如'你定'、'随便'、'都行'），但结合对话历史能推断出表单类型",
            "",
            "以下情况视为需求模糊（needsClarification=true）：",
            "- 用户只说'创建一个表单'，没有表单名称也没有任何字段信息",
            "- 用户说了表单名称但完全无法推断应该包含什么字段",
            "",
            "## 输出格式（JSON）",
            "```json",
            """{
  "needsClarification": false,
  "clarificationQuestions": [],
  "formName": "表单中文名称",
  "formCode": "拼音蛇形编码（如 qingjia_shenqing）",
  "fields": [
    {
      "fieldTitleText": "字段中文名",
      "fieldTitleKey": "拼音蛇形（如 xingming）",
      "fieldType": 0,
      "fieldTypeName": "TEXT",
      "options": ["选项1", "选项2"]
    }
  ]
}""",
            "```",
            "",
            "## 字段类型对照表",
            FIELD_TYPE_TABLE,
            "",
            "## 关键规则",
            "- 手机号/电话/邮箱/身份证 → TEXT(0)，不是 NUMBER",
            "- fieldTitleKey 用拼音全拼蛇形（姓名→xingming，请假类型→qingjialeixing）",
            "- SELECT(4) 类型必须提供 options 数组",
            "- 如果用户明确说「下拉」「选择」，用 SELECT(4)；「多选」用 MULTIPLE_SELECT(5)",
            "- 「金额」「价格」→NUMBER(1)，「日期」→DATE(2)，「附件」「文件」→FILE_UPLOAD(3)",
            "- 「部门」→DEPARTMENT(6)，「人员」「审批人」→USER(7)",
            "- 无法判断字段类型时，设置 needsClarification=true 并提问，不要猜测",
            "- 如果用户回复模糊（如'你定'、'随便'、'都行'），结合对话历史推断表单类型，生成常见字段，不要追问",
            "- 只输出 JSON，不输出其他内容",
        ]

        # Add keyword hints from guide
        if guide and guide.get("keywordIndex"):
            idx = guide["keywordIndex"]
            hints = []
            for kw, ftypes in list(idx.items())[:30]:
                if isinstance(ftypes, list):
                    hints.append(f"  '{kw}' → {', '.join(ftypes)}")
                else:
                    hints.append(f"  '{kw}' → {ftypes}")
            parts.append("")
            parts.append("## 关键词映射参考")
            parts.extend(hints)

        return "\n".join(parts)

    def build_parse_user_message(self, user_input: str, compressed_history: str = "") -> str:
        """
        Build user message for parse_fields step.
        
        对标 chat-bi 的设计：历史上下文注入到 user message 中，而不是 system prompt。
        这样 system prompt 保持静态，user message 包含动态上下文。
        
        Args:
            user_input: 用户当前输入
            compressed_history: 压缩后的对话历史（可选）
        """
        parts = []
        
        # 如果有历史，先注入历史上下文
        if compressed_history:
            parts.extend([
                "## 对话历史",
                compressed_history,
                "",
            ])
        
        # 然后是当前用户需求
        parts.extend([
            "## 当前用户需求",
            user_input,
            "",
            "请分析并输出 JSON。",
        ])
        
        return "\n".join(parts)

    def build_generate_prompt(
        self,
        form_template: Dict[str, Any],
        field_templates: Dict[str, Dict[str, Any]],
        guide: Optional[Dict] = None,
    ) -> str:
        """
        System prompt for generate step (静态规则，不含历史上下文)。
        LLM assembles complete FormConfig using templates as base.

        Args:
            form_template: Form template from upstream
            field_templates: Field templates by type
            guide: Guide configuration from upstream
        """
        parts = [
            "你是低代码平台的表单配置组装器。",
            "",
            "## 任务",
            "根据已解析的字段列表和模板，组装出完整的表单配置 JSON。",
            "",
            "## 组装规则",
            "1. 以表单模板为基础，deep copy 后修改",
            "2. 替换表单级字段：formCode、formName、titleFieldKey、formTitle（格式 $fieldKey$）",
            "3. 用字段模板替换 formFieldConfigVos 数组",
            "4. 每个字段复制对应类型的模板，修改 fieldTitleKey、fieldTitleText、formFieldType",
            "5. SELECT 类型需要添加 optionSettings",
            "6. 保留模板中的所有系统字段（isShowFieldAdd、isShowFieldDetail、isEditField 等）",
            "7. fieldConditionDisplays 必须为 []",
            "8. 只输出完整 JSON",
            "",
            "## 表单模板",
            "```json",
            json.dumps(form_template, indent=2, ensure_ascii=False),
            "```",
        ]

        # Add field templates
        if field_templates:
            parts.append("")
            parts.append("## 字段模板（按类型选择使用）")
            for type_name, tmpl in field_templates.items():
                parts.append(f"### {type_name}")
                parts.append("```json")
                parts.append(json.dumps(tmpl, indent=2, ensure_ascii=False))
                parts.append("```")

        parts.append("")
        parts.append("## 字段类型代码")
        parts.append(FIELD_TYPE_TABLE)

        return "\n".join(parts)

    def build_modify_prompt(
        self,
        config: Dict[str, Any],
        guide: Optional[Dict] = None,
    ) -> str:
        """
        System prompt for modify step (静态规则，不含历史上下文)。
        LLM modifies an existing FormConfig based on natural language instruction.

        Args:
            config: Current form configuration to modify
            guide: Guide configuration from upstream
        """
        parts = [
            "你是低代码平台的表单配置修改器。",
            "",
            "## 任务",
            "根据用户的修改指令，修改现有的表单配置 JSON。",
            "输出修改后的完整 JSON（包含所有字段，不要省略未修改的部分）。",
            "",
            "## 修改规则",
            "1. 保留原有表单结构，只修改指令涉及的部分",
            "2. 新增字段时参照已有字段的结构",
            "3. 删除字段时从 formFieldConfigVos 数组中移除",
            "4. 修改字段类型时同步更新 formFieldType 和 fieldTypeName",
            "5. 修改字段名时同步更新 fieldTitleText 和 fieldTitleKey",
            "6. 不要改动 formCode、topButtons、bottomButtons 等系统字段（除非指令明确要求）",
            "7. fieldConditionDisplays 必须为 []",
            "8. 只输出修改后的完整 JSON",
            "",
            "## 字段类型代码",
            FIELD_TYPE_TABLE,
            "",
            "## 当前配置",
            "```json",
            json.dumps(config, indent=2, ensure_ascii=False),
            "```",
        ]

        if guide and guide.get("keywordIndex"):
            idx = guide["keywordIndex"]
            hints = []
            for kw, ftypes in list(idx.items())[:20]:
                if isinstance(ftypes, list):
                    hints.append(f"  '{kw}' → {', '.join(ftypes)}")
                else:
                    hints.append(f"  '{kw}' → {ftypes}")
            parts.append("")
            parts.append("## 关键词映射参考")
            parts.extend(hints)

        return "\n".join(parts)
