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

    # ── 三十六审 P1-A: 真事务化——Prepare(全部可失败动作) / Commit(纯引用替换) ──
    # 三十五审的"两阶段"只覆盖 load_all_packs + router 构造; commit 段仍
    # 按顺序直接改在服务对象(unload → _loaded_packs → nodes → app.state
    # → handlers → routes), 中后段失败时 runtime 已是半装配新值(真实注入:
    # register_tasks 静默不注册 → 最终断言抛, 但 registry/routers/_loaded_
    # packs/handlers 已全部被替换)。现在:
    #   Prepare: load_all_packs + critical 断言(loaded/tools/api 名单) +
    #     pack_configs 过滤 + register_tasks 到 **staging dict**(不动在服务
    #     的 TaskManager) + critical 全量断言(含 handler) + router 构造与
    #     route 展开(临时 router, 不动 app.router.routes);
    #   Commit: 快照旧 runtime → 纯引用替换(nodes 全局/app.state/handlers/
    #     routes/compressor) → 任一步异常按快照回滚。
    #   unload 钩子后置到 commit 成功之后(旧 pack 的资源只在确认切换后
    #     才释放; 释放失败不回滚装配——资源随进程退出回收, 但不再污染
    #     在服务的运行态)。
    _assert_critical_packs_ready(
        app_state,
        {"loaded": sorted(pack_routers),
         "pack_tools": pack_tools,
         # API 挂载在 commit 段执行; 此处先按"将挂载名单"预检
         # (mount 阶段对 critical 失败会抛, 双保险)
         "api_mounted": sorted(pack_routers)},
        requested=list(pack_names or []),
        check_task_handlers=False)

    # manifest 只保留"实际加载成功"的 pack:依赖被跳过的 pack 工具并不存在,
    # 若把它的 manifest 留在 pack_configs,/api/meta/packs 会把它暴露给
    # 前端欢迎页(声明了却不存在的工具集)。管理端插件中心走 load_pack_configs
    # 全量清单 + dependency_status,不受此影响。
    pack_configs = {
        name: cfg for name, cfg in load_pack_configs(pack_names=pack_names).items()
        if name in pack_routers
    }

    # ── Prepare: register_tasks 到 staging(不动在服务的 TaskManager) ──
    # 三十六审 P1-A: 此前 reset_handlers() → 逐 pack register_tasks 直接
    # 操作在服务的 TaskManager, 钩子失败/静默不注册时旧 handlers 已被清空。
    # 现在 Prepare 只把 handler 收集进 staged_handlers; commit 成功后才
    # 一次性替换 TaskManager 内部表。scheduler 线程(chatbi register_tasks
    # 内部启动)是进程级单例且幂等(_start_refresh_scheduler 已存活即跳过),
    # 在 staging 阶段启动不影响旧 runtime——它读的是 settings/db, 不读
    # handlers; 若 commit 失败回滚, 已存活的 scheduler 继续跑旧配置(与
    # 切换前语义一致, 无需杀线程)。
    task_manager = getattr(app_state, "task_manager", None)
    staged_handlers: Dict[str, Any] = {}
    staged_handler_meta: Dict[str, Dict[str, str]] = {}
    if task_manager is not None:
        import importlib
        for pack_name in (pack_routers or {}):
            try:
                mod = importlib.import_module(f"domains.{pack_name}.pack")
                hook = getattr(mod, "register_tasks", None)
                if callable(hook):
                    _collect_register_tasks(
                        hook, pack_name, task_manager, app_state,
                        staged_handlers, staged_handler_meta)
            except ImportError:
                continue

    # ── Prepare: critical 全量断言(含 staging handler) ──
    # 用 staging 容器做断言: handler 完整性在切换运行态之前验证。
    _staged_tm = _StagedTaskManagerView(
        staged_handlers,
        store=getattr(task_manager, "store", None) if task_manager else None)
    _assert_critical_packs_ready(
        _StagedAppState(app_state, task_manager=_staged_tm),
        {"loaded": sorted(pack_routers),
         "pack_tools": pack_tools,
         "api_mounted": sorted(pack_routers)},
        requested=list(pack_names or []))

    # ── Prepare: router 构造 + route 展开(不动 app.router.routes) ──
    # 三十六审 P1-B: 此前 commit 段 _unmount_all → app.include_router,
    # include_router 中途失败时旧 route 已被卸掉。现在先在临时 FastAPI
    # 上展开全部 route 对象, commit 只做"一次性列表替换"。
    staged_routes: List[Any] = []
    staged_mounted: Dict[str, List[Any]] = {}
    if app is not None:
        from services.pack_api_mount import build_staged_routes
        staged_routes, staged_mounted = build_staged_routes(
            app, list(pack_routers.keys()))

    # ── Commit: 快照旧 runtime → 纯引用替换 → 异常回滚 ──
    snap = _runtime_snapshot(app_state, nodes, app)
    try:
        nodes.configure(
            registry=registry,
            llm_client=app_state.llm_client,
            asset_client=app_state.asset_client,
            conversation=app_state.conversation_manager,
            prompt_loader=prompt_loader,
            pack_routers=pack_routers,
            pack_configs=pack_configs,
        )
        app_state.registry = registry
        app_state.pack_configs = pack_configs
        app_state.pack_routers = pack_routers
        app_state.pack_tools = pack_tools
        app_state.prompt_loader = prompt_loader
        # 依赖检测状态(含被跳过的 pack;管理端插件中心"依赖未配置"徽标的数据源)
        app_state.pack_dependency_status = dep_status
        app_state._loaded_packs = sorted(pack_routers or {})

        if task_manager is not None:
            task_manager.reset_handlers()
            task_manager._handlers.update(staged_handlers)
            task_manager._handler_meta.update(staged_handler_meta)

        if app is not None:
            from services.pack_api_mount import commit_routes
            commit_routes(app, staged_routes, staged_mounted)

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
    except Exception:
        logger.exception("装配 commit 失败, 按快照回滚旧运行态")
        _restore_runtime(app_state, nodes, app, snap)
        raise

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

    # pack 可选钩子 unload():上次在载、本次不在载(禁用/依赖失联)的
    # pack 释放自持资源(如数据库连接)。钩子异常只记日志——卸载清理
    # 失败不能阻断装配(资源最终随进程退出回收)。
    # 三十六审 P1-A: 后置到 commit 成功之后——旧 pack 的资源只在确认
    # 切换后才释放, 释放失败不影响已提交的新运行态。
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

    api_mounted = sorted(staged_mounted.keys()) if app is not None else []
    total_tools = sum(len(v) for v in pack_tools.values())
    logger.info(
        f"packs assembled: {sorted(pack_routers)} , {total_tools} tools"
        f"{f' , pack api: {api_mounted}' if api_mounted else ''}"
    )
    return {
        "loaded": sorted(pack_routers),
        "tools": total_tools,
        "pack_tools": pack_tools,
        "dependency_status": dep_status,
        "api_mounted": api_mounted,
    }


# critical pack 契约(三十四审 P1-B): 启用即必须完整可用——
# 任一必需组件缺失都终止启动/装配, 不允许"部分 ChatBI"的假 ready
# 三十五审 P1-A: 名单从 sdk.pack_api.critical_packs() 单一真相源
# 读取(此前三个模块各自复制, 漏同步即窗口); 必需组件明细仍在此处
from sdk.pack_api import critical_packs

_CRITICAL_PACK_REQUIREMENTS = {
    "chatbi": {
        "required_tools": ("ask_data", "switch_chart"),
        "requires_api": True,
        "required_task_types": ("chatbi.refresh_semantics",),
    },
}


# ── 三十六审 P1-A: staging 容器(register_tasks 的 Prepare 收集目标) ──

class _StagedTaskManagerView:
    """给 register_tasks 钩子与 critical 断言看的"TaskManager 视图"。

    只实现钩子实际用到的 register(task_type, handler, pack_name=)——
    写入 staging dict, 不触碰在服务的 TaskManager。断言函数经
    _handlers 属性读取 staging 内容。
    """

    def __init__(self, handlers: Dict[str, Any], store: Any = None):
        self._handlers = handlers
        self.store = store  # KG 启动收敛等只读钩子用(list_tasks)

    def register(self, task_type, handler, pack_name: str = "") -> None:
        self._handlers[task_type] = handler

    def __getattr__(self, item):
        raise AttributeError(
            f"_StagedTaskManagerView 不支持成员 '{item}'"
            f"(register_tasks 钩子只应调用 register/读 store)")


class _StagedAppState:
    """critical 断言用的 app_state 替身: task_manager 换成 staging 视图。"""

    def __init__(self, real_app_state: Any, task_manager: Any):
        self._real = real_app_state
        self.task_manager = task_manager

    def __getattr__(self, item):
        return getattr(self._real, item)


def _collect_register_tasks(hook, pack_name: str, task_manager: Any,
                            app_state: Any,
                            staged_handlers: Dict[str, Any],
                            staged_handler_meta: Dict[str, Dict[str, str]]
                            ) -> None:
    """调用 register_tasks 钩子, 把 handler 收集进 staging dict。

    钩子签名兼容: (manager, app_state) / (manager)。钩子内部异常直接
    上抛——Prepare 段失败不切换运行态(critical pack 的钩子失败必须
    fail-fast; 非 critical pack 的钩子异常由 loader 层语义兜底,
    此处不吞)。
    """
    view = _StagedTaskManagerView(
        staged_handlers,
        store=getattr(task_manager, "store", None) if task_manager else None)
    try:
        hook(view, app_state)
    except TypeError:
        hook(view)  # 兼容单参签名(无 app_state 诉求的 pack)
    # handler 元数据按 staging 内容重建(pack 归属)
    for task_type in staged_handlers:
        staged_handler_meta.setdefault(
            task_type, {"packName": pack_name})


def _runtime_snapshot(app_state: Any, nodes: Any, app: Any) -> Dict[str, Any]:
    """commit 前抓取旧运行态快照(回滚用)。

    三十六审 P1-A: commit 段即使理论上只剩引用替换, 也保留快照——
    任何一步异常(含未来新增步骤)都按快照恢复, 不留半装配运行态。
    """
    snap: Dict[str, Any] = {
        "nodes": {
            "registry": nodes._registry,
            "pack_routers": dict(nodes._pack_routers or {}),
            "pack_configs": dict(nodes._pack_configs or {}),
            "llm_client": nodes._llm_client,
            "asset_client": nodes._asset_client,
            "conversation": nodes._conversation,
            "prompt_loader": nodes._prompt_loader,
        },
        "state": {
            "registry": getattr(app_state, "registry", None),
            "pack_configs": getattr(app_state, "pack_configs", None),
            "pack_routers": getattr(app_state, "pack_routers", None),
            "pack_tools": getattr(app_state, "pack_tools", None),
            "prompt_loader": getattr(app_state, "prompt_loader", None),
            "pack_dependency_status": getattr(
                app_state, "pack_dependency_status", None),
            "_loaded_packs": getattr(app_state, "_loaded_packs", None),
        },
    }
    task_manager = getattr(app_state, "task_manager", None)
    if task_manager is not None:
        snap["handlers"] = dict(getattr(task_manager, "_handlers", {}) or {})
        snap["handler_meta"] = dict(
            getattr(task_manager, "_handler_meta", {}) or {})
    if app is not None:
        from services.pack_api_mount import MOUNTED_ATTR
        snap["routes"] = list(getattr(app.router, "routes", None) or [])
        snap["mounted"] = dict(
            getattr(app.state, MOUNTED_ATTR, None) or {})
    return snap


def _restore_runtime(app_state: Any, nodes: Any, app: Any,
                      snap: Dict[str, Any]) -> None:
    """commit 失败后按快照恢复旧运行态(尽力而为, 恢复异常只记日志)。"""
    try:
        n = snap["nodes"]
        nodes.configure(
            registry=n["registry"],
            llm_client=n["llm_client"],
            asset_client=n["asset_client"],
            conversation=n["conversation"],
            prompt_loader=n["prompt_loader"],
            pack_routers=n["pack_routers"],
            pack_configs=n["pack_configs"],
        )
        for k, v in snap["state"].items():
            setattr(app_state, k, v)
        task_manager = getattr(app_state, "task_manager", None)
        if task_manager is not None and "handlers" in snap:
            task_manager.reset_handlers()
            task_manager._handlers.update(snap["handlers"])
            task_manager._handler_meta.update(snap["handler_meta"])
        if app is not None and "routes" in snap:
            from services.pack_api_mount import MOUNTED_ATTR
            app.router.routes = list(snap["routes"])
            setattr(app.state, MOUNTED_ATTR, snap["mounted"])
    except Exception:
        logger.exception("运行态快照恢复失败(建议重启进程恢复一致性)")


def _assert_critical_packs_ready(app_state: Any, result: Dict[str, Any],
                                  requested: Optional[List[str]] = None,
                                  check_task_handlers: bool = True
                                  ) -> None:
    """critical pack 完整性断言(三十四审 P1-B / 三十五审 P1-A)。

    三十五审 P1-A: 断言同时接收 **requested**(本次装配请求/启用的
    pack 名单)与 loaded(成功子集)——此前只看 loaded, critical pack
    被任何路径跳过后 `continue` 直接漏检(真实注入: loaded 只有
    knowledge_graph 时函数返回成功)。requested 含 critical 而
    loaded 不含时必须失败。
    其余不变量: 必需工具已注册 / API 已挂载 / 任务 handler 已注册。

    check_task_handlers: 任务 handler 注册是 Commit 段动作(register_tasks
    循环), Prepare 段调用时须传 False(冷启动时 handler 尚未注册,
    在 Prepare 查它会把正常启动误判为 fail); Commit 段末尾的全量
    复核用默认 True。
    """
    from sdk.pack_api import PackConfigurationError
    loaded = set(result.get("loaded") or [])
    pack_tools = result.get("pack_tools") or {}
    api_mounted = set(result.get("api_mounted") or [])
    requested_set = set(requested) if requested is not None else loaded
    task_manager = getattr(app_state, "task_manager", None)
    for pack_name in critical_packs():
        if pack_name not in requested_set:
            continue   # 本次未请求/未启用
        if pack_name not in loaded:
            # requested 含 critical 但 loaded 不含——被某条路径跳过
            # (loader 层已拦截大多数, 此处兜底残余路径)
            raise PackConfigurationError(
                f"critical pack {pack_name} 在 requested"
                f"({sorted(requested_set)})中但未成功加载"
                f"(loaded={sorted(loaded)})——终止(fail-fast), "
                f"不允许部分 ChatBI 的假 ready")
        req = _CRITICAL_PACK_REQUIREMENTS.get(pack_name)
        if not req:
            continue
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
        if check_task_handlers and task_manager is not None:
            handlers = getattr(task_manager, "_handlers", {}) or {}
            for tt in req.get("required_task_types", ()):
                if tt not in handlers:
                    raise PackConfigurationError(
                        f"critical pack {pack_name} 的任务 handler "
                        f"{tt} 未注册——终止启动(fail-fast)")
