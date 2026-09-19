"""PackManager - pack 启停的热切换编排。

【模块定位】
管理端(api/admin.py)切换插件开关后,由本模块把新的启停状态"装"进引擎:
重新加载 pack → 重新注入 nodes 模块全局 → 替换 app.state 上的共享引用。
启动路径(main.py lifespan)也复用同一入口,保证"冷启动"与"热切换"
走完全相同的装配逻辑(一处改动两条路径同时生效)。

【为什么热切换不需要重建 LangGraph 图】
graph.py 构建图时把节点函数(nodes.classify_intent_node 等模块级函数)
注册进 StateGraph,而节点函数执行时读的是 nodes 模块的模块级全局
(_registry/_pack_routers/...,由 configure() 注入)。图的拓扑(三个节点
+ 两条条件边)不随 pack 增减变化,变化只体现在注入的依赖上。因此:
  - 热切换 = 重新 nodes.configure(...) + 替换 app.state 引用
  - graph 对象保持不变:checkpointer 连接不重建(避免连接泄漏),
    各处闭包里持有的 graph 引用也自动"看到"新工具集

【时序语义】
切换瞬间在途的请求持有旧依赖引用或恰好读到新全局,最坏情况是某个工具
恰好被禁用导致该次调用走"工具不存在"的错误路径(与重启的窗口期相比
可忽略)。新请求立即全量生效。

【Java 类比】
类似 Spring 的 RefreshScope + ApplicationContext.refresh():
Bean 定义换掉,持旧引用的在途调用用完即弃,新调用全部走新容器。
"""
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 热切换装配总锁(三十四审 P2): "读 enabled 集合 → 装配 → 挂 API/任务"
# 整段串行化——此前只有 PackState 文件锁和 API mount 局部锁, 两个
# 管理员并发启停时两个 assemble 可交错, 最终的状态文件/registry/
# 任务 handler/API routes 不一定来自同一份 enabled snapshot。
# 进程内锁: 当前部署形态为单 worker 多进程各自持有独立内存态,
# 状态文件由 PackState 自身的文件锁保护跨进程写安全; 若未来
# 多 worker 均可触发热切换, 需升级为跨进程锁(见 admin.py 注释)。
_HOT_RELOAD_LOCK = threading.Lock()


def hot_reload_lock():
    """热切换装配总锁上下文(admin 启停 API 使用)。"""
    return _HOT_RELOAD_LOCK


def assemble_packs(
    app_state: Any,
    pack_names: Optional[List[str]] = None,
    app: Any = None,
) -> Dict[str, Any]:
    """按启停名单装配 pack 依赖并热替换引擎/路由共享的引用。

    Args:
        app_state: FastAPI 的 app.state(需已挂 conversation_manager、
            asset_client、llm_client、pack_state——main.py lifespan 负责)。
        pack_names: 要加载的 pack 名单。None = 交给 load_all_packs 走
            PACKS_ENABLED env 缺省路径(仅 main.py 启动时使用;
            热切换调用方应始终传 PackState 解析出的显式名单)。
        app: FastAPI 实例(可选)。传入时同步挂载 pack 自有 API 路由
            (/api/packs/{name});冷启动传 main 的 app,热切换传 request.app。

    Returns:
        装配摘要:{"loaded": [...], "tools": 总工具数, "pack_tools": {...},
        "dependency_status": {...}}。

    Raises:
        RuntimeError: 名单里一个 pack 都加载不出来(load_all_packs 抛出,
        调用方应阻止"禁用最后一个 pack"使这件事不发生)。
    """
    # 延迟导入:domains/engine 是重型模块,且 services 层保持按需依赖
    from domains import load_all_packs, load_pack_configs
    from engine import nodes

    settings_store = getattr(app_state, "settings_store", None)
    registry, prompt_loader, pack_routers, pack_tools, dep_status = load_all_packs(
        pack_names=pack_names, settings_store=settings_store, app_state=app_state
    )

    # pack 可选钩子 unload():上次在载、本次不在载(禁用/依赖失联)的
    # pack 释放自持资源(如数据库连接)。钩子异常只记日志——卸载清理
    # 失败不能阻断装配(资源最终随进程退出回收)。
    prev_loaded = set(getattr(app_state, "_loaded_packs", None) or [])
    import importlib
    for name in sorted(prev_loaded - set(pack_routers or {})):
        try:
            mod = importlib.import_module(f"domains.{name}.pack")
            hook = getattr(mod, "unload", None)
            if callable(hook):
                try:
                    hook()
                except Exception:
                    logger.exception(f"pack unload hook failed: {name}")
        except ImportError:
            pass
    app_state._loaded_packs = sorted(pack_routers or {})
    # manifest 只保留"实际加载成功"的 pack:依赖被跳过的 pack 工具并不存在,
    # 若把它的 manifest 留在 pack_configs,/api/meta/packs 会把它暴露给
    # 前端欢迎页(声明了却不存在的工具集)。管理端插件中心走 load_pack_configs
    # 全量清单 + dependency_status,不受此影响。
    pack_configs = {
        name: cfg for name, cfg in load_pack_configs(pack_names=pack_names).items()
        if name in pack_routers
    }

    # pack 可选钩子 enhance_asset_client(asset_client, upstream)：向通用
    # adapter 注入本 pack 的领域客户端（端点表/凭证策略/响应归一化归 pack，
    # adapter/传输层零领域知识）。无此钩子的 pack（如纯数据类）跳过；
    # 测试态 app_state 可能缺 upstream，一并守卫。
    if (getattr(app_state, "asset_client", None) is not None
            and getattr(app_state, "upstream", None) is not None):
        import importlib
        for pack_name in (pack_configs or {}):
            try:
                mod = importlib.import_module(f"domains.{pack_name}.pack")
                hook = getattr(mod, "enhance_asset_client", None)
                if callable(hook):
                    hook(app_state.asset_client, app_state.upstream)
            except ImportError:
                continue

    # 重新注入节点模块全局:graph 对象不重建(见模块文档),
    # 节点函数执行时读到的就是这套新依赖
    nodes.configure(
        registry=registry,
        llm_client=app_state.llm_client,
        asset_client=app_state.asset_client,
        conversation=app_state.conversation_manager,
        prompt_loader=prompt_loader,
        pack_routers=pack_routers,
        pack_configs=pack_configs,
    )

    # 替换 app.state 上的共享引用(meta/admin 路由按请求读取)
    app_state.registry = registry
    app_state.pack_configs = pack_configs
    app_state.pack_routers = pack_routers
    app_state.pack_tools = pack_tools
    app_state.prompt_loader = prompt_loader
    # 依赖检测状态(含被跳过的 pack;管理端插件中心"依赖未配置"徽标的数据源)
    app_state.pack_dependency_status = dep_status

    # pack 可选钩子 register_tasks(task_manager, app_state):注册后台任务
    # handler。每次装配先清空再由在载 pack 重新注册——禁用的 pack 任务类型
    # 随之失效(submit 会拒绝),与工具/路由的启停语义一致。
    # app_state 透传给钩子:handler 运行期要取 llm_client / settings_store。
    task_manager = getattr(app_state, "task_manager", None)
    if task_manager is not None:
        task_manager.reset_handlers()
        # 监听者与 handler 同生命周期:register_tasks 重调前清空,
        # 防热切换 N 次后同一终态回调挂 N 份
        task_manager.reset_terminal_listeners()
        import importlib
        for pack_name in (pack_routers or {}):
            try:
                mod = importlib.import_module(f"domains.{pack_name}.pack")
                hook = getattr(mod, "register_tasks", None)
                if callable(hook):
                    try:
                        hook(task_manager, app_state)
                    except TypeError:
                        hook(task_manager)  # 兼容单参签名(无 app_state 诉求的 pack)
            except ImportError:
                continue

    # pack 自有 HTTP API 动态挂载(先卸后挂;app 未提供时跳过,如部分测试态)
    api_mounted: List[str] = []
    if app is not None:
        from services.pack_api_mount import mount_pack_routers
        api_mounted = mount_pack_routers(app, list(pack_routers.keys()))

    # 刷新压缩侧重点：热切换后启用集变化，manifest compact_focus 声明
    # 需重新聚合（与 main.lifespan 启动装配同一语义，覆盖启动时的初值）
    compressor = getattr(app_state, "compressor", None)
    if compressor is not None:
        focus_parts = [
            (cfg.get("domain") or {}).get("compact_focus", "").strip()
            for cfg in (pack_configs or {}).values()
            if (cfg.get("domain") or {}).get("compact_focus")
        ]
        compressor.set_compact_focus("；".join(focus_parts))

    total_tools = sum(len(v) for v in pack_tools.values())
    logger.info(
        f"packs assembled: {sorted(pack_routers)} , {total_tools} tools"
        f"{f' , pack api: {api_mounted}' if api_mounted else ''}"
    )
    result = {
        "loaded": sorted(pack_routers),
        "tools": total_tools,
        "pack_tools": pack_tools,
        "dependency_status": dep_status,
        "api_mounted": api_mounted,
    }
    _assert_critical_packs_ready(app_state, result)
    return result


# critical pack 契约(三十四审 P1-B): 启用即必须完整可用——
# 任一必需组件缺失都终止启动/装配, 不允许"部分 ChatBI"的假 ready
_CRITICAL_PACKS = {
    "chatbi": {
        "required_tools": ("ask_data", "switch_chart"),
        "requires_api": True,
        "required_task_types": ("chatbi.refresh_semantics",),
    },
}


def _assert_critical_packs_ready(app_state: Any, result: Dict[str, Any]) -> None:
    """critical pack 完整性断言(三十四审 P1-B)。

    启用名单(loaded)含 critical pack 时, 以下不变量必须全部成立,
    否则抛 PackConfigurationError 终止:
      - pack 在 loaded(依赖闸门/import/registry 失败会让它不在);
      - 必需工具已注册(ask_data/switch_chart);
      - API 已挂载(requires_api);
      - 必需任务 handler 已注册(chatbi.refresh_semantics)。
    依赖闸门跳过/工具构造失败等路径在 loader 层已被 critical
    包装拦截; 本断言兜住"装配结果不完整"的残余路径(如任务
    handler 注册静默失败)。
    """
    from sdk.pack_api import PackConfigurationError
    loaded = set(result.get("loaded") or [])
    pack_tools = result.get("pack_tools") or {}
    api_mounted = set(result.get("api_mounted") or [])
    # 启用名单 = 本次装配请求的 pack(loaded 是成功子集);
    # critical 判定以 loaded 为准——没进 loaded 的 critical pack
    # 已在 loader 层抛错(依赖闸门除外, 见下)
    task_manager = getattr(app_state, "task_manager", None)
    for pack_name, req in _CRITICAL_PACKS.items():
        if pack_name not in loaded:
            continue   # 未启用或已被 loader 拦截
        missing_tools = [t for t in req["required_tools"]
                         if t not in (pack_tools.get(pack_name) or [])]
        if missing_tools:
            raise PackConfigurationError(
                f"critical pack {pack_name} 缺少必需工具: {missing_tools}"
                f"——终止启动(fail-fast)")
        if req.get("requires_api") and pack_name not in api_mounted:
            raise PackConfigurationError(
                f"critical pack {pack_name} 的 API 未挂载"
                f"——终止启动(fail-fast)")
        if task_manager is not None:
            handlers = getattr(task_manager, "_handlers", {}) or {}
            for tt in req.get("required_task_types", ()):
                if tt not in handlers:
                    raise PackConfigurationError(
                        f"critical pack {pack_name} 的任务 handler "
                        f"{tt} 未注册——终止启动(fail-fast)")
