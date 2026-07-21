"""GraphState — LangGraph StateGraph 的状态定义。

替代旧 graph/state.py 的 AgentState,适配多工具插件化架构:
- 不再硬编码表单字段(guide/template_names 等)
- 工具内部状态通过 tool_state 透传,Graph 不读内部结构
- 支持 LangGraph interrupt/restore 的追问机制

状态流转:
  START → classify_intent → execute_tool → handle_result → END
                                    ↑  interrupt(追问)  ↓
                                    └─── resume ────────┘
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from sdk.tool import ToolResult


class GraphState(TypedDict, total=False):
    """LangGraph 图状态。

    TypedDict + total=False:所有字段可选,LangGraph 会按 reducer 合并。
    不用 Pydantic BaseModel:LangGraph 的 channel 机制要求 dict-like state。

    字段分组:
    ── 输入 ──
    - user_input:          用户消息
    - conversation_history: 对话历史 [{role, content}]
    - compressed_history:   压缩后的历史文本
    - conversation_id:      会话 ID(checkpoint thread_id)
    - forward_headers:      嵌入模式透传的请求头
    - current_config:       当前已有配置(modify 类工具用)

    ── 意图识别 ──
    - tool_name:            选中的工具名
    - intent_reason:        LLM 给出的判断理由

    ── 工具执行 ──
    - tool_state:           工具内部 state(透传,Graph 不读内部)
    - tool_result:          工具执行结果

    ── 追问(LangGraph interrupt) ──
    - pending_questions:    interrupt 的 value(追问问题列表)
    - clarify_answers:      resume 的 value(用户回答)

    ── SSE 事件 ──
    - sse_events:           节点产出的事件列表(由 stream.py 消费)
    """

    # ── 输入 ──
    user_input: str
    conversation_history: List[Dict[str, str]]
    compressed_history: str
    conversation_id: str
    forward_headers: Dict[str, str]
    current_config: Optional[Dict[str, Any]]

    # ── 意图识别 ──
    tool_name: str
    intent_reason: str

    # ── 工具执行 ──
    tool_state: Dict[str, Any]
    tool_result: Optional[Dict[str, Any]]  # ToolResult.model_dump()

    # ── 追问(LangGraph interrupt) ──
    pending_questions: List[Dict[str, Any]]
    clarify_answers: Dict[str, Any]

    # ── SSE 事件 ──
    sse_events: List[Dict[str, Any]]
