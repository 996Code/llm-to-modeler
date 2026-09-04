"""知识图谱插件的依赖探针(由平台 pack_dependency 懒加载调用)。

签名约定:fn(resolved_settings: dict) -> None;抛异常 = 探针失败。
拿到的是本插件全部配置的最终值(设置页保存值 > env > 默认)。

探针实现各自 import 驱动库(neo4j / pymilvus)——这两个依赖只属于本插件,
平台的 requirements 里也由本插件引入。
"""


def neo4j(settings: dict) -> None:
    """Neo4j 连通性:driver.verify_connectivity(bolt 握手 + 鉴权)。"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.get("neo4j_uri"),
        auth=(settings.get("neo4j_user") or "neo4j",
              settings.get("neo4j_password") or ""),
    )
    try:
        driver.verify_connectivity()
    finally:
        driver.close()


def milvus(settings: dict) -> None:
    """Milvus 连通性:列 collection(轻量 RPC,兼作鉴权验证)。"""
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=settings.get("milvus_uri"),
        user=settings.get("milvus_user") or None,
        password=settings.get("milvus_password") or None,
    )
    try:
        client.list_collections()
    finally:
        client.close()
