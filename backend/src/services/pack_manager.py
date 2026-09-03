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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def assemble_packs(app_state: Any, pack_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """按启停名单装配 pack 依赖并热替换引擎/路由共享的引用。

    Args:
        app_state: FastAPI 的 app.state(需已挂 conversation_manager、
            asset_client、llm_client、pack_state——main.py lifespan 负责)。
        pack_names: 要加载的 pack 名单。None = 交给 load_all_packs 走
            PACKS_ENABLED env 缺省路径(仅 main.py 启动时使用;
            热切换调用方应始终传 PackState 解析出的显式名单)。

    Returns:
        装配摘要:{"loaded": [...], "tools": 总工具数, "pack_tools": {...}}。

    Raises:
        RuntimeError: 名单里一个 pack 都加载不出来(load_all_packs 抛出,
        调用方应阻止"禁用最后一个 pack"使这件事不发生)。
    """
    # 延迟导入:domains/engine 是重型模块,且 services 层保持按需依赖
    from domains import load_all_packs, load_pack_configs
    from engine import nodes

    registry, prompt_loader, pack_routers, pack_tools = load_all_packs(pack_names=pack_names)
    pack_configs = load_pack_configs(pack_names=pack_names)

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
    logger.info(f"packs assembled: {sorted(pack_routers)} , {total_tools} tools")
    return {"loaded": sorted(pack_routers), "tools": total_tools, "pack_tools": pack_tools}
