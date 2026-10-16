"""知识图谱插件的依赖探针(由平台 pack_dependency 懒加载调用)。

签名约定:fn(resolved_settings: dict) -> None;抛异常 = 探针失败。
拿到的是本插件全部配置的最终值(设置页保存值 > env > 默认)。

探针实现各自 import 驱动库(neo4j / psycopg)——这两个依赖只属于本插件,
平台的 requirements 里也由本插件引入。

【超时纪律】探针必须自带短超时:平台的 future.result(timeout=3) 只放弃
等待、杀不掉本线程;若依赖网络栈按自家默认超时(如 neo4j 驱动连接超时
30s)挂住,探针线程会长期占用执行器并拖慢/误导后续探测。这里统一把
连接超时压到 2s——比平台外层 3s 超时更紧,保证"超时返回"而不是"挂死"。
"""
import os

_PROBE_CONNECT_TIMEOUT = float(os.getenv("KG_PROBE_CONNECT_TIMEOUT", "2"))


def neo4j(settings: dict) -> None:
    """Neo4j 连通性:driver.verify_connectivity(bolt 握手 + 鉴权)。"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.get("neo4j_uri"),
        auth=(settings.get("neo4j_user") or "neo4j",
              settings.get("neo4j_password") or ""),
        connection_timeout=_PROBE_CONNECT_TIMEOUT,
        max_connection_pool_size=1,
    )
    try:
        driver.verify_connectivity()
    finally:
        driver.close()


def pg_vector(settings: dict) -> None:
    """PG 向量后端连通性(VECTOR_BACKEND=pg 默认):SELECT 1 + pgvector 扩展在。

    走 DATABASE_URL(与业务库同实例——向量表建在 public schema);
    扩展缺失给明确错误(需 DBA: CREATE EXTENSION vector)。
    """
    import os
    import psycopg

    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL 未配置——PG 向量后端必填")
    with psycopg.connect(dsn, connect_timeout=_PROBE_CONNECT_TIMEOUT) as conn:
        row = conn.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if not row:
            raise RuntimeError(
                "pgvector 扩展未安装——请执行 CREATE EXTENSION vector;"
                "镜像需含 pgvector(如 pgvector/pgvector:pg16)")
