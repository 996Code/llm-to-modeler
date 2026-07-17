"""
LangGraph Workflow Nodes

Two pipelines sharing the same AgentState:

CREATE pipeline (6 steps):
  fetch_guide → list_assets → parse_fields → fetch_templates → generate → validate
  (validate fail, retry<max → back to generate)

MODIFY pipeline (3 steps):
  fetch_guide → modify → validate
  (validate fail, retry<max → back to modify)

Each node reports progress via callback → SSE → frontend.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Literal, Optional

from src.graph.state import AgentState, ParsedField
from src.llm.client import LLMClient
from src.llm.prompt_builder import PromptBuilder
from src.services.upstream_client import UpstreamClient

logger = logging.getLogger(__name__)

# type code → template filename stem
TYPE_TO_TEMPLATE = {
    0: "text", 1: "number", 2: "date", 3: "file_upload",
    4: "select", 5: "multiple_select", 6: "department", 7: "user",
    8: "auto_number", 9: "child_form", 12: "segment", 16: "rich_text",
}

TYPE_NAMES = {
    0: "TEXT", 1: "NUMBER", 2: "DATE", 3: "FILE_UPLOAD",
    4: "SELECT", 5: "MULTIPLE_SELECT", 6: "DEPARTMENT", 7: "USER",
    8: "AUTO_NUMBER", 9: "CHILD_FORM", 12: "SEGMENT", 16: "RICH_TEXT",
}


# ── Step 0: classify_intent (LLM) ────────────────────────────

def classify_intent_node(
    state: AgentState,
    llm_client: LLMClient,
    prompt_builder: PromptBuilder,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """LLM 识别用户意图：create / modify / general"""
    if progress:
        progress("classify_intent", "正在理解您的意图...")
    logger.info("Step 0: classify_intent")

    has_config = state.source_config is not None or state.current_config is not None

    system_prompt = prompt_builder.build_intent_prompt()
    user_msg = prompt_builder.build_intent_user_message(
        state.user_input,
        compressed_history=state.compressed_history,
        has_existing_config=has_config,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    parsed = llm_client.chat_json(messages)
    intent = parsed.get("intent", "create")
    reason = parsed.get("reason", "")

    # 安全兜底：没有已有配置时不允许 modify
    if intent == "modify" and not has_config:
        intent = "create"
        reason = "无已有配置，降级为 create"

    intent_label = {"create": "创建表单", "modify": "修改表单", "general": "闲聊"}.get(intent, intent)
    if progress:
        progress(
            "classify_intent_done",
            f"意图：{intent_label}",
            intent=intent,
        )
    logger.info(f"Intent: {intent} ({reason}), has_config={has_config}")

    return {"intent": intent, "intent_reason": reason}


# ── general_reply (LLM) ──────────────────────────────────────

def general_reply_node(
    state: AgentState,
    llm_client: LLMClient,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """闲聊回复（不走表单管线）"""
    if progress:
        progress("general_reply", "正在回复...")
    logger.info("Step: general_reply")

    messages = [
        {"role": "system", "content": (
            "你是低代码平台的表单配置助手。用户在和你闲聊，"
            "简短友好地回复。如果用户问你能做什么，简单介绍你可以"
            "通过自然语言生成和修改低代码表单配置。"
        )},
        {"role": "user", "content": state.user_input},
    ]

    reply = llm_client.chat(messages, temperature=0.3)
    logger.info(f"General reply: {reply[:80]}...")

    return {"general_reply": reply}


# ── Shared: fetch_guide ──────────────────────────────────────

def fetch_guide_node(
    state: AgentState,
    upstream: UpstreamClient,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """获取配置指南 (guide.json) — used by both create and modify."""
    if progress:
        progress("fetch_guide", "正在获取配置指南...")
    logger.info("Step: fetch_guide")

    guide = upstream.get_guide()
    return {"guide": guide}


# ── CREATE pipeline: list_assets ─────────────────────────────

def list_assets_node(
    state: AgentState,
    upstream: UpstreamClient,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """列出可用模板和Schema文件名（禁止猜测文件名）"""
    if progress:
        progress("list_assets", "正在获取模板和Schema列表...")
    logger.info("Step: list_assets")

    template_names = upstream.list_templates()
    schema_names = upstream.list_schemas()

    if progress:
        progress("list_assets_done", f"发现 {len(template_names)} 个模板", count=len(template_names))

    return {
        "template_names": template_names,
        "schema_names": schema_names,
    }


# ── CREATE pipeline: parse_fields (LLM) ──────────────────────

def parse_fields_node(
    state: AgentState,
    llm_client: LLMClient,
    prompt_builder: PromptBuilder,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """LLM解析自然语言→结构化字段列表，或判断是否需要追问用户"""
    if progress:
        progress("parse_fields", "AI 正在理解需求，解析字段信息...")
    logger.info("Step: parse_fields")

    # 对标 chat-bi：system prompt 只放静态规则，历史上下文注入 user message
    system_prompt = prompt_builder.build_parse_prompt(guide=state.guide)
    user_msg = prompt_builder.build_parse_user_message(
        state.user_input, 
        compressed_history=state.compressed_history
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    parsed = llm_client.chat_json(messages)

    # 检查是否需要追问
    needs_clarification = parsed.get("needsClarification", False)
    clarification_questions = parsed.get("clarificationQuestions", [])

    if needs_clarification:
        if progress:
            progress(
                "parse_fields_clarification",
                "需求不够清晰，需要追问用户",
                questions=clarification_questions,
            )
        logger.info(f"Needs clarification: {clarification_questions}")
        return {
            "needs_clarification": True,
            "clarification_questions": clarification_questions,
        }

    # 需求清晰，继续解析字段
    form_name = parsed.get("formName", "新表单")
    form_code = parsed.get("formCode", "new_form")
    raw_fields = parsed.get("fields", [])

    # Convert to ParsedField objects
    parsed_fields: List[ParsedField] = []
    for f in raw_fields:
        type_code = f.get("fieldType", 0)
        parsed_fields.append(ParsedField(
            fieldTitleText=f.get("fieldTitleText", ""),
            fieldTitleKey=f.get("fieldTitleKey", ""),
            formFieldType=type_code,
            fieldTypeName=f.get("fieldTypeName", TYPE_NAMES.get(type_code, "TEXT")),
            description=f.get("description", ""),
            options=f.get("options"),
        ))

    if progress:
        progress(
            "parse_fields_done",
            f"已解析：{form_name}，共 {len(parsed_fields)} 个字段",
            formName=form_name,
            fieldCount=len(parsed_fields),
        )

    logger.info(f"Parsed: {form_name} ({form_code}), {len(parsed_fields)} fields")

    return {
        "needs_clarification": False,
        "clarification_questions": [],
        "form_name": form_name,
        "form_code": form_code,
        "parsed_fields": parsed_fields,
    }


# ── CREATE pipeline: fetch_templates ─────────────────────────

def fetch_templates_node(
    state: AgentState,
    upstream: UpstreamClient,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """获取表单模板 + 按字段类型获取字段模板"""
    if progress:
        progress("fetch_templates", "正在获取表单和字段模板...")
    logger.info("Step: fetch_templates")

    # Get form template
    form_template = upstream.get_template("simple_form")

    # Get field templates by type (deduplicated)
    needed_types = set()
    for f in state.parsed_fields:
        needed_types.add(f.formFieldType)

    field_templates: Dict[str, Dict[str, Any]] = {}
    for type_code in needed_types:
        template_stem = TYPE_TO_TEMPLATE.get(type_code, "text")
        tmpl = upstream.get_template(f"{template_stem}_field")
        if tmpl:
            type_name = TYPE_NAMES.get(type_code, "TEXT")
            field_templates[type_name] = tmpl

    if progress:
        progress(
            "fetch_templates_done",
            f"已获取表单模板 + {len(field_templates)} 种字段模板",
            templateCount=len(field_templates),
        )

    logger.info(f"Fetched: form_template={'yes' if form_template else 'no'}, "
                f"{len(field_templates)} field templates")

    return {
        "form_template": form_template,
        "field_templates": field_templates,
    }


# ── CREATE pipeline: generate (LLM) ──────────────────────────

def generate_config_node(
    state: AgentState,
    llm_client: LLMClient,
    prompt_builder: PromptBuilder,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """LLM基于模板组装完整FormConfig"""
    is_retry = bool(state.validation_errors)

    if is_retry:
        if progress:
            progress("generate_retry", f"校验未通过，正在修复重新生成（第{state.retry_count}次）...")
    else:
        if progress:
            progress("generate", "AI 正在基于模板组装表单配置...")

    logger.info(f"Step: generate (retry={is_retry}, count={state.retry_count})")

    # Build prompt with templates (system prompt 只放静态规则)
    system_prompt = prompt_builder.build_generate_prompt(
        form_template=state.form_template or {},
        field_templates=state.field_templates,
        guide=state.guide,
    )

    # Build user message with parsed fields
    fields_data = {
        "formName": state.form_name,
        "formCode": state.form_code,
        "fields": [
            {
                "fieldTitleText": f.fieldTitleText,
                "fieldTitleKey": f.fieldTitleKey,
                "fieldType": f.formFieldType,
                "fieldTypeName": f.fieldTypeName,
                **({"options": f.options} if f.options else {}),
            }
            for f in state.parsed_fields
        ],
    }

    # 对标 chat-bi：历史上下文注入 user message
    user_parts = []
    if state.compressed_history:
        user_parts.extend([
            "## 对话历史",
            state.compressed_history,
            "",
        ])

    if is_retry and state.current_config:
        # Retry: include validation errors
        error_msgs = [e.get("message", str(e)) for e in state.validation_errors[:5]]
        user_parts.extend([
            "## 校验失败，请修复",
            "\n".join(f"- {m}" for m in error_msgs),
            "",
            "## 当前配置",
            f"```json\n{json.dumps(state.current_config, ensure_ascii=False)}\n```",
            "请修复后输出完整配置。",
        ])
    else:
        user_parts.extend([
            "## 字段信息",
            f"```json\n{json.dumps(fields_data, ensure_ascii=False, indent=2)}\n```",
            "",
            "请根据以上字段信息和模板，组装完整的表单配置 JSON。",
        ])

    user_msg = "\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    config = llm_client.chat_json(messages)

    field_count = len(config.get("formFieldConfigVos", []))
    if progress:
        progress("generate_done", f"已生成配置：{config.get('formName', '')}，{field_count} 个字段")

    logger.info(f"Generated: {field_count} fields, formCode={config.get('formCode')}")

    return {"current_config": config, "validation_errors": []}


# ── MODIFY pipeline: modify (LLM) ────────────────────────────

def modify_config_node(
    state: AgentState,
    llm_client: LLMClient,
    prompt_builder: PromptBuilder,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """LLM基于自然语言指令修改现有FormConfig"""
    is_retry = bool(state.validation_errors)

    if is_retry:
        if progress:
            progress("generate_retry", f"校验未通过，正在修复（第{state.retry_count}次）...")
    else:
        if progress:
            progress("generate", "AI 正在根据指令修改配置...")

    logger.info(f"Step: modify (retry={is_retry}, count={state.retry_count})")

    # The base config to modify — from current_config (retry) or source_config (first)
    base_config = state.current_config if is_retry else state.source_config
    if not base_config:
        logger.error("modify_config_node: no base config to modify!")
        return {"current_config": state.source_config, "validation_errors": []}

    # Build prompt (system prompt 只放静态规则)
    system_prompt = prompt_builder.build_modify_prompt(
        config=base_config,
        guide=state.guide,
    )

    # 对标 chat-bi：历史上下文注入 user message
    user_parts = []
    if state.compressed_history:
        user_parts.extend([
            "## 对话历史",
            state.compressed_history,
            "",
        ])

    if is_retry:
        error_msgs = [e.get("message", str(e)) for e in state.validation_errors[:5]]
        user_parts.extend([
            "## 原始修改指令",
            state.user_input,
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
            state.user_input,
            "",
            "请根据指令修改上面的配置，输出修改后的完整 JSON。",
        ])

    user_msg = "\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    config = llm_client.chat_json(messages)

    field_count = len(config.get("formFieldConfigVos", []))
    if progress:
        progress("generate_done", f"已修改配置：{config.get('formName', '')}，{field_count} 个字段")

    logger.info(f"Modified: {field_count} fields, formCode={config.get('formCode')}")

    return {"current_config": config, "validation_errors": []}


# ── Shared: validate ─────────────────────────────────────────

def validate_config_node(
    state: AgentState,
    upstream: UpstreamClient,
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """提交上游校验"""
    if progress:
        progress("validate", "正在提交平台校验...")
    logger.info("Step: validate")

    if not state.current_config:
        return {
            "validation_errors": [{"message": "No configuration to validate"}],
            "retry_count": state.retry_count + 1,
        }

    mode = "UPDATE" if state.source_config else "CREATE"
    result = upstream.validate_form(state.current_config, mode=mode)

    if result.get("valid"):
        if progress:
            progress("validate_pass", "校验通过 ✓")
        logger.info("Validation passed")
        return {"validation_errors": []}
    else:
        errors = result.get("errors", [])
        error_msgs = [e.get("message", str(e)) for e in errors[:3]]
        if progress:
            progress("validate_fail", f"校验发现 {len(errors)} 个问题: {'; '.join(error_msgs)}")
        logger.warning(f"Validation failed: {len(errors)} errors")
        return {
            "validation_errors": errors,
            "retry_count": state.retry_count + 1,
        }


# ── Conditional edges ────────────────────────────────────────

def route_by_intent(state: AgentState) -> Literal["fetch_guide", "general_reply"]:
    """classify_intent 之后的路由：general → 闲聊，否则 → 获取指南进入表单管线"""
    if state.intent == "general":
        return "general_reply"
    return "fetch_guide"


def route_after_guide(state: AgentState) -> Literal["list_assets", "modify"]:
    """fetch_guide 之后的路由：modify → 修改管线，否则 → 创建管线"""
    if state.intent == "modify":
        return "modify"
    return "list_assets"


def should_clarify(state: AgentState) -> Literal["clarify", "continue"]:
    """Check if we need to ask user for clarification."""
    if state.needs_clarification:
        return "clarify"
    return "continue"


def should_retry_create(state: AgentState) -> Literal["retry", "done"]:
    """Create pipeline: retry generate on validation failure."""
    if state.validation_errors and state.retry_count <= state.max_retries:
        return "retry"
    return "done"


def should_retry_modify(state: AgentState) -> Literal["retry", "done"]:
    """Modify pipeline: retry modify on validation failure."""
    if state.validation_errors and state.retry_count <= state.max_retries:
        return "retry"
    return "done"
