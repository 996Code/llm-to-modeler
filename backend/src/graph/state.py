"""
LangGraph Workflow State

Carries data through the workflow following the Skill-defined pipeline:
  fetch_guide → list_assets → parse_fields → fetch_templates → generate → validate
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ParsedField(BaseModel):
    """A field parsed from user's natural language description."""
    fieldTitleText: str = ""        # 中文名, e.g. "姓名"
    fieldTitleKey: str = ""         # 拼音蛇形, e.g. "xingming"
    formFieldType: int = 0          # type code, e.g. 0=TEXT
    fieldTypeName: str = "TEXT"     # type name, e.g. "TEXT"
    description: str = ""           # extra description from user
    options: Optional[List[str]] = None  # for SELECT types


class AgentState(BaseModel):
    """Workflow state."""

    # ── User input ──
    user_input: str = ""
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)

    # ── Intent classification (Step 0) ──
    intent: str = ""            # "create" | "modify" | "general"
    intent_reason: str = ""     # LLM 判断理由
    general_reply: str = ""     # general 意图的回复文本

    # ── Step 1: fetch_guide ──
    guide: Optional[Dict[str, Any]] = None

    # ── Step 2: list_assets ──
    template_names: List[str] = Field(default_factory=list)
    schema_names: List[str] = Field(default_factory=list)

    # ── Step 3: parse_fields (LLM) ──
    form_name: str = ""                     # parsed form name, e.g. "请假申请表"
    form_code: str = ""                     # parsed form code, e.g. "qingjia_shenqing"
    parsed_fields: List[ParsedField] = Field(default_factory=list)
    
    # ── Clarification (追问机制) ──
    needs_clarification: bool = False       # 是否需要追问用户
    clarification_questions: List[str] = Field(default_factory=list)  # 追问问题列表

    # ── Step 4: fetch_templates ──
    form_template: Optional[Dict[str, Any]] = None   # simple_form template
    field_templates: Dict[str, Dict[str, Any]] = Field(default_factory=dict)  # {typeName: template}

    # ── Step 5: generate ──
    current_config: Optional[Dict[str, Any]] = None  # final assembled FormConfig

    # ── Step 6: validate ──
    validation_errors: List[Dict[str, Any]] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3

    # ── Compressed history (set by workflow.run before graph invoke) ──
    # Formatted text: [历史摘要] + [最近对话] + [当前状态]
    # Injected into LLM prompts by nodes.
    compressed_history: str = ""

    # ── Metadata ──
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    # For modify flow
    source_config: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentState":
        return cls.model_validate(data)
