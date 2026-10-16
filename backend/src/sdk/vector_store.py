"""SDK 通用向量存储 —— pgvector 单后端(PG 内向量列 + HNSW)。

【演进】向量存储曾为 Milvus(独立服务)→ N1 切 PG(pgvector)→ Milvus
服务下线后切换后端一并移除(死路径不留)。VECTOR_BACKEND env 仍被
读取但仅接受 "pg"(存量 .env 带旧值时启动报错提示,而非静默漂移)。

存储形态:每 scope 一张表 {prefix}_{scope 去 - 换 _}_v1
  (chunk_id 主键 / doc_id 分区 / seq / text / embedding VECTOR 列 + HNSW),
与 KG chunk / ChatBI 语义模型索引共用本 Protocol。

单测用内存 Fake,不要求真实 PG。
"""
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Protocol

from sdk.scope_registry import check_scope_id

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    """向量存储协议(鸭子类型)。方法集由 FakeVector 测试替身证明充分。

    scope_id 契约见模块 docstring——实现方应在入口校验。
    """

    def ensure_collection(self, scope: str, dim: int) -> None: ...
    def drop_collection(self, scope: str) -> None: ...
    def upsert_chunks(self, scope: str, items: List[Dict[str, Any]]) -> int: ...
    def delete_by_doc(self, scope: str, doc_id: str) -> None: ...
    def delete_by_ids(self, scope: str, chunk_ids: List[str]) -> None: ...
    def existing_chunk_ids(self, scope: str, doc_id: str) -> set: ...
    def search(self, scope: str, query_vector: List[float], top_k: int = 5,
               doc_id: Optional[str] = None) -> List[Dict[str, Any]]: ...
    def count(self, scope: str) -> int: ...
    def close(self) -> None: ...


def collection_name(collection_prefix: str, scope_id: str) -> str:
    """collection 名:{prefix}_{scope 的 - 转 _}_v1。"""
    return f"{collection_prefix}_{scope_id.replace('-', '_')}_v1"


# ── 进程级单例(设置热改时重建) ────────────────────────────────

_cached_stores: Dict[tuple, Any] = {}   # 指纹 → store(按前缀多槽)
_cache_lock = threading.Lock()     # 只保护缓存指针读写(锁内零网络 IO)
_build_lock = threading.Lock()     # 串行化连接构建

def resolve_vector_backend() -> str:
    """解析向量后端:仅 pg(Milvus 后端已随服务下线移除)。

    env 仍读取——存量 .env 带 VECTOR_BACKEND=milvus 时 fail-fast 报错
    指引改 pg,而非静默落回 pg 让"以为在用 Milvus"的部署继续蒙着跑。
    """
    backend = (os.getenv("VECTOR_BACKEND", "") or "pg").strip().lower()
    if backend != "pg":
        raise ValueError(
            f"未知 VECTOR_BACKEND: {backend!r}——向量存储现为 pgvector 单后端"
            "(Milvus 已下线),请将 .env 的 VECTOR_BACKEND 删除或设为 pg。")
    return "pg"


def backend_display_name() -> str:
    """向量后端展示名(分层治理 P0-5: 健康组件/观测键单一事实源)。

    前端健康面板与 chatbi health/detail 的向量组件键都由此派生——
    下次换向量后端只改本函数与实现,消费方零改。
    """
    return "pg"


def get_vector_store(settings: Dict[str, Any],
                     collection_prefix: str = "") -> Any:
    """按解析后的配置取/建向量存储单例(指纹 = backend + 连接参数 + 前缀)。

    后端固定 pg(pgvector),连接串取 settings["pg_vector_url"](逃生口,
    无 pack schema 字段时用)或 env DATABASE_URL。

    多 pack 各自的前缀并存(chatbi 与 knowledge_graph 同时活跃):单槽
    缓存会让两 pack 互踢——后到者 close 掉前者的连接,前者下一次调用
    报 ConnectionNotExist(曾经只在检索日志里看到"should create
    connection first"的根因)。按指纹各存一份,连接数 = 活跃前缀数(≤2);

    锁纪律/失败纪律同 graph_store.get_graph_store:命中路径轻锁,构建
    在锁外,构建失败清缓存不留坏连接。
    """
    resolve_vector_backend()  # 仅校验(非 pg 报错);后端固定 pg
    from sdk.pg_vector_store import PgVectorStore
    dsn = (settings.get("pg_vector_url")
           or os.getenv("DATABASE_URL", "") or "").strip()
    if not dsn:
        raise RuntimeError(
            "向量存储(pgvector)需要 PostgreSQL 连接串:请在 env 配置 "
            "DATABASE_URL(与平台存储同库,pgvector 扩展由建表时自动启用)。")
    fp = ("pg", dsn, collection_prefix)

    def build() -> Any:
        return PgVectorStore(dsn=dsn, collection_prefix=collection_prefix)

    with _cache_lock:
        hit = _cached_stores.get(fp)
        if hit is not None:
            return hit

    with _build_lock:
        with _cache_lock:
            hit = _cached_stores.get(fp)
            if hit is not None:
                return hit
        try:
            store = build()
        except Exception:
            with _cache_lock:
                _cached_stores.pop(fp, None)
            raise
        with _cache_lock:
            _cached_stores[fp] = store
        return store


def reset_vector_store_cache(prefix: Optional[str] = None) -> None:
    """清单例(测试辅助 / pack unload 钩子)。

    prefix=None 清全部前缀的槽(兼容旧测试全量用法);指定前缀只清
    该 pack 的槽——pack unload 钩子必须传各自前缀:多槽缓存下,清全部
    会让"禁用 KG"顺手拆掉 chatbi 的活连接(互踢 bug 的 unload 侧残留)。
    指纹元组末位是 collection_prefix, 据此匹配归属。
    """
    with _cache_lock:
        targets = [fp for fp in _cached_stores
                   if prefix is None or fp[-1] == prefix]
        for fp in targets:
            s = _cached_stores.pop(fp, None)
            if s is None:
                continue
            try:
                s.close()
            except Exception:
                pass


def probe_pg_vector(settings: Dict[str, Any]) -> None:
    """PG 向量后端连通性探针(分层治理 P0-1: 自两插件上提 SDK)。

    DSN 解析与 get_vector_store 同源(pg_vector_url 逃生口 > DATABASE_URL);
    扩展缺失给明确错误(需 DBA: CREATE EXTENSION vector)。抛异常=探针失败。
    """
    import os
    import psycopg

    dsn = (settings.get("pg_vector_url")
           or os.getenv("DATABASE_URL", "") or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL 未配置——pgvector 向量后端必填")
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        row = conn.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if not row:
            raise RuntimeError(
                "pgvector 扩展未安装(需 DBA: CREATE EXTENSION vector)")
