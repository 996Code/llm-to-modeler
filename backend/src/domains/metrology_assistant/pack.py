"""metrology_assistant 装配入口(南京院竞标 Demo 薄编排插件)。

契约对照 knowledge_graph/pack.py:create_registry 必须;无自有 API/
后台任务/设置页(Demo 阶段全免,工程化后再加管理页三 tab)。
"""
import logging

from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_registry(app_state=None) -> ToolRegistry:
    registry = ToolRegistry()
    from domains.metrology_assistant.tools.assistant import MetrologyAssistantTool
    registry.register(MetrologyAssistantTool(app_state))
    return registry
