"""MilvusVectorStore - 知识图谱插件的向量存储(每知识库一个 collection)。

【隔离模型】
  collection 名 = kg_{kb_id}_v1 —— 删库即 drop,库与库之间物理隔离;
  维度建库时探测一次记入 kg_knowledge_bases(换 embedding 模型 = 换维度,
  重建库或重新导入)。

【与 embedder 的关系】
  本类只管向量存取,不负责向量化;构造时注入 embedder(可调用对象
  texts -> List[List[float]],M4 接 LLMClient.embeddings)。
  vector_enabled=False 的库(未配置 embedding 模型)完全跳过本层,
  降级为纯图谱检索。

【pymilvus 版本】本机 Milvus 2.4 服务端 → pymilvus 2.4/2.5 系客户端
  (MilvusClient quick-setup API:create_collection/upsert/search/delete)。
"""
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# collection 字段(MilvusClient quick-setup 固定骨架 + 标量字段由 schema 定义)
_VECTOR_FIELD = "vector"
_ID_FIELD = "chunk_id"
_MAX_TEXT_LEN = 65535  # VARCHAR 上限(Milvus 2.4)


def collection_name(kb_id: str) -> str:
    return f"kg_{kb_id.replace('-', '_')}_v1"


class MilvusVectorStore:
    """每库一个 collection 的向量存取。"""

    def __init__(self, uri: str, user: str = "", password: str = ""):
        from pymilvus import MilvusClient
        self._client = MilvusClient(
            uri=uri, user=user or None, password=password or None,
        )

    # ── 生命周期 ───────────────────────────────────────────

    def ping(self) -> None:
        self._client.list_collections()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.warning("milvus client close failed", exc_info=True)

    # ── collection 管理 ────────────────────────────────────

    def has_collection(self, kb_id: str) -> bool:
        return self._client.has_collection(collection_name(kb_id))

    def ensure_collection(self, kb_id: str, dim: int) -> None:
        """建库 collection(幂等)。dim 来自建库时的一次 embedding 探测。"""
        name = collection_name(kb_id)
        if self._client.has_collection(name):
            return
        from pymilvus import DataType
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(_ID_FIELD, DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("seq", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=_MAX_TEXT_LEN)
        schema.add_field(_VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=dim)
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name=_VECTOR_FIELD, index_type="HNSW",
            metric_type="COSINE", params={"M": 16, "efConstruction": 200},
        )
        self._client.create_collection(
            collection_name=name, schema=schema, index_params=index_params,
        )
        logger.info(f"milvus collection created: {name} (dim={dim})")

    def drop_collection(self, kb_id: str) -> None:
        name = collection_name(kb_id)
        if self._client.has_collection(name):
            self._client.drop_collection(name)
            logger.info(f"milvus collection dropped: {name}")

    # ── 数据操作 ───────────────────────────────────────────

    def upsert_chunks(self, kb_id: str, items: List[Dict[str, Any]]) -> int:
        """写入/覆盖 chunk 向量。items: [{chunk_id, doc_id, seq, text, vector}]。"""
        if not items:
            return 0
        name = collection_name(kb_id)
        # pymilvus MilvusClient.upsert 的参数名是 data(2.4/2.5 一致)
        rows = [{
            _ID_FIELD: it["chunk_id"],
            "doc_id": it["doc_id"],
            "seq": int(it.get("seq") or 0),
            "text": str(it["text"])[:_MAX_TEXT_LEN],
            _VECTOR_FIELD: it["vector"],
        } for it in items]
        self._client.upsert(collection_name=name, data=rows)
        # flush 让写入立刻可见(search/stats 一致性;单文档块数量小,代价可忽略)
        try:
            self._client.flush(collection_name=name)
        except Exception:
            logger.warning("milvus flush failed(数据最终一致,不影响导入)", exc_info=True)
        return len(rows)

    def delete_by_doc(self, kb_id: str, doc_id: str) -> None:
        """删除某文档的全部向量(重导入清理路径)。删除后 flush,防旧数据复现。"""
        name = collection_name(kb_id)
        self._client.delete(collection_name=name, filter=f'doc_id == "{doc_id}"')
        try:
            self._client.flush(collection_name=name)
        except Exception:
            logger.warning("milvus flush failed after delete", exc_info=True)

    def delete_by_chunks(self, kb_id: str, chunk_ids: List[str]) -> None:
        if not chunk_ids:
            return
        ids = ",".join(f'"{c}"' for c in chunk_ids)
        self._client.delete(
            collection_name=collection_name(kb_id),
            filter=f"{_ID_FIELD} in [{ids}]",
        )

    def search(
        self, kb_id: str, query_vector: List[float], top_k: int = 5,
        doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """相似度检索(COSINE,分数越高越相似)。"""
        expr = f'doc_id == "{doc_id}"' if doc_id else None
        results = self._client.search(
            collection_name=collection_name(kb_id),
            data=[query_vector],
            limit=top_k,
            filter=expr,
            output_fields=["doc_id", "seq", "text"],
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        )
        hits: List[Dict[str, Any]] = []
        for result in (results or []):
            for hit in result:
                entity = hit.get("entity") or {}
                hits.append({
                    "chunkId": hit.get("id"),
                    "docId": entity.get("doc_id"),
                    "seq": entity.get("seq"),
                    "text": entity.get("text") or "",
                    "score": hit.get("distance"),
                })
        return hits

    def count(self, kb_id: str) -> int:
        name = collection_name(kb_id)
        if not self._client.has_collection(name):
            return 0
        stats = self._client.get_collection_stats(name)
        return int(stats.get("row_count") or 0)


# ── 进程级单例(设置热改时重建) ────────────────────────────────

_cached_store: Optional[MilvusVectorStore] = None
_cached_fp: tuple = ()
_cache_lock = threading.Lock()     # 只保护缓存指针读写(锁内零网络 IO)
_build_lock = threading.Lock()     # 串行化连接构建


def get_vector_store(settings: Dict[str, Any]) -> MilvusVectorStore:
    """按解析后的插件配置取/建向量存储单例(指纹 = 连接三元组)。

    锁纪律/失败纪律同 graph_store.get_graph_store:命中路径轻锁,构建
    在锁外,构建失败清缓存不留坏连接。
    """
    global _cached_store, _cached_fp
    fp = (settings.get("milvus_uri"), settings.get("milvus_user"), settings.get("milvus_password"))
    with _cache_lock:
        if _cached_store is not None and _cached_fp == fp:
            return _cached_store

    with _build_lock:
        with _cache_lock:
            if _cached_store is not None and _cached_fp == fp:
                return _cached_store
        try:
            store = MilvusVectorStore(uri=fp[0], user=fp[1] or "", password=fp[2] or "")
        except Exception:
            with _cache_lock:
                if _cached_store is not None:
                    try:
                        _cached_store.close()
                    except Exception:
                        pass
                _cached_store, _cached_fp = None, ()
            raise
        old = None
        with _cache_lock:
            old = _cached_store
            _cached_store, _cached_fp = store, fp
        if old is not None and old is not store:
            try:
                old.close()
            except Exception:
                pass
        return store


def reset_vector_store_cache() -> None:
    """测试辅助:清单例。"""
    global _cached_store, _cached_fp
    with _cache_lock:
        if _cached_store is not None:
            _cached_store.close()
        _cached_store, _cached_fp = None, ()
