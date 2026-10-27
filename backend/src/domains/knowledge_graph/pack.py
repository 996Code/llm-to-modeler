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


class KnowledgeQuestionRouter:
    """知识问答入口的意图解析由检索管线负责,不重复做工具分类。"""

    def route(self, user_input, artifact, history="", llm_client=None, conv_id=None):
        return "kb_search", 1.0


def create_router(registry):
    return KnowledgeQuestionRouter()


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
    另:零库首次启动自动建「默认知识库」(用户 10-15 建议:开箱即用,
    上传/问答不再面对空态);用户删掉后不重建(尊重删除)。
    """
    global _RECOVERY_DONE
    from domains.knowledge_graph import tasks
    tasks._app_state = app_state
    _ensure_default_kb(app_state)
    if task_manager is None or _RECOVERY_DONE:
        return
    completed = tasks._recover_stale_importing(task_manager)
    # 兼容旧的测试/第三方 hook: None 表示函数完成但未返回状态；只有
    # 明确 False 才代表失败，真实实现会在异常路径返回 False。
    if completed is not False:
        _RECOVERY_DONE = True


def _ensure_default_kb(app_state) -> None:
    """零库时建默认结构:「默认分组」文件夹 + 里面「默认知识库」(用户裁决);
    已有库(含 folder)不动;失败只记日志不阻断启动。"""
    try:
        from domains.knowledge_graph import runtime
        store = runtime.get_kg_store(app_state)
        if store.list_kbs():
            return
        folder = store.create_kb("默认分组", kind="folder")
        store.create_kb("默认知识库",
                       description="系统默认知识库——可直接上传文档开始使用",
                       parent_id=folder["id"])
        logger.info("零库启动:已创建默认分组/默认知识库")
    except Exception as e:
        logger.warning(f"默认知识库创建失败(不影响启动): {e}")


def unload() -> None:
    """卸载钩子(平台热切换/停机时调用):释放 Neo4j/向量连接单例。

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
