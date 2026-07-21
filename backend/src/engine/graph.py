"""LangGraph StateGraph 构建 + 编排。

替代旧 engine/dispatcher.py,用 LangGraph 的 StateGraph 实现工具调度:
- classify_intent → route → execute_tool → handle_result
- interrupt/restore 追问机制
- checkpoint 自动持久化状态

Graph 结构:
  START → classify_intent ──→ execute_tool ──→ handle_result → END
              │                    ↑                │
              └─ route_by_tool ────┘                │
                                   ↑                ↓
                              (interrupt)      route_after_result
                                   │           ┌─ "done" → END
                              Command(resume) └─ "rerun" → execute_tool(重跑)
"""
import logging
from typing import Any, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from engine.graph_state import GraphState
from engine import nodes

logger = logging.getLogger(__name__)


def build_graph(
    registry: Any,
    llm_client: Any,
    asset_client: Any,
    conversation: Any = None,
    prompt_loader: Any = None,
) -> Any:
    """构建并编译 LangGraph StateGraph。

    Args:
        registry: ToolRegistry 实例
        llm_client: LLMClient 实例
        asset_client: AssetClient 实例
        conversation: ConversationManager 实例
        prompt_loader: PromptLoader 实例

    Returns:
        CompiledStateGraph (可调用 .stream() / .invoke())
    """
    # 1. 注入共享依赖到 nodes 模块
    nodes.configure(
        registry=registry,
        llm_client=llm_client,
        asset_client=asset_client,
        conversation=conversation,
        prompt_loader=prompt_loader,
    )

    # 2. 构建 StateGraph
    workflow = StateGraph(GraphState)

    # 添加节点
    workflow.add_node("classify_intent", nodes.classify_intent_node)
    workflow.add_node("execute_tool", nodes.execute_tool_node)
    workflow.add_node("handle_result", nodes.handle_result_node)

    # 添加边
    workflow.add_edge(START, "classify_intent")
    workflow.add_conditional_edges(
        "classify_intent",
        nodes.route_by_tool,
        {"tool": "execute_tool", "end": END},
    )
    workflow.add_edge("execute_tool", "handle_result")
    workflow.add_conditional_edges(
        "handle_result",
        nodes.route_after_result,
        {"rerun": "execute_tool", "done": END},
    )

    # 3. 编译(带 checkpoint)
    # InMemorySaver:状态存在内存中,重启后丢失(适合开发)
    # 生产环境可换成 SqliteSaver 或 PostgresSaver
    checkpointer = InMemorySaver()

    graph = workflow.compile(checkpointer=checkpointer)

    logger.info(
        f"LangGraph StateGraph compiled: "
        f"{len(registry.all())} tools, checkpointer=InMemorySaver"
    )

    return graph
