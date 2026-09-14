"""chatbi pack 装配入口 —— 平台钩子实现集合(契约对照 knowledge_graph/pack.py)。

  create_registry()    必须: 注册 ask_data / switch_chart 工具
  create_api_router()  可选: 数据源管理/语义层/图谱/记忆/健康 HTTP API
  register_tasks()     可选: 元数据定时刷新(后台任务框架)
  unload()             可选: 释放运行时单例
"""
import logging

from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_registry(app_state=None) -> ToolRegistry:
    """工具注册表(app_state 由装配层注入;工具内懒建 pack 存储)。"""
    registry = ToolRegistry()
    try:
        from domains.chatbi.tools.ask_data import AskDataTool
        from domains.chatbi.tools.switch_chart import SwitchChartTool
        registry.register(AskDataTool(app_state))
        registry.register(SwitchChartTool(app_state))
    except Exception:
        logger.exception("chatbi 工具注册失败")
    return registry


def create_api_router():
    """插件自有 HTTP API(/api/packs/chatbi/*: 数据源/语义层/图谱/记忆/健康)。"""
    from domains.chatbi.api import router
    return router


def register_tasks(manager, app_state=None) -> None:
    """后台任务: 元数据/语义层定时刷新(任务类型 chatbi.refresh_semantics)。"""
    from domains.chatbi import tasks
    tasks.register_tasks(manager, app_state)


def unload() -> None:
    """卸载钩子: 释放 pack 单例(热切换/停机)。

    stores.reset_caches 覆盖: 向量存储连接缓存 + pack 库单例(经 runtime)
    + 向量前缀登记——比只清 runtime._db 更完整。
    """
    from domains.chatbi import stores
    stores.reset_caches()
    logger.info("chatbi unloaded: runtime cache released")
