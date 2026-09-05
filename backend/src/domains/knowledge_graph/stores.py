"""knowledge_graph 存储适配层 —— 插件设置 → SDK 存储单例。

【职责】
SDK 图/向量存储(graph_store/vector_store)的调用参数由调用方注入;
本模块把"插件设置解析链"与"SDK 构造"接在一起,给 runtime/pack 提供
三个稳定入口。KG 插件的存储命名空间前缀在这里声明并登记:

  - Neo4j 约束/索引前缀 "kg_"(存量约束 kg_entity_key/kg_entity_name
    原样兼容,数据无缝);
  - Milvus collection 前缀 "kg"(存量 kg_{kb_id}_v1 原样兼容)。

前缀登记是幂等的(同 owner 重复登记合法),装配/热切换反复经过这里
不会炸;撞前缀(别的插件声明了 kg)会在装配期 fail-fast。
"""
from sdk import graph_store as sdk_graph
from sdk import vector_store as sdk_vector
from sdk.scope_registry import register_prefix, unregister_prefix

PACK_NAME = "knowledge_graph"
GRAPH_PREFIX = "kg_"     # Neo4j 约束/索引名前缀(与存量数据一致)
VECTOR_PREFIX = "kg"     # Milvus collection 名前缀(与存量数据一致)


def _ensure_prefix_registered() -> None:
    register_prefix("kg", owner=PACK_NAME)


def get_graph(app_state) -> sdk_graph.Neo4jGraphStore:
    """Neo4j 图存储单例(连接配置来自设置解析链;指纹含前缀)。"""
    from domains.knowledge_graph import runtime
    _ensure_prefix_registered()
    settings = runtime.settings_reader(app_state).all()
    return sdk_graph.get_graph_store(
        settings,
        prefix=GRAPH_PREFIX,
    )


def get_vector(app_state) -> sdk_vector.MilvusVectorStore:
    """Milvus 向量存储单例(同上)。"""
    from domains.knowledge_graph import runtime
    _ensure_prefix_registered()
    settings = runtime.settings_reader(app_state).all()
    return sdk_vector.get_vector_store(
        settings,
        collection_prefix=VECTOR_PREFIX,
    )


def reset_caches() -> None:
    """释放 SDK 存储连接(pack unload 钩子调用)。

    在途导入任务持有 driver 引用继续跑——粗暴 close 会让任务以
    "Driver closed" 失败。有在途任务时跳过释放(连接随进程退出回收,
    或下次 unload 再试);禁用插件的主要收益(路由下线/任务类型拒收)
    不受影响。
    """
    from domains.knowledge_graph import tasks as kg_tasks
    with kg_tasks._inflight_lock:
        busy = len(kg_tasks._inflight)
    if busy:
        # 连接与前缀登记都留给在途任务:在途任务仍持有 prefix="kg_" 的
        # store 在写数据,此刻注销前缀会打开抢注窗口(另一插件登记 kg,
        # 两边共用命名空间直到冲突在数据互踩后才暴露)。下次 unload 再清。
        import logging
        logging.getLogger(__name__).warning(
            f"unload 跳过连接释放与前缀注销:{busy} 个导入任务在途(随任务结束/进程退出回收)")
        return
    sdk_graph.reset_graph_store_cache()
    sdk_vector.reset_vector_store_cache()
    unregister_prefix("kg", owner=PACK_NAME)
