"""knowledge_graph 存储适配层 —— 插件设置 → SDK 存储单例。

【职责】
SDK 图/向量存储(graph_store/vector_store)的调用参数由调用方注入;
本模块把"插件存储装配"接在一起,给 runtime/pack 提供稳定入口。
KG 插件的存储命名空间前缀在这里声明并登记:

  - 图存储前缀 "kg"(PG 图表在插件 schema 内,前缀登记保留为命名空间
    声明;向量表前缀 "kg" 与存量 kg_{kb_id}_v1 原样兼容)。

前缀登记是幂等的(同 owner 重复登记合法),装配/热切换反复经过这里
不会炸;撞前缀(别的插件声明了 kg)会在装配期 fail-fast。
"""
from sdk import graph_store as sdk_graph
from sdk import vector_store as sdk_vector
from sdk.scope_registry import register_prefix, unregister_prefix

PACK_NAME = "knowledge_graph"
VECTOR_PREFIX = "kg"     # 向量存储表/collection 名前缀(与存量数据一致)


def _ensure_prefix_registered() -> None:
    register_prefix("kg", owner=PACK_NAME)


def get_graph(app_state) -> sdk_graph.PgGraphStore:
    """图存储单例(PostgreSQL;图表在本插件 schema,连接走平台共享池)。"""
    from domains.knowledge_graph import runtime
    _ensure_prefix_registered()
    runtime.get_kg_store(app_state)   # 确保插件 schema 就绪(图图表同域)
    return sdk_graph.get_graph_store(PACK_NAME)


def get_vector(app_state) -> sdk_vector.VectorStore:
    """向量存储单例(pg/pgvector)。"""
    from domains.knowledge_graph import runtime
    _ensure_prefix_registered()
    settings = runtime.settings_reader(app_state).all()
    return sdk_vector.get_vector_store(
        settings,
        collection_prefix=VECTOR_PREFIX,
    )


def reset_caches() -> None:
    """释放 SDK 存储连接(pack unload 钩子调用)。

    在途导入任务持有 store 引用继续跑——粗暴清理会让任务中途失败。
    有在途任务时跳过释放(连接随进程退出回收,或下次 unload 再试);
    禁用插件的主要收益(路由下线/任务类型拒收)不受影响。
    """
    from domains.knowledge_graph import tasks as kg_tasks
    # 在途导入数:框架 dedupe 表只读聚合(kg.import:* 前缀的任务数)
    mgr = getattr(kg_tasks._app_state, "task_manager", None)
    busy = mgr.active_dedupe_count("kg.import:") if mgr else 0
    if busy:
        # 连接与前缀登记都留给在途任务:在途任务仍在写数据,此刻注销
        # 前缀会打开抢注窗口(另一插件登记 kg,两边共用命名空间直到
        # 冲突在数据互踩后才暴露)。下次 unload 再清。
        import logging
        logging.getLogger(__name__).warning(
            f"unload 跳过连接释放与前缀注销:{busy} 个导入任务在途(随任务结束/进程退出回收)")
        return
    sdk_graph.reset_graph_store_cache()
    # 只清本 pack 前缀的槽——清全部会拆掉 chatbi 等其他 pack 的活连接
    sdk_vector.reset_vector_store_cache(VECTOR_PREFIX)
    unregister_prefix("kg", owner=PACK_NAME)
