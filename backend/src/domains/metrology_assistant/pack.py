"""metrology_assistant 装配入口(南京院竞标 Demo 薄编排插件)。

契约对照 knowledge_graph/pack.py:create_registry 必须;无自有 API/
后台任务/设置页(Demo 阶段全免,工程化后再加管理页三 tab)。
create_router:确定性直达主工具——场景识别在工具内做,不为单工具
多烧一次路由 LLM(设计 5.1 路由第 2 层/第 9 章"零路由模型调用")。
"""
import logging

from sdk.pack_router import DefaultPackRouter
from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)


class MetrologyDirectRouter(DefaultPackRouter):
    """单主工具确定性路由:一切消息直达 metrology_assistant。

    超范围问题由工具内"other"分支如实答复(超出演示范围),不经
    LLM 路由兜底到闲聊工具——路由零 LLM,场景识别零额外调用。
    """

    def route(self, user_input: str, artifact=None, history: str = "",
              llm_client=None, conv_id: str = None) -> tuple:
        tools = self._registry.all()
        return (tools[0].name if tools else None, 1.0)


def create_registry(app_state=None) -> ToolRegistry:
    registry = ToolRegistry()
    from domains.metrology_assistant.tools.assistant import MetrologyAssistantTool
    registry.register(MetrologyAssistantTool(app_state))
    return registry


def create_router(registry=None):
    return MetrologyDirectRouter(registry)


def create_api_router():
    """插件自有 HTTP API(测试页数据面;挂 /api/packs/metrology_assistant)。"""
    from domains.metrology_assistant.api import router
    return router
