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

_RECOVERY_DONE = False


def create_registry(app_state=None) -> ToolRegistry:
    """工具注册表。app_state 由装配层注入(平台组件入口;旧签名兼容)。"""
    from sdk.pack_api import PackConfigurationError
    registry = ToolRegistry()
    try:
        from domains.knowledge_graph.tools.kb_search import KbSearchTool
        registry.register(KbSearchTool(app_state))
    except Exception as e:
        raise PackConfigurationError(
            f"knowledge_graph 核心工具 kb_search 构造失败: {e}"
            "——拒绝加载不完整的 knowledge_graph pack") from e
    return registry


def create_api_router():
    """插件自有 HTTP API(路由表定义见 domains/knowledge_graph/api.py)。"""
    from domains.knowledge_graph.api import router
    return router


def register_tasks(manager, app_state=None) -> None:
    """后台任务类型注册(平台装配时调用;app_state 供 handler 运行期取依赖)。"""
    from domains.knowledge_graph import tasks
    tasks.register_tasks(manager, app_state)


def start(app_state, task_manager=None) -> None:
    """注入运行期状态并执行一次启动收敛。

    启动收敛属于 KG pack 的数据生命周期，不应由平台核心按 pack 名称
    分支调用。失败不置位，下一次显式装配仍可重试。
    """
    global _RECOVERY_DONE
    from domains.knowledge_graph import tasks
    tasks._app_state = app_state
    if task_manager is None or _RECOVERY_DONE:
        return
    completed = tasks._recover_stale_importing(task_manager)
    # 兼容旧的测试/第三方 hook: None 表示函数完成但未返回状态；只有
    # 明确 False 才代表失败，真实实现会在异常路径返回 False。
    if completed is not False:
        _RECOVERY_DONE = True


def unload() -> None:
    """卸载钩子(平台热切换/停机时调用):释放 Neo4j/Milvus 连接单例。

    没有这个钩子的话,禁用插件后 driver/gRPC channel 会一直挂到进程
    退出;重新启用时按设置指纹复用或重建,不影响正确性。
    """
    global _RECOVERY_DONE
    from domains.knowledge_graph.stores import reset_caches
    reset_caches()
    # start 的一次性守卫只覆盖当前加载周期。热禁用后再次启用时，停用
    # 窗口内可能留下 importing 文档，必须重新执行启动收敛。
    _RECOVERY_DONE = False
    logger.info("knowledge_graph unloaded: graph/vector connections released")
