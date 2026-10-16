"""chatbi 依赖探针(manifest dependencies.probe 声明;独立短连接,不共享池)。"""
import logging

logger = logging.getLogger(__name__)

_PROBE_CONNECT_TIMEOUT = 3


def pg_vector(fields: dict) -> bool:
    """PG 向量后端连通性(语义层向量检索;pgvector 单后端)。

    走 DATABASE_URL(与平台存储同实例);扩展缺失给明确错误
    (需 DBA: CREATE EXTENSION vector)。
    """
    import os
    import psycopg

    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        return False
    try:
        with psycopg.connect(dsn, connect_timeout=_PROBE_CONNECT_TIMEOUT) as conn:
            row = conn.execute(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            if not row:
                raise RuntimeError("pgvector 扩展未安装(需 DBA: CREATE EXTENSION vector)")
        return True
    except Exception as e:
        logger.warning("chatbi pg_vector 探针失败: %s", e)
        return False
