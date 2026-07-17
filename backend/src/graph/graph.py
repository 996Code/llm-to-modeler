"""
LangGraph Workflow

Two pipelines selected by whether source_config is present:

CREATE (source_config is None):
  fetch_guide → list_assets → parse_fields → fetch_templates → generate → validate
  (validate fail, retry<max → generate)
  (validate fail, retry>=max → done)

MODIFY (source_config provided):
  fetch_guide → modify → validate
  (validate fail, retry<max → modify)
  (validate fail, retry>=max → done)

Each node reports progress via callback → SSE → frontend.

Multi-turn conversation:
  - conversation_history is loaded from SQLite by API layer
  - If history is long (>70% of model limit), it's compressed before graph invoke
  - compressed_history is injected into LLM prompts by nodes
"""

import functools
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from langgraph.graph import END, START, StateGraph

from src.ai.compressor import (
    CompactResult,
    compact_history_sync,
    format_history_for_prompt,
    get_compression_circuit_breaker,
    should_compress,
)
from src.graph.nodes import (
    classify_intent_node,
    fetch_guide_node,
    fetch_templates_node,
    general_reply_node,
    generate_config_node,
    list_assets_node,
    modify_config_node,
    parse_fields_node,
    route_after_guide,
    route_by_intent,
    should_clarify,
    should_retry_create,
    should_retry_modify,
    validate_config_node,
)
from src.graph.state import AgentState
from src.llm.client import LLMClient
from src.llm.prompt_builder import PromptBuilder
from src.services.upstream_client import UpstreamClient

logger = logging.getLogger(__name__)


class FormConfigWorkflow:
    """Form configuration generation workflow."""

    def __init__(self, upstream: UpstreamClient, llm_client: LLMClient):
        self.upstream = upstream
        self.llm_client = llm_client
        self.prompt_builder = PromptBuilder()

    def run(
        self,
        user_input: str,
        conversation_history: Optional[list] = None,
        current_config: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> AgentState:
        """Run the unified workflow with intent classification.

        Pipeline: classify_intent → fetch_guide → (create|modify|general)
        Intent is decided by LLM, not by whether current_config is present.
        current_config is passed as source_config for the modify intent.

        Multi-turn conversation:
          - conversation_history is compressed if too long
          - compressed_history is injected into AgentState for LLM prompts
        """
        # ── Compress conversation history if needed ──
        compressed_history = self._compress_history(
            conversation_history or [],
            current_config,
        )

        workflow = self._build_graph(progress_callback)

        initial_state = AgentState(
            user_input=user_input,
            conversation_history=conversation_history or [],
            compressed_history=compressed_history,
            source_config=current_config,
            conversation_id=conversation_id,
            user_id=user_id,
        )

        logger.info(f"Starting workflow: {user_input[:100]}...")
        final_dict = workflow.invoke(initial_state)
        final_state = AgentState.from_dict(final_dict)

        field_count = 0
        if final_state.current_config:
            field_count = len(final_state.current_config.get("formFieldConfigVos", []))
        logger.info(f"Workflow done: intent={final_state.intent}, "
                    f"fields={field_count}, errors={len(final_state.validation_errors)}")
        return final_state

    def _compress_history(
        self,
        messages: List[Dict[str, str]],
        current_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compress conversation history if it's too long.

        Returns formatted history text ready for prompt injection.
        Uses circuit breaker to prevent repeated failures.
        """
        if not messages:
            return ""

        # Check if compression is needed
        model_limit = int(os.getenv("LLM_MAX_TOKENS", "200000"))
        if not should_compress(messages, model_limit=model_limit):
            # No compression needed — just format recent messages
            return format_history_for_prompt(messages, compact_result=None)

        # Check circuit breaker
        cb = get_compression_circuit_breaker()
        if cb.is_tripped():
            logger.warning("压缩熔断器已触发，跳过压缩")
            return format_history_for_prompt(messages, compact_result=None)

        # Compress
        try:
            keep_recent = int(os.getenv("COMPRESSION_KEEP_RECENT_TURNS", "3"))
            result = compact_history_sync(
                messages=messages,
                llm_client=self.llm_client,
                keep_recent=keep_recent,
                current_config=current_config,
            )
            cb.record_success()
            return format_history_for_prompt(messages, compact_result=result)
        except Exception as e:
            cb.record_failure()
            logger.error(f"压缩失败: {e}")
            # Fallback: no compression
            return format_history_for_prompt(messages, compact_result=None)

    def _build_graph(
        self,
        progress_callback: Optional[Callable] = None,
    ):
        """Build unified StateGraph with intent-driven routing.

        START → classify_intent
          ├─ general  → general_reply → END
          └─ create/modify → fetch_guide
                            ├─ modify  → modify → validate → (retry|END)
                            └─ create  → list_assets → parse_fields
                                          ├─ clarify → END
                                          └─ continue → fetch_templates → generate → validate → (retry|END)
        """
        workflow = StateGraph(AgentState)

        def emit(stage, message, **extra):
            if progress_callback:
                try:
                    progress_callback(stage, message, **extra)
                except Exception:
                    pass

        # ── Step 0: classify_intent ──
        workflow.add_node("classify_intent", functools.partial(
            classify_intent_node, llm_client=self.llm_client,
            prompt_builder=self.prompt_builder, progress=emit,
        ))

        # ── Shared nodes ──
        workflow.add_node("fetch_guide", functools.partial(
            fetch_guide_node, upstream=self.upstream, progress=emit,
        ))
        workflow.add_node("general_reply", functools.partial(
            general_reply_node, llm_client=self.llm_client, progress=emit,
        ))

        # ── CREATE nodes ──
        workflow.add_node("list_assets", functools.partial(
            list_assets_node, upstream=self.upstream, progress=emit,
        ))
        workflow.add_node("parse_fields", functools.partial(
            parse_fields_node, llm_client=self.llm_client,
            prompt_builder=self.prompt_builder, progress=emit,
        ))
        workflow.add_node("fetch_templates", functools.partial(
            fetch_templates_node, upstream=self.upstream, progress=emit,
        ))
        workflow.add_node("generate", functools.partial(
            generate_config_node, llm_client=self.llm_client,
            prompt_builder=self.prompt_builder, progress=emit,
        ))

        # ── MODIFY nodes ──
        workflow.add_node("modify", functools.partial(
            modify_config_node, llm_client=self.llm_client,
            prompt_builder=self.prompt_builder, progress=emit,
        ))

        # ── Shared validate ──
        workflow.add_node("validate", functools.partial(
            validate_config_node, upstream=self.upstream, progress=emit,
        ))

        # ── Edges ──
        workflow.add_edge(START, "classify_intent")

        # classify_intent → general_reply or fetch_guide
        workflow.add_conditional_edges(
            "classify_intent",
            route_by_intent,
            {"general_reply": "general_reply", "fetch_guide": "fetch_guide"},
        )

        # general_reply → END
        workflow.add_edge("general_reply", END)

        # fetch_guide → modify or list_assets
        workflow.add_conditional_edges(
            "fetch_guide",
            route_after_guide,
            {"modify": "modify", "list_assets": "list_assets"},
        )

        # MODIFY pipeline: modify → validate → (retry|done)
        workflow.add_edge("modify", "validate")
        workflow.add_conditional_edges(
            "validate",
            should_retry_modify,
            {"retry": "modify", "done": END},
        )

        # CREATE pipeline: list_assets → parse_fields → (clarify|continue)
        workflow.add_edge("list_assets", "parse_fields")
        workflow.add_conditional_edges(
            "parse_fields",
            should_clarify,
            {"clarify": END, "continue": "fetch_templates"},
        )
        workflow.add_edge("fetch_templates", "generate")
        workflow.add_edge("generate", "validate")
        workflow.add_conditional_edges(
            "validate",
            should_retry_create,
            {"retry": "generate", "done": END},
        )

        return workflow.compile()
