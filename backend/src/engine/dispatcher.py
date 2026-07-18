"""ToolDispatcher — 工具调度器。

阶段 0:空壳,不实现调度逻辑(阶段 3 实现)。
本阶段只声明类存在,Engine 模块可被 import。
"""
from typing import Any


class ToolDispatcher:
    """阶段 0 占位。阶段 3 实现 _select_tools / _partition_tool_calls /
    _run_single / _run_concurrent / _resume_ask 等调度逻辑。"""

    def __init__(self, registry: Any = None, conversation: Any = None,
                 llm_client: Any = None):
        self._registry = registry
        self._conversation = conversation
        self._llm_client = llm_client
        self._max_clarify_rounds = 3
