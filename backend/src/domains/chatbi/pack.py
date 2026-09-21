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
    校验/必需工具失败包装成 PackConfigurationError，表示本 pack 不可
    安全运行。平台默认只跳过该可选 pack；部署显式把 chatbi 加入
    PACKS_CRITICAL 时才阻断整次启动/装配。
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
            f"——拒绝加载不完整的 chatbi pack") from e
    return registry


def create_api_router():
    """插件自有 HTTP API(/api/packs/chatbi/*: 数据源/语义层/图谱/记忆/健康)。"""
    from domains.chatbi.api import router
    return router


def register_tasks(manager, app_state=None) -> None:
    """后台任务: 元数据/语义层定时刷新(任务类型 chatbi.refresh_semantics)。"""
    from domains.chatbi import tasks
    tasks.register_tasks(manager, app_state)


def start(app_state, task_manager=None) -> None:
    """启动 pack 自有生命周期资源。

    平台只调用通用 ``start`` 钩子，不需要知道 ChatBI 有 scheduler。
    """
    if task_manager is None:
        return
    from domains.chatbi.tasks import _start_refresh_scheduler
    _start_refresh_scheduler(task_manager, app_state)


def health_status() -> dict:
    """向平台 readiness 提供本 pack 的运行状态。"""
    from domains.chatbi.tasks import (
        scheduler_failure_threshold, scheduler_status)

    status = scheduler_status()
    if not status.get("ever_started") or status.get("stopped_by_unload"):
        return {"status": "ok", "component": "scheduler"}
    if not status.get("alive"):
        return {
            "status": "degraded",
            "detail": "chatbi scheduler thread dead",
        }
    failures = status.get("consecutive_failures", 0)
    if failures >= scheduler_failure_threshold():
        return {
            "status": "degraded",
            "detail": (f"chatbi scheduler failing ({failures}x): "
                       f"{status.get('last_error')}"),
        }
    return {"status": "ok", "component": "scheduler"}


def unload() -> None:
    """卸载钩子: 释放 pack 单例(热切换/停机)。

    stores.reset_caches 覆盖: 向量存储连接缓存 + pack 库单例(经 runtime)
    + 向量前缀登记——比只清 runtime._db 更完整。
    """
    # 十审 7.6: 先停调度线程再 reset(停止窗口内不访问已重置资源)
    from domains.chatbi.tasks import stop_refresh_scheduler
    # 停止失败必须向平台传播：继续清缓存会让仍存活的线程访问已释放资源，
    # 且 finalize 会误报卸载成功。平台会据此进入 degraded 并要求重启。
    stop_refresh_scheduler(stopped_by='unload')
    from domains.chatbi import stores
    stores.reset_caches()
    logger.info("chatbi unloaded: runtime cache released + scheduler stopped")
