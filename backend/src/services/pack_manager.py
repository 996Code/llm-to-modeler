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
import inspect
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AssemblyRollbackFailedError(RuntimeError):
    """装配 commit 失败且内部快照恢复也存在失败组件(三十九审 P1-B)。

    调用方(admin toggle/recheck)捕获后必须置 degraded——runtime 可能
    残留半装配状态, 不能谎称已回滚。
    """


def _replace_task_handlers(task_manager: Any, handlers: Dict[str, Any],
                           metadata: Dict[str, Dict[str, str]]) -> None:
    """替换任务表；兼容旧宿主提供的 reset/update 鸭子协议。

    平台内置 ``TaskManager`` 走单次锁内替换。嵌入式宿主或旧测试替身若
    还没有新方法，则退回其既有接口；这条兼容路径不改变内置运行时的
    原子性保证。
    """
    replace = getattr(task_manager, "replace_handlers", None)
    if callable(replace):
        replace(handlers, metadata)
        return
    reset = getattr(task_manager, "reset_handlers", None)
    if not callable(reset):
        raise AttributeError("task_manager 缺少 replace_handlers/reset_handlers")
    # 旧宿主没有原子替换契约，只能沿用 reset/update。不要擅自持有它的
    # 私有锁再调用 reset_handlers()：若 reset 内部也获取同一把非可重入锁，
    # 会在装配线程中永久自锁。平台内置 TaskManager 始终走上面的原子路径。
    reset()
    task_manager._handlers.update(handlers)
    task_manager._handler_meta.update(metadata)

# 热切换装配总锁:"读 enabled 集合 → 装配 → 挂 API/任务"整段串行化。
# 生产部署契约是单实例、单 Uvicorn worker；这把进程内锁仍用于隔离
# 管理请求与后台任务线程在 handler/routes 替换期间的交错。
_HOT_RELOAD_LOCK = threading.Lock()


def hot_reload_lock():
    """热切换装配总锁上下文(admin 启停 API 使用)。"""
    return _HOT_RELOAD_LOCK


def assemble_packs(
    app_state: Any,
    pack_names: Optional[List[str]] = None,
    app: Any = None,
    *,
    _assembly_errors: Optional[Dict[str, str]] = None,
    _dependency_status: Optional[Dict[str, Any]] = None,
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
    assembly_errors = dict(_assembly_errors or {})
    load_errors: Dict[str, str] = {}
    load_kwargs = {
        "pack_names": pack_names,
        "settings_store": settings_store,
        "app_state": app_state,
    }
    # 兼容嵌入宿主/旧测试替身的既有签名；平台实现通过 errors 输出
    # “工厂/import/工具合并”阶段的逐 pack 失败原因。若不回传，冷启动
    # 隔离后的管理页只能看到 enabled=true / loaded=false，却无法说明原因。
    try:
        supports_load_errors = "errors" in inspect.signature(
            load_all_packs).parameters
    except (TypeError, ValueError):
        supports_load_errors = False
    if supports_load_errors:
        load_kwargs["errors"] = load_errors
    registry, prompt_loader, pack_routers, pack_tools, dep_status = (
        load_all_packs(**load_kwargs))
    assembly_errors.update(load_errors)
    merged_dep_status = dict(_dependency_status or {})
    merged_dep_status.update(dep_status)

    # ── 三十六审 P1-A: 真事务化——Prepare(全部可失败动作) / Commit(纯引用替换) ──
    # 三十五审的"两阶段"只覆盖 load_all_packs + router 构造; commit 段仍
    # 按顺序直接改在服务对象(unload → _loaded_packs → nodes →
    # app.state → handlers → routes), 中后段失败时 runtime 已是半装配新值(真实注入:
    # register_tasks 静默不注册 → 最终断言抛, 但 registry/routers/_loaded_
    # packs/handlers 已全部被替换)。现在:
    #   Prepare: load_all_packs + 部署级 fail-fast 断言(loaded/tools/api 名单) +
    #     pack_configs 过滤 + register_tasks 到 **staging dict**(不动在服务
    #     的 TaskManager) + fail-fast 全量断言(含 handler) + router 构造与
    #     route 展开(临时 router, 不动 app.router.routes) + enhancer 预演;
    #   Commit: 快照旧 runtime → 纯引用替换(nodes 全局/app.state/handlers/
    #     routes/compressor/asset client) → 任一步异常按快照回滚;
    #   Post-commit: unload 钩子对 old_loaded - new_loaded 差集执行
    #     (三十七审 P1-D: 用快照的 old_loaded——此前在 _loaded_packs
    #     被覆盖后才读 prev_loaded, 差集恒空, 禁用后资源永不释放)。
    _assert_critical_packs_ready(
        app_state,
        {"loaded": sorted(pack_routers),
         "pack_tools": pack_tools,
         # API 挂载在 commit 段执行; 此处先按"将挂载名单"预检
         # (mount 阶段对 PACKS_CRITICAL 失败会抛, 双保险)
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
    # 一次性替换 TaskManager 内部表。
    # 三十七审 P1-B(副作用隔离): staging view 不再透传会触发生命周期的
    # 调用——各 pack 的 start 钩子及其自有后台资源都后移到 commit 成功之后
    # (_start_pack_lifecycle)。prepare 失败时零生命周期副作用。
    task_manager = getattr(app_state, "task_manager", None)
    staged_handlers: Dict[str, Any] = {}
    staged_handler_meta: Dict[str, Dict[str, str]] = {}
    task_errors: Dict[str, str] = {}
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
            except Exception as e:
                if pack_name in critical_packs():
                    raise PackConfigurationError(
                        f"fail-fast pack {pack_name} 任务注册失败: {e}——"
                        f"终止装配") from e
                logger.exception(
                    "可选 pack %s 任务注册失败,将隔离整个 pack", pack_name)
                task_errors[pack_name] = f"任务注册失败: {e}"

    # ── Prepare: 部署级 fail-fast 全量断言(含 staging handler) ──
    # 用 staging 容器做断言: handler 完整性在切换运行态之前验证。
    _staged_tm = _StagedTaskManagerView(
        staged_handlers,
        metadata=staged_handler_meta,
        store=getattr(task_manager, "store", None) if task_manager else None)
    # ── Prepare: router 构造 + route 展开(不动 app.router.routes) ──
    # 三十六审 P1-B: 此前 commit 段 _unmount_all → app.include_router,
    # include_router 中途失败时旧 route 已被卸掉。现在先在临时 FastAPI
    # 上展开全部 route 对象, commit 只做"一次性列表替换"。
    staged_routes: List[Any] = []
    staged_mounted: Dict[str, List[Any]] = {}
    route_errors: Dict[str, str] = {}
    if app is not None:
        from services.pack_api_mount import build_staged_routes
        # 保持嵌入宿主/测试替身的旧二参签名兼容；平台实现支持 errors
        # 收集时才传第三参。签名协商不靠捕获执行期 TypeError。
        try:
            supports_errors = "errors" in inspect.signature(
                build_staged_routes).parameters
        except (TypeError, ValueError):
            supports_errors = False
        if supports_errors:
            staged_routes, staged_mounted = build_staged_routes(
                app, list(pack_routers.keys()), errors=route_errors)
        else:
            staged_routes, staged_mounted = build_staged_routes(
                app, list(pack_routers.keys()))

    # pack 默认可选不等于允许“半个插件”留在运行态。API/任务 hook 抛错，
    # 或 manifest 声明的运行契约不完整时，默认只隔离该 pack，再从剩余
    # pack 重新 Prepare；PACKS_CRITICAL 中的 pack 仍直接 fail-fast。
    contract_errors = _runtime_contract_failures(
        pack_configs=pack_configs,
        pack_tools=pack_tools,
        api_mounted=(set(staged_mounted) if app is not None
                     else set(pack_routers)),
        task_handlers=staged_handlers,
        task_handler_meta=staged_handler_meta,
        check_api=app is not None,
        check_tasks=task_manager is not None,
    )
    # 具体 hook 异常优先于派生的“组件缺失”结论，便于管理端/health 定位。
    failed_packs = {**contract_errors, **task_errors, **route_errors}
    if failed_packs:
        critical_failed = sorted(set(failed_packs) & set(critical_packs()))
        if critical_failed:
            name = critical_failed[0]
            raise PackConfigurationError(
                f"fail-fast pack {name} 装配契约失败: {failed_packs[name]}")
        assembly_errors.update(failed_packs)
        remaining = [
            name for name in pack_routers if name not in failed_packs]
        if not remaining:
            raise RuntimeError(
                "所有可选 pack 都在完整性校验中失败，系统无可用工具包: "
                f"{failed_packs}")
        logger.error(
            "隔离装配失败的可选 pack 并重试剩余集合: failed=%s remaining=%s",
            failed_packs, remaining)
        return assemble_packs(
            app_state, remaining, app=app,
            _assembly_errors=assembly_errors,
            _dependency_status=merged_dep_status)

    # 以真实 staging 结果做最终 fail-fast 断言，不能用“计划挂载名单”
    # 代替实际 API/handler 结果。
    _assert_critical_packs_ready(
        _StagedAppState(app_state, task_manager=_staged_tm),
        {"loaded": sorted(pack_routers),
         "pack_tools": pack_tools,
         "api_mounted": (sorted(staged_mounted)
                         if app is not None else sorted(pack_routers))},
        requested=list(pack_names or []))

    # ── Prepare: enhancer 预演(三十七审 P1-C) ──
    # enhance_asset_client 此前在 commit try/except 之外执行——hook 先改
    # asset client 再抛错时, 新 runtime 与部分 client 状态同时残留。
    # HttpAssetClient 的领域注入是 set_config_api(单一引用), 快照/恢复
    # 该引用即可让 enhancer 进入事务边界; 预演阶段不执行 hook(它只对
    # commit 后的新 pack_configs 有意义), 而是把执行挪进 commit try 块,
    # 异常时按快照恢复 client 引用。
    snap = _runtime_snapshot(app_state, nodes, app)

    # ── Commit: 快照旧 runtime → 纯引用替换 → 异常回滚 ──
    # 三十八审 P1-B: 回滚是**独立 compensator 链**(逐组件 try, 按 commit
    # 逆序, 聚合全部错误)——此前单一 try 里任一恢复 setter 再抛错,
    # 后续组件(handlers/routes)全部跳过。
    # 三十八审 P2-B: lifecycle/unload 不再在 assemble 内执行——外部
    # 副作用(scheduler 启动/连接释放/KG 数据收敛)不可逆, 必须等
    # runtime 与磁盘状态都确认后才 finalize(见 finalize_assembly)。
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
        app_state.pack_dependency_status = merged_dep_status
        app_state._loaded_packs = sorted(pack_routers or {})
        app_state._pack_assembly_errors = assembly_errors

        if task_manager is not None:
            _replace_task_handlers(
                task_manager, staged_handlers, staged_handler_meta)

        if app is not None:
            from services.pack_api_mount import commit_routes
            commit_routes(app, staged_routes, staged_mounted)

        # 刷新压缩侧重点：热切换后启用集变化，manifest compact_focus 声明
        # 需重新聚合（与 main.lifespan 启动装配同一语义，覆盖启动时的初值）
        # 三十七审 P1-C: compressor 进入事务边界——快照已含 compact_focus,
        # set_compact_focus 抛错时与其他运行态一并回滚。
        compressor = getattr(app_state, "compressor", None)
        if compressor is not None:
            focus_parts = [
                (cfg.get("domain") or {}).get("compact_focus", "").strip()
                for cfg in (pack_configs or {}).values()
                if (cfg.get("domain") or {}).get("compact_focus")
            ]
            compressor.set_compact_focus("；".join(focus_parts))

        # pack 可选钩子 enhance_asset_client(asset_client, upstream)：向通用
        # adapter 注入本 pack 的领域客户端（端点表/凭证策略/响应归一化归 pack，
        # adapter/传输层零领域知识）。无此钩子的 pack（如纯数据类）跳过；
        # 测试态 app_state 可能缺 upstream，一并守卫。
        # 三十七审 P1-C: 挪进 commit try 块——hook 抛错时按快照回滚
        # asset client 引用(与 nodes/app.state/handlers/routes 同边界)。
        # 三十八审 P2-C: 按启用集合**整体替换**——先清空旧引用再逐 pack
        # 注入, 禁用的 pack 的领域客户端不再残留(此前 njmind_form 禁用后
        # _config_api 仍是它的 ModelerAPI)。
        if (getattr(app_state, "asset_client", None) is not None
                and getattr(app_state, "upstream", None) is not None):
            import importlib
            asset_client = app_state.asset_client
            asset_client.set_config_api(None)   # 撤销旧 pack 的注入
            for pack_name in (pack_configs or {}):
                try:
                    mod = importlib.import_module(f"domains.{pack_name}.pack")
                    hook = getattr(mod, "enhance_asset_client", None)
                    if callable(hook):
                        hook(asset_client, app_state.upstream)
                except ImportError:
                    continue
    except Exception as commit_err:
        logger.exception("装配 commit 失败, 按快照回滚旧运行态")
        # 三十九审 P1-B: 内部快照恢复的失败必须传播——此前忽略
        # _restore_runtime 的 False, 上层拿不到 summary 便假设"内部
        # 已回滚", 谎报 State rolled back 而 runtime 实际留新值。
        # 恢复失败时抛结构化异常, 调用方据此进入 degraded。
        restored = _restore_runtime(app_state, nodes, app, snap)
        if not restored:
            raise AssemblyRollbackFailedError(
                f"commit 失败且快照恢复存在失败组件(原错误: "
                f"{commit_err})——runtime 可能残留半装配状态") from commit_err
        raise

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
        "dependency_status": merged_dep_status,
        "assembly_errors": assembly_errors,
        "api_mounted": api_mounted,
        # 三十八审 P1-A: 返回事务 handle——persist 失败时按快照确定性
        # 恢复(不重跑完整装配), finalize(生命周期/unload)由调用方在
        # 磁盘确认后执行
        "_tx": {
            "snapshot": snap,
            "pack_routers": pack_routers,
            "task_manager": task_manager,
            "app": app,
        },
    }


def finalize_assembly(app_state: Any, tx: Dict[str, Any]) -> None:
    """装配事务的 finalize 段(三十八审 P2-B): runtime 与磁盘都确认后执行。

    外部不可逆副作用只在此时发生:
      - lifecycle 启动(由各 pack 的 start 钩子自持);
      - unload 钩子(旧在载、新不在载的 pack 释放资源)。
    调用方(main 冷启动 / admin persist 成功后)负责在 PackState 持久化
    **之后**调用本函数——persist 失败时 finalize 尚未执行, 无需撤销。
    """
    snap = tx["snapshot"]
    pack_routers = tx["pack_routers"]
    task_manager = tx["task_manager"]
    app = tx["app"]

    old_loaded = set(snap["state"]["_loaded_packs"] or [])
    new_loaded = set(pack_routers or {})
    lifecycle_errors = set(
        (getattr(app_state, "_pack_lifecycle_errors", {}) or {}).keys())
    # start 不是隐式幂等契约。只启动本次新增 pack；此前 start 失败并
    # 留有 lifecycle error 的 pack 允许在 recheck 时重试。否则切换任意
    # 无关 pack 都会重复创建其它插件的线程/连接。
    start_names = (new_loaded - old_loaded) | (new_loaded & lifecycle_errors)
    _start_pack_lifecycle(
        app_state, pack_routers, task_manager, start_names=start_names)

    # unload 钩子(三十七审 P1-D): 差集基于快照的 old_loaded
    import importlib
    unload_errors: Dict[str, str] = {}
    for name in sorted(old_loaded - new_loaded):
        try:
            mod = importlib.import_module(f"domains.{name}.pack")
            hook = getattr(mod, "unload", None)
            if callable(hook):
                try:
                    hook()
                except Exception as e:
                    logger.exception(f"pack unload hook failed: {name}")
                    unload_errors[name] = str(e)
        except ImportError:
            pass
    if unload_errors:
        lifecycle_errors = dict(
            getattr(app_state, "_pack_lifecycle_errors", {}) or {})
        lifecycle_errors.update({
            name: f"卸载失败: {error}"
            for name, error in unload_errors.items()
        })
        app_state._pack_lifecycle_errors = lifecycle_errors
        raise RuntimeError(
            "pack unload 未完整完成，进程需重启: "
            + "; ".join(
                f"{name}: {error}" for name, error in unload_errors.items()))


def rollback_assembly(app_state: Any, tx: Dict[str, Any]) -> bool:
    """按事务快照确定性回滚 runtime(三十八审 P1-A)。

    persist 失败时使用——不再重跑一次完整装配(反向 assemble 会重新执行
    依赖检测/hook/生命周期, 本身可能失败, 失败后 runtime 与状态分裂)。
    快照恢复是纯引用替换, 确定性执行。

    Returns:
        True = 全部组件恢复成功; False = 有组件恢复失败(调用方必须进入
        degraded 状态并拒绝后续流量, 不能谎称已回滚)。
    """
    return _restore_runtime(app_state, nodes_module(), tx["app"],
                            tx["snapshot"])


def nodes_module():
    """延迟导入 engine.nodes(避免 services 层模块级反向依赖)。"""
    from engine import nodes
    return nodes


# 部署级 fail-fast pack 契约。名单来自 PACKS_CRITICAL，默认空；
# 具体必需组件由 pack 自己的 config.yaml.runtime_contract 声明。
from sdk.pack_api import critical_packs, PackConfigurationError


# ── 三十六审 P1-A: staging 容器(register_tasks 的 Prepare 收集目标) ──

class _StagedTaskManagerView:
    """给 register_tasks 钩子与 fail-fast 断言看的 TaskManager 视图。

    只实现钩子实际用到的 register(task_type, handler, pack_name=)——
    写入 staging dict, 不触碰在服务的 TaskManager。断言函数经
    _handlers 属性读取 staging 内容。
    """

    def __init__(self, handlers: Dict[str, Any],
                 metadata: Optional[Dict[str, Dict[str, str]]] = None,
                 store: Any = None):
        self._handlers = handlers
        self._handler_meta = metadata if metadata is not None else {}
        self.store = store  # KG 启动收敛等只读钩子用(list_tasks)

    def register(self, task_type, handler, pack_name: str = "") -> None:
        from sdk.pack_api import TaskRegistrationError
        if not isinstance(task_type, str) or not task_type.strip():
            raise TaskRegistrationError("task_type 必须是非空字符串")
        if not callable(handler):
            raise TaskRegistrationError(
                f"任务 {task_type!r} 的 handler 必须可调用")
        if task_type in self._handlers:
            raise TaskRegistrationError(
                f"重复注册任务类型 {task_type!r}; pack 任务名必须全局唯一")
        self._handlers[task_type] = handler

    def __getattr__(self, item):
        raise AttributeError(
            f"_StagedTaskManagerView 不支持成员 '{item}'"
            f"(register_tasks 钩子只应调用 register/读 store)")


class _StagedAppState:
    """fail-fast 断言用 app_state 替身: task_manager 换成 staging 视图。"""

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
    上抛——Prepare 段失败不切换运行态(PACKS_CRITICAL pack 的钩子
    失败必须 fail-fast；普通 pack 的钩子异常由 loader 层语义兜底，
    此处不吞)。
    """
    view = _StagedTaskManagerView(
        staged_handlers,
        metadata=staged_handler_meta,
        store=getattr(task_manager, "store", None) if task_manager else None)
    try:
        params = inspect.signature(hook).parameters.values()
        positional = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_app_state = bool(positional) and (
            len(positional) >= 2 or any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params))
    except (TypeError, ValueError):
        accepts_app_state = False
    if accepts_app_state:
        hook(view, app_state)
    else:
        hook(view)  # 兼容单参签名(无 app_state 诉求的 pack)
    # handler 元数据按 staging 内容重建(pack 归属)
    for task_type in staged_handlers:
        staged_handler_meta.setdefault(
            task_type, {"packName": pack_name})


def _runtime_contract_failures(
    pack_configs: Dict[str, Any],
    pack_tools: Dict[str, List[str]],
    api_mounted: set[str],
    task_handlers: Dict[str, Any],
    task_handler_meta: Dict[str, Dict[str, str]],
    *,
    check_api: bool,
    check_tasks: bool,
) -> Dict[str, str]:
    """校验各 pack 自声明的运行完整性，返回 pack → 原因。

    ``runtime_contract`` 是 pack 自身的原子装配边界，不是平台的必选
    插件名单。普通 pack 不满足时只隔离自身；部署显式列入
    ``PACKS_CRITICAL`` 时由调用方把同一失败提升为整次装配失败。
    """
    failures: Dict[str, str] = {}
    for pack_name, config in (pack_configs or {}).items():
        contract = (config or {}).get("runtime_contract") or {}
        if not contract:
            continue
        missing_tools = [
            name for name in contract.get("required_tools", ())
            if name not in (pack_tools.get(pack_name) or [])]
        if missing_tools:
            failures[pack_name] = f"缺少必需工具: {missing_tools}"
            continue
        if (check_api and contract.get("requires_api")
                and pack_name not in api_mounted):
            failures[pack_name] = "必需 API 未挂载"
            continue
        if check_tasks:
            missing_tasks = [
                name for name in contract.get("required_task_types", ())
                if (name not in task_handlers
                    or (task_handler_meta.get(name) or {}).get("packName")
                    != pack_name)]
            if missing_tasks:
                failures[pack_name] = f"缺少必需任务 handler: {missing_tasks}"
    return failures


def _runtime_snapshot(app_state: Any, nodes: Any, app: Any) -> Dict[str, Any]:
    """commit 前抓取旧运行态快照(回滚用)。

    三十六审 P1-A: commit 段即使理论上只剩引用替换, 也保留快照——
    任何一步异常(含未来新增步骤)都按快照恢复, 不留半装配运行态。
    三十七审 P1-C: 补齐 compressor 的 compact_focus 与 asset client 的
    领域注入引用(set_config_api 的单一引用)——enhancer/set_compact_focus
    抛错时一并恢复。
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
            "_pack_assembly_errors": getattr(
                app_state, "_pack_assembly_errors", None),
        },
    }
    compressor = getattr(app_state, "compressor", None)
    if compressor is not None:
        snap["compact_focus"] = getattr(compressor, "_compact_focus", None)
    asset_client = getattr(app_state, "asset_client", None)
    if asset_client is not None:
        snap["asset_config_api"] = getattr(asset_client, "_config_api", None)
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
                      snap: Dict[str, Any]) -> bool:
    """commit 失败后按快照恢复旧运行态(三十八审 P1-B: 独立 compensator 链)。

    每个组件独立 try——任一恢复动作抛错不再阻断后续组件(handlers/
    routes 必须尽力恢复); 全部错误聚合后返回。可直接赋值的字段直接
    赋值(不走已知失败的 setter); 必须经 setter 的(compressor focus)
    用 try 包裹。

    Returns:
        True = 全部恢复成功; False = 有组件恢复失败(调用方进入 degraded)。
    """
    errors: List[str] = []

    def _step(name: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.exception(f"运行态恢复失败(组件 {name})")

    # 快照键容错(三十八审自查 B2): 不完整/畸形快照(缺 nodes/state 键)
    # 记为组件失败而不是 KeyError 崩溃——rollback 的契约是"要么恢复
    # 成功, 要么返回 False", 调用方据此进入 degraded
    n = snap.get("nodes") or {}
    if n:
        _step("nodes", lambda: nodes.configure(
            registry=n["registry"],
            llm_client=n["llm_client"],
            asset_client=n["asset_client"],
            conversation=n["conversation"],
            prompt_loader=n["prompt_loader"],
            pack_routers=n["pack_routers"],
            pack_configs=n["pack_configs"],
        ))
    else:
        errors.append("nodes: 快照缺失 nodes 键")
    if "state" in snap:
        _step("app.state", lambda: [
            setattr(app_state, k, v) for k, v in snap["state"].items()])
    else:
        errors.append("app.state: 快照缺失 state 键")
    compressor = getattr(app_state, "compressor", None)
    if compressor is not None and "compact_focus" in snap:
        # 直接赋值不走 setter——commit 失败可能正是 setter 抛错,
        # 恢复时再调同一个 setter 会二次失败(三十八审 4.2 反例)
        _step("compressor", lambda: setattr(
            compressor, "_compact_focus", snap["compact_focus"]))
    asset_client = getattr(app_state, "asset_client", None)
    if asset_client is not None and "asset_config_api" in snap:
        _step("asset_client", lambda: setattr(
            asset_client, "_config_api", snap["asset_config_api"]))
    task_manager = getattr(app_state, "task_manager", None)
    if task_manager is not None and "handlers" in snap:
        def _restore_handlers():
            _replace_task_handlers(
                task_manager, snap["handlers"], snap["handler_meta"])
        _step("handlers", _restore_handlers)
    if app is not None and "routes" in snap:
        def _restore_routes():
            from services.pack_api_mount import MOUNTED_ATTR
            app.router.routes = list(snap["routes"])
            setattr(app.state, MOUNTED_ATTR, snap["mounted"])
        _step("routes", _restore_routes)

    if errors:
        logger.error(
            f"运行态快照恢复存在失败组件(建议重启): {errors}")
        return False
    return True


def _start_pack_lifecycle(app_state: Any, pack_routers: Dict[str, Any],
                          task_manager: Any,
                          start_names: Optional[set[str]] = None) -> None:
    """finalize 段调用每个 pack 自有的可选 ``start`` 钩子。

    平台只负责生命周期时序和部署级 fail-fast 语义；scheduler、启动
    收敛或其他领域资源由 pack 自己实现，避免平台核心按 pack 名称分支。
    """
    import importlib
    lifecycle_errors = dict(
        getattr(app_state, "_pack_lifecycle_errors", {}) or {})
    # 已不在本次装配中的 pack 不保留旧错误。
    lifecycle_errors = {
        name: error for name, error in lifecycle_errors.items()
        if name in (pack_routers or {})}
    names = set(pack_routers or {}) if start_names is None else start_names
    for pack_name in sorted(names):
        try:
            mod = importlib.import_module(f"domains.{pack_name}.pack")
            hook = getattr(mod, "start", None)
            if callable(hook):
                hook(app_state, task_manager)
            lifecycle_errors.pop(pack_name, None)
        except Exception as e:
            lifecycle_errors[pack_name] = str(e)
            if pack_name in critical_packs():
                # 部署显式要求 fail-fast 的 pack 生命周期失败。
                app_state._pack_lifecycle_errors = lifecycle_errors
                raise PackConfigurationError(
                    f"fail-fast pack {pack_name} 生命周期启动失败: {e}"
                    f"——终止(fail-fast)") from e
            logger.exception(f"pack 生命周期启动失败: {pack_name}")
    app_state._pack_lifecycle_errors = lifecycle_errors


def _assert_critical_packs_ready(app_state: Any, result: Dict[str, Any],
                                  requested: Optional[List[str]] = None,
                                  check_task_handlers: bool = True
                                  ) -> None:
    """部署级 fail-fast pack 完整性断言。

    断言同时接收 **requested**(本次装配请求/启用名单)与 loaded
    (成功子集)。requested 含 PACKS_CRITICAL 中的 pack 而 loaded
    不含时失败。默认名单为空，不对 ChatBI 或其它插件做特殊绑定。
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
            # requested 含 fail-fast pack 但 loaded 不含——被某路径跳过
            # (loader 层已拦截大多数, 此处兜底残余路径)
            raise PackConfigurationError(
                f"fail-fast pack {pack_name} 在 requested"
                f"({sorted(requested_set)})中但未成功加载"
                f"(loaded={sorted(loaded)})——终止装配")
        from domains import load_pack_configs
        manifest = load_pack_configs(pack_names=[pack_name]).get(pack_name) or {}
        req = manifest.get("runtime_contract") or {}
        if not req:
            continue
        missing_tools = [t for t in req.get("required_tools", ())
                         if t not in (pack_tools.get(pack_name) or [])]
        if missing_tools:
            raise PackConfigurationError(
                f"fail-fast pack {pack_name} 缺少必需工具: {missing_tools}"
                f"——终止启动(fail-fast)")
        if req.get("requires_api") and pack_name not in api_mounted:
            raise PackConfigurationError(
                f"fail-fast pack {pack_name} 的 API 未挂载"
                f"——终止启动(fail-fast)")
        if check_task_handlers and task_manager is not None:
            handlers = getattr(task_manager, "_handlers", {}) or {}
            handler_meta = getattr(task_manager, "_handler_meta", {}) or {}
            for tt in req.get("required_task_types", ()):
                if (tt not in handlers
                        or (handler_meta.get(tt) or {}).get("packName")
                        != pack_name):
                    raise PackConfigurationError(
                        f"fail-fast pack {pack_name} 的任务 handler "
                        f"{tt} 未由该 pack 注册——"
                        f"终止启动(fail-fast)")
