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
    """工具注册表(app_state 由装配层注入;工具内懒建 pack 存储)。

    三十二审 P2: 装配期即校验 pack 运行配置(PACK_DDL_RETRY_* 等)
    ——非法值(如 attempts=1.9)在 pack 装配时抛 ValueError, 不再等
    首次业务请求才 500(fail-fast 名实相符)。
    三十三审 P1: 校验失败改抛 PackConfigurationError——此前普通
    ValueError 被平台 loader catch-continue 吞掉(真实复现: 即使
    PACKS_ENABLED=chatbi, 服务仍以 0 pack/0 工具"成功"启动);
    致命错误必须传播到 lifespan 让 Uvicorn 启动失败。
    三十四审 P1-B: 必需工具(ask_data/switch_chart)构造异常同样
    包装成 PackConfigurationError——普通 RuntimeError/TypeError 到
    loader 后仍会被多 pack 容错吞掉(真实注入复现: chatbi 失败
    + knowledge_graph 正常时服务照常 ready), critical pack 的
    任一必需组件失败都必须终止启动。
    """
    from sdk.pack_api import PackConfigurationError
    from domains.chatbi.runtime import validate_pack_runtime_config
    try:
        validate_pack_runtime_config()
    except ValueError as e:
        raise PackConfigurationError(f"chatbi 运行配置非法: {e}") from e
    registry = ToolRegistry()
    try:
        from domains.chatbi.tools.ask_data import AskDataTool
        from domains.chatbi.tools.switch_chart import SwitchChartTool
        registry.register(AskDataTool(app_state))
        registry.register(SwitchChartTool(app_state))
    except PackConfigurationError:
        raise
    except Exception as e:
        raise PackConfigurationError(
            f"chatbi 必需工具(ask_data/switch_chart)构造失败: {e}"
            f"——critical pack, 终止启动(fail-fast)") from e
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
    # 十审 7.6: 先停调度线程再 reset(停止窗口内不访问已重置资源)
    try:
        from domains.chatbi.tasks import stop_refresh_scheduler
        stop_refresh_scheduler()
    except Exception as e:
        logger.warning("chatbi scheduler 停止失败: %s", e)
    from domains.chatbi import stores
    stores.reset_caches()
    logger.info("chatbi unloaded: runtime cache released + scheduler stopped")
