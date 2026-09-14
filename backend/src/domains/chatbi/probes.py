"""chatbi 依赖探针(manifest dependencies.probe 声明;独立短连接,不共享池)。"""
import logging

logger = logging.getLogger(__name__)


def milvus(fields: dict) -> bool:
    """Milvus 连通性探针(向量检索;配置解析链: 设置页 > env > 默认)。"""
    uri = (fields.get("milvus_uri") or "").strip()
    if not uri:
        return False
    try:
        from pymilvus import connections
        connections.connect(alias="chatbi_probe", uri=uri, timeout=3)
        connections.disconnect(alias="chatbi_probe")
        return True
    except Exception as e:
        logger.warning("chatbi milvus 探针失败: %s", e)
        return False
