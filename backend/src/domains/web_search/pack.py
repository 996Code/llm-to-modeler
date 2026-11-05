"""web_search pack 装配入口 —— 联网搜索独立公用插件。

【定位】(用户裁决方案 A)独立 pack=插件列表一等公民(自有设置页+联网调研页),
知识库/客服直连本 pack 的 websearch 底层(照客服直连 kg/chatbi 底层模式)。
"""
import logging

from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_registry(app_state=None) -> ToolRegistry:
    from sdk.pack_api import PackConfigurationError
    registry = ToolRegistry()
    try:
        from domains.web_search.tools.web_search import WebSearchTool
        registry.register(WebSearchTool(app_state))
    except Exception as e:
        raise PackConfigurationError(
            f"web_search 核心工具构造失败: {e}——拒绝加载不完整的 web_search pack") from e
    return registry


def create_api_router():
    """插件自有 HTTP API(路由表定义见 domains/web_search/api.py)。"""
    from domains.web_search.api import router
    return router
