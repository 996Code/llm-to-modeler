"""knowledge_graph pack 装配入口 —— 平台可选钩子的实现集合。

【契约对照】(平台侧语义见 domains/__init__.py 与 services/pack_manager.py)
  create_registry()     必须:返回本 pack 的工具注册表(M4 注册 kb_search;
                        M2 先返回空注册表——引擎合并时零工具不致命)
  create_api_router()   可选:本 pack 的 HTTP API(平台挂 /api/packs/knowledge_graph)
  register_tasks(mgr)   可选:后台任务 handler 注册(M3 落地导入流水线)
"""
import logging

from sdk.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_registry(app_state=None) -> ToolRegistry:
    """工具注册表。app_state 由装配层注入(平台组件入口;旧签名兼容)。"""
    registry = ToolRegistry()
    try:
        from domains.knowledge_graph.tools.kb_search import KbSearchTool
        registry.register(KbSearchTool(app_state))
    except Exception:
        logger.exception("knowledge_graph 工具注册失败")
    return registry


def create_api_router():
    """插件自有 HTTP API(路由表定义见 domains/knowledge_graph/api.py)。"""
    from domains.knowledge_graph.api import router
    return router


def register_tasks(manager, app_state=None) -> None:
    """后台任务类型注册(平台装配时调用;app_state 供 handler 运行期取依赖)。"""
    from domains.knowledge_graph import tasks
    tasks.register_tasks(manager, app_state)


def unload() -> None:
    """卸载钩子(平台热切换/停机时调用):释放 Neo4j/Milvus 连接单例。

    没有这个钩子的话,禁用插件后 driver/gRPC channel 会一直挂到进程
    退出;重新启用时按设置指纹复用或重建,不影响正确性。
    """
    from domains.knowledge_graph.stores import reset_caches
    reset_caches()
    logger.info("knowledge_graph unloaded: graph/vector connections released")
