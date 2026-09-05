"""MilvusVectorStore —— SDK 通用向量存储(Milvus 2.4/2.5,pymilvus)。

【模块定位】
从 knowledge_graph 插件下沉的通用向量设施:按 scope 物理隔离的
collection 管理 + 向量写入/删除/相似度检索。零领域知识——只收
"文本 + 向量"对,不认识 embedding 模型/知识库/文档(embedding 由
调用方生成后传入;SDK 不依赖任何 LLM 配置,测试可用 Fake 向量)。

【数据模型】(每 scope 一个 collection,物理隔离)
  collection 名 = {collection_prefix}_{scope 转下划线}_v1
  字段: chunk_id VARCHAR(64) PK / doc_id VARCHAR / seq INT64 /
        text VARCHAR(65535) / vector FLOAT_VECTOR(dim)
  索引: HNSW(metric=COSINE, M=16, efConstruction=200)
  写后 flush——读己之写(检索立刻可见)。

【命名空间隔离(与 scope_registry 契约配套)】
  - collection_prefix:每个使用方插件声明自己的前缀并 register_prefix
    登记("kg"/"bi"/"rag"...),collection 名天然不撞;
  - scope_id 契约:所有方法首参必须是服务端签发的 UUID
    (scope_registry.new_scope_id() 或调用方自签),入口防御拒收
    用户输入直传。

【连接管理】进程级单例 + 指纹缓存(含前缀),双检锁纪律同 graph_store。

【测试替身】VectorStore Protocol 声明了替身需实现的完整方法集;
单测用内存 Fake,不要求真实 Milvus。
"""
import logging
import threading
from typing import Any, Dict, List, Optional, Protocol

from sdk.scope_registry import is_scope_id_safe

logger = logging.getLogger(__name__)

# collection 字段(MilvusClient quick-setup 固定骨架 + 标量字段由 schema 定义)
_VECTOR_FIELD = "vector"
_ID_FIELD = "chunk_id"
_MAX_TEXT_LEN = 65535  # VARCHAR 上限(Milvus 2.4)


class VectorStore(Protocol):
    """向量存储协议(鸭子类型)。方法集由 FakeVector 测试替身证明充分。

    scope_id 契约见模块 docstring——实现方应在入口校验。
    """

    def ensure_collection(self, scope: str, dim: int) -> None: ...
    def drop_collection(self, scope: str) -> None: ...
    def upsert_chunks(self, scope: str, items: List[Dict[str, Any]]) -> int: ...
    def delete_by_doc(self, scope: str, doc_id: str) -> None: ...
    def search(self, scope: str, query_vector: List[float], top_k: int = 5,
               doc_id: Optional[str] = None) -> List[Dict[str, Any]]: ...
    def count(self, scope: str) -> int: ...
    def close(self) -> None: ...


def collection_name(collection_prefix: str, scope_id: str) -> str:
    """collection 名:{prefix}_{scope 的 - 转 _}_v1(与存量 kg_* 规则一致)。"""
    return f"{collection_prefix}_{scope_id.replace('-', '_')}_v1"


class MilvusVectorStore:
    """每 scope 一个 collection 的向量存取。

    Args:
        collection_prefix: collection 名前缀(命名空间边界;调用方需先
            在 scope_registry 登记该前缀)。
    """

    def __init__(self, uri: str, user: str = "", password: str = "",
                 collection_prefix: str = "kg"):
        from pymilvus import MilvusClient
        self._client = MilvusClient(
            uri=uri, user=user or None, password=password or None,
        )
        self._prefix = collection_prefix

    def _check_scope(self, scope_id: str) -> None:
        """scope_id 契约入口防御(同 graph_store,详见 scope_registry)。"""
        if not is_scope_id_safe(scope_id):
            raise ValueError(f"非法 scope_id(必须为服务端签发的 UUID): {scope_id!r}")

    def _name(self, kb_id: str) -> str:
        return collection_name(self._prefix, kb_id)

    # ── 生命周期 ───────────────────────────────────────────

    def ping(self) -> None:
        self._client.list_collections()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.warning("milvus client close failed", exc_info=True)

    # ── collection 管理 ────────────────────────────────────

    def ensure_collection(self, kb_id: str, dim: int) -> None:
        """建库 collection(幂等)。dim 来自建库时的一次 embedding 探测。"""
        self._check_scope(kb_id)
        name = self._name(kb_id)
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
        self._check_scope(kb_id)
        name = self._name(kb_id)
        if self._client.has_collection(name):
            self._client.drop_collection(name)
            logger.info(f"milvus collection dropped: {name}")

    # ── 数据操作 ───────────────────────────────────────────

    def upsert_chunks(self, kb_id: str, items: List[Dict[str, Any]]) -> int:
        """写入/覆盖 chunk 向量。items: [{chunk_id, doc_id, seq, text, vector}]。"""
        self._check_scope(kb_id)
        if not items:
            return 0
        name = self._name(kb_id)
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
        self._check_scope(kb_id)
        name = self._name(kb_id)
        self._client.delete(collection_name=name, filter=f'doc_id == "{doc_id}"')
        try:
            self._client.flush(collection_name=name)
        except Exception:
            logger.warning("milvus flush failed after delete", exc_info=True)

    def search(
        self, kb_id: str, query_vector: List[float], top_k: int = 5,
        doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """相似度检索(COSINE,分数越高越相似)。"""
        self._check_scope(kb_id)
        expr = f'doc_id == "{doc_id}"' if doc_id else None
        results = self._client.search(
            collection_name=self._name(kb_id),
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
        self._check_scope(kb_id)
        name = self._name(kb_id)
        if not self._client.has_collection(name):
            return 0
        stats = self._client.get_collection_stats(name)
        return int(stats.get("row_count") or 0)


# ── 进程级单例(设置热改时重建) ────────────────────────────────

_cached_store: Optional[MilvusVectorStore] = None
_cached_fp: tuple = ()
_cache_lock = threading.Lock()     # 只保护缓存指针读写(锁内零网络 IO)
_build_lock = threading.Lock()     # 串行化连接构建


def get_vector_store(settings: Dict[str, Any],
                     collection_prefix: str = "kg") -> MilvusVectorStore:
    """按解析后的配置取/建向量存储单例(指纹 = 连接三元组 + 前缀)。

    锁纪律/失败纪律同 graph_store.get_graph_store:命中路径轻锁,构建
    在锁外,构建失败清缓存不留坏连接。
    """
    global _cached_store, _cached_fp
    fp = (settings.get("milvus_uri"), settings.get("milvus_user"),
          settings.get("milvus_password"), collection_prefix)
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
