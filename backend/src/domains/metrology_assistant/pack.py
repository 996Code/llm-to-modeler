"""metrology_assistant 装配入口(南京院竞标 Demo 薄编排插件)。

提供工具注册、确定性路由及测试页和配置管理 API。
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
    """插件自有 HTTP API(挂 /api/packs/metrology_assistant)。

    测试页数据面(api.py)和场景配置管理面(config_api.py)。
    """
    from fastapi import APIRouter
    from domains.metrology_assistant.api import router as test_router
    from domains.metrology_assistant.config_api import router as config_router
    combined = APIRouter()
    combined.include_router(test_router)
    combined.include_router(config_router)
    return combined
