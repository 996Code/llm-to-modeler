"""chatbi 存储适配层 —— SDK 向量/关系存储单例 + Embedder 适配 + scope 登记。

【移植来源】自 chat-bi backend/app/services/ 移植的检索栈存储底座:
  - VectorRecord/SearchResult ← chat-bi vector_store.py 的数据结构(1:1 保留,
    调用方 indexing/retrieval/fewshot 维持源码形态);
  - Embedder 缓存/批处理逻辑 ← chat-bi embedder.py LocalEmbedder
    (文本→向量 LRU 缓存 + 空列表直返;加载逻辑被引擎 llm.embeddings 取代,
    EMBEDDING_BACKEND 由引擎 env 决定,pack 不再关心 local/api);
  - ChatBI 自己的 Mock/Milvus 封装**不移植**——SDK 已有等价设施
    (sdk.vector_store.MilvusVectorStore),本模块只做"记录模型 ↔ chunk 模型"适配。

【与源实现的物理映射】(语义等价,物理形态不同——差异点见各成员 docstring)
  - 源: 全部数据源共用一个 collection,靠 metadata.data_source_id 标量过滤;
  - 本仓库: **每个数据源一个 scope_id(UUID,登记在数据源行上)**,
    collection 名 = chatbi_{scope}_v1(物理隔离);源标量过滤 → scope 定位。
  - collection 内部用 doc_id 分区:DOC_SCHEMA(语义层索引) / DOC_FEWSHOT(few-shot),
    对应源实现的 semantic_models / fewshot 两个逻辑 collection。
  - 源 record id "<ds>:<type>:<name>" → chunk_id "type:name"(scope 已隔离
    数据源,ds 前缀冗余;且 SDK chunk_id 上限 64 字符,带 UUID 前缀会溢出)。
  - 源 metadata(question/sql) → few-shot 的 chunk text 存 JSON(SDK 无元数据列);
    同时持久化一份到 PG(chatbi_few_shot_examples)作管理/审计权威行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sdk import vector_store as sdk_vector
from sdk.relational_store import PackRelationalDB
from sdk.scope_registry import new_scope_id, register_prefix, unregister_prefix

logger = logging.getLogger(__name__)

PACK_NAME = "chatbi"
VECTOR_PREFIX = "chatbi"   # Milvus collection 名前缀(装配期经 register_prefix 登记)

# collection 内 doc_id 分区:语义层索引 / few-shot 示例
# (对应源实现的 semantic_models / fewshot 两个逻辑 collection)
DOC_SCHEMA = "schema"
DOC_FEWSHOT = "fewshot"


def _now() -> str:
    """ISO 时间戳(TEXT 列;与平台 Store 同惯例)。"""
    return datetime.now(timezone.utc).isoformat()


# ── 数据结构(自 chat-bi vector_store.py 1:1 移植)──────────────────

@dataclass
class VectorRecord:
    """一条向量记录: id + 向量 + 标量元数据 + 原文(可选)。

    对标 chat-bi VectorRecord: metadata 里放 data_source_id/type/name
    供适配层编码(chunk_id/标量过滤);text 为原文(调试/展示用)。
    """
    id: str
    vector: List[float]
    metadata: Dict[str, Any] = field(default_factory=dict)
    text: Optional[str] = None


@dataclass
class SearchResult:
    """检索结果: 记录 + 相似度分数(COSINE,越高越相似)。"""
    record: VectorRecord
    score: float


# ── Embedder 适配(自 chat-bi embedder.py 移植缓存/批处理逻辑)────────

class ChatBIEmbedder:
    """批量 + LRU 缓存的 embed 适配器,底座 = 引擎 llm.embeddings。

    移植自 chat-bi LocalEmbedder 的核心行为:
      - 空文本列表直接返回空(不触发底层调用);
      - 相同文本复用已编码向量(查询问题/few-shot 检索会反复 embed 同一问题),
        缓存满则清最早一半(源实现同款简单可靠策略);
      - 一次调用批量处理所有未缓存文本(源 build_index 的批量口径);
      - 维度缓存:首次 embed 后记录 dim(建 collection 时探测用),
        之前为 None。重试交给引擎限流/退避机制(chat-bi 亦无应用层重试)。
    """

    def __init__(self, llm: Any, stage: str = "chatbi.embed",
                 cache_max: int = 2048):
        self._llm = llm
        self._stage = stage
        self._cache: Dict[str, List[float]] = {}
        self._cache_max = cache_max
        self._dim: Optional[int] = None

    @property
    def dim(self) -> Optional[int]:
        """向量维度(首次 embed 后确定;None = 尚未探测)。"""
        return self._dim

    def embed(self, texts: List[str]) -> List[List[float]]:
        """文本 → 向量列表(顺序与输入一致)。失败上抛,由调用方降级。"""
        if not texts:
            return []

        # 分离已缓存 / 未缓存,只对未缓存文本出站(源实现同款)
        results: List[Optional[List[float]]] = [None] * len(texts)
        to_encode: List[str] = []
        to_encode_idx: List[int] = []
        for i, t in enumerate(texts):
            cached = self._cache.get(t)
            if cached is not None:
                results[i] = cached
            else:
                to_encode.append(t)
                to_encode_idx.append(i)

        if to_encode:
            encoded = self._llm.embeddings(to_encode, stage=self._stage)
            for idx, text, vec in zip(to_encode_idx, to_encode, encoded):
                if self._dim is None:
                    self._dim = len(vec)   # 维度缓存:首次探测后不再变
                results[idx] = vec
                # LRU 淘汰:缓存满则清最早一半(源实现同款,不依赖 OrderedDict)
                if len(self._cache) >= self._cache_max:
                    drop_count = self._cache_max // 2
                    for k in list(self._cache.keys())[:drop_count]:
                        del self._cache[k]
                self._cache[text] = list(vec)

        return results  # type: ignore[return-value]


# ── 向量存储适配(ChatBI 记录模型 ↔ SDK chunk 模型)──────────────────

class ChatBIVectorStore:
    """SDK MilvusVectorStore 的 ChatBI 适配壳。

    源实现的 store.upsert/search/delete_by_filter 三件套在本仓库的对应:
      - upsert(records)            → upsert_records(scope, records, doc_id)
        (chunk_id 由 metadata 的 type/name 编码,few-shot 用 rec.id;
         建库前按首个向量维度 ensure_collection,幂等)
      - search(query_vector, ...)  → search_records(scope, ...)
        (doc_id 分区取代 data_source_id 标量过滤;score_threshold 在
         适配层后置过滤——SDK search 无阈值参数,与源 Milvus 实现的
         "limit=top_k 后过滤"行为一致)
      - delete_by_filter({ds})     → delete_doc(scope, DOC_SCHEMA)
        (返回删除数量,供 rebuild_index 日志/验证;用 count 前后差值计算)
    """

    def __init__(self, sdk_store: Any):
        self._sdk = sdk_store

    def ping(self) -> None:
        """底层 Milvus 真实连通性探针(委托 SDK ping = list_collections)。

        健康端点用(四审 P1: 此前"适配对象可构造"就显示 ok——配置错了
        也绿, 假阳性)。
        """
        self._sdk.ping()

    @staticmethod
    def _chunk_id(rec: VectorRecord) -> str:
        """VectorRecord → SDK chunk_id(≤64 字符)。

        schema 记录: metadata 带 type/name → "type:name"
        (源 id 的 ds 前缀由 per-scope collection 隔离取代);
        few-shot 记录: rec.id 即 example_id(md5 hex,天然无冒号)。
        """
        rtype = rec.metadata.get("type")
        name = rec.metadata.get("name")
        if rtype and name:
            return f"{rtype}:{name}"
        return rec.id

    def upsert_records(self, scope: str, records: List[VectorRecord],
                       doc_id: str) -> int:
        """批量写入/覆盖(同 chunk_id 覆盖,幂等)。返回写入条数。"""
        if not records:
            return 0
        items = [{
            "chunk_id": self._chunk_id(rec),
            "doc_id": doc_id,
            "seq": seq,
            "text": rec.text or "",
            "vector": rec.vector,
        } for seq, rec in enumerate(records)]
        # 维度探测建 collection(幂等):dim 取首个向量,与建库时一致
        self._sdk.ensure_collection(scope, len(items[0]["vector"]))
        return self._sdk.upsert_chunks(scope, items)

    def search_records(self, scope: str, query_vector: List[float],
                       top_k: int = 20, score_threshold: float = 0.0,
                       doc_id: Optional[str] = None) -> List[SearchResult]:
        """相似度检索:SDK top_k → 适配层阈值过滤 → SearchResult 列表。

        chunk_id 反解码:含 ":" 视为 schema 记录("type:name"),
        否则(few-shot 的 md5 id)metadata 留空,由 fewshot 层解析 text JSON。
        """
        hits = self._sdk.search(scope, query_vector, top_k=top_k, doc_id=doc_id)
        out: List[SearchResult] = []
        for h in hits:
            score = float(h.get("score") or 0.0)
            if score < score_threshold:   # 阈值后置过滤(宁缺毋滥)
                continue
            chunk_id = str(h.get("chunkId") or "")
            if ":" in chunk_id:
                rtype, _, name = chunk_id.partition(":")
                metadata = {"type": rtype, "name": name}
            else:
                metadata = {}
            out.append(SearchResult(
                record=VectorRecord(
                    id=chunk_id,
                    vector=query_vector,   # search 不回向量,用查询向量占位(源实现同款)
                    metadata=metadata,
                    text=h.get("text") or "",
                ),
                score=score,
            ))
        return out

    def delete_doc(self, scope: str, doc_id: str) -> int:
        """删除某 doc 分区全部向量,返回删除数量(前后 count 差值)。"""
        before = self._sdk.count(scope)
        self._sdk.delete_by_doc(scope, doc_id)
        after = self._sdk.count(scope)
        return max(0, before - after)

    def count(self, scope: str) -> int:
        """collection 记录总数(测试/监控用)。"""
        return self._sdk.count(scope)


# ── pack 关系库(PG,few-shot 元数据 + scope 登记)───────────────────

# 本模块自有的 DDL(表名 chatbi_ 前缀;chatbi_data_sources/
# chatbi_semantic_models 的建表 DDL 在 models.py CHATBI_DDL,ensure_schema
# 一并执行保证幂等齐备)。
# 注意 chatbi_data_sources 按 models.py 旧 DDL 建过的话没有 scope_id 列,
# ALTER ... IF NOT EXISTS 兜底补列(幂等)。
CHATBI_RETRIEVAL_DDL = [
    "ALTER TABLE chatbi_data_sources "
    "ADD COLUMN IF NOT EXISTS scope_id TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_ds_scope "
    "ON chatbi_data_sources(scope_id)",
    """CREATE TABLE IF NOT EXISTS chatbi_few_shot_examples (
        id TEXT PRIMARY KEY,
        data_source_id TEXT NOT NULL,
        question TEXT NOT NULL,
        sql TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_fewshot_ds "
    "ON chatbi_few_shot_examples(data_source_id)",
]

# pack 关系库单例收敛到 runtime.get_pack_db(本模块不再持有实例)


def _ensure_prefix_registered() -> None:
    """存储命名前缀登记(幂等;撞前缀在装配期 fail-fast)。"""
    register_prefix(VECTOR_PREFIX, owner=PACK_NAME)


def get_db() -> PackRelationalDB:
    """pack 关系库单例(委托 runtime.get_pack_db, 单一实例单一建表点)。

    历史: 本函数曾自建 PackRelationalDB + 建检索栈表, 与 runtime._db 形成
    双单例(unload 只清其一)。现收敛为委托——全量 DDL 聚合在
    runtime._init_pack_schema, 卸载钩子清 runtime 一处即全清。
    """
    from domains.chatbi import runtime
    return runtime.get_pack_db()


# ── scope 登记(数据源行 ↔ 物理 collection 的映射)───────────────────

def get_or_create_scope(db: PackRelationalDB, data_source_id: str) -> str:
    """取/签发数据源的向量 scope_id(UUID,登记在数据源行上)。

    首次调用签发 new_scope_id() 并回写行(供后续检索/删除定位
    collection chatbi_{scope}_v1);重复调用幂等返回已登记值。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT scope_id FROM chatbi_data_sources WHERE id = ?",
            (data_source_id,),
        ).fetchone()
        if row is None:
            # 数据源行不存在 → 无处登记 scope(继续走会产出孤儿 collection,
            # 且下次调用重复签发导致索引"丢失"), fail-fast
            raise ValueError(f"数据源不存在, 无法签发 scope: {data_source_id!r}")
        if row.get("scope_id"):
            return row["scope_id"]
        scope = new_scope_id()
        conn.execute(
            "UPDATE chatbi_data_sources SET scope_id = ?, updated_at = ? "
            "WHERE id = ?",
            (scope, _now(), data_source_id),
        )
    logger.info("chatbi scope 已签发: ds=%s scope=%s",
                data_source_id[:8], scope[:8])
    return scope


def get_scope(db: PackRelationalDB, data_source_id: str) -> Optional[str]:
    """读数据源已登记的 scope_id(未登记/数据源不存在 → None)。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT scope_id FROM chatbi_data_sources WHERE id = ?",
            (data_source_id,),
        ).fetchone()
    return (row or {}).get("scope_id") or None


def list_scopes(db: PackRelationalDB) -> List[str]:
    """全部活跃数据源已登记的 scope_id(无 ds 检索的全量召回用)。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT scope_id FROM chatbi_data_sources "
            "WHERE is_active = 1 AND scope_id != ''",
        ).fetchall()
    return [r["scope_id"] for r in rows]


def resolve_scopes(db: Optional[PackRelationalDB],
                   data_source_id: Optional[str] = None,
                   scope: Optional[str] = None) -> List[str]:
    """检索的物理 scope 解析:显式 scope > 数据源行登记 > 全部活跃数据源。

    对应源实现的 filter 语义:
      data_source_id 非空 → 单源标量过滤(未登记 scope → 空列表,召回为空);
      data_source_id 为空 → 全量召回(源实现 filter=None 的跨源检索)。
    """
    if scope:
        return [scope]
    if db is None:
        raise ValueError(
            "无法定位向量 collection:需要显式 scope 参数或可用的 pack 关系库 db"
            "(用于解析数据源行上登记的 scope_id)")
    if data_source_id:
        s = get_scope(db, data_source_id)
        return [s] if s else []
    return list_scopes(db)


def clear_scope(db: PackRelationalDB, data_source_id: str) -> None:
    """解除数据源与 scope 的登记(数据源删除路径调用;物理 collection
    由 delete_data_source_storage 负责 drop)。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE chatbi_data_sources SET scope_id = '', updated_at = ? "
            "WHERE id = ?",
            (_now(), data_source_id),
        )


# ── 索引 revision namespace(十七审 7.7: 原子发布)─────────────────
# 构建方: 新内容先写入独立分区 doc_id="schema_r{version}", 全量成功后
# 单条 UPSERT 原子翻转 active 指针(仅更高 version 可翻转——旧任务晚完成
# 不覆盖新任务); 读者(retrieve)按 scope 读 active 分区。
# 构建中途崩溃 → 指针未动, 读者仍读旧分区; 残余分区是垃圾不影响正确性。

def ensure_index_revision_schema(db: PackRelationalDB) -> None:
    """幂等建 active 指针表(老部署升级路径;新部署由 CHATBI_DDL 覆盖)。"""
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chatbi_index_revisions ("
            "scope TEXT PRIMARY KEY, "
            "active_doc_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL)")


def get_active_doc_id(db: Optional[PackRelationalDB],
                      scope: str) -> Optional[str]:
    """读 scope 当前生效的语义索引分区 doc_id(无记录 → None, 调用方
    回退 legacy "schema" 分区)。任何异常按 None 处理(读路径不崩)。"""
    if db is None:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions "
                "WHERE scope = ?", (scope,)).fetchone()
        return (row or {}).get("active_doc_id") or None
    except Exception:
        return None


def set_active_doc_id(db: PackRelationalDB, scope: str, doc_id: str,
                      version: int) -> bool:
    """原子翻转 active 指针; 仅当 version 高于现存值才生效。

    Returns:
        True = 本次写入成为 active;False = 已有更新的 revision(晚完成的
        旧任务让路, 不覆盖)。调用方仅在 True 时清理旧分区。
    """
    ensure_index_revision_schema(db)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO chatbi_index_revisions "
            "(scope, active_doc_id, version, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope) DO UPDATE SET "
            "active_doc_id = EXCLUDED.active_doc_id, "
            "version = EXCLUDED.version, "
            "updated_at = EXCLUDED.updated_at "
            "WHERE EXCLUDED.version > chatbi_index_revisions.version",
            (scope, doc_id, version, _now()))
        # SQLite: 无匹配行时 rowcount 0(INSERT 成功为 1); PG 的 execute
        # rowcount 对 DO UPDATE WHERE 不命中同样报 0——两种驱动口径一致
        rowcount = getattr(cur, "rowcount", None)
        if rowcount:
            return True
        # rowcount 不可靠的驱动: 回读确认
        row = conn.execute(
            "SELECT active_doc_id FROM chatbi_index_revisions "
            "WHERE scope = ?", (scope,)).fetchone()
        return bool(row and row["active_doc_id"] == doc_id)


def delete_data_source_storage(db: PackRelationalDB, sdk_store: Any,
                               data_source_id: str) -> Optional[str]:
    """数据源删除的存储清理:drop 向量 collection + 删 few-shot 行 + 解除登记。

    Returns:
        被清理的 scope_id(数据源从未建索引 → None)。
    """
    scope = get_scope(db, data_source_id)
    if scope:
        try:
            sdk_store.drop_collection(scope)
        except Exception as e:   # 向量清理失败不阻塞元数据删除(孤儿 collection 可手工清)
            logger.warning("chatbi drop collection 失败 (scope=%s): %s",
                           scope[:8], e)
        # 十七审 7.7: 同步清 active 指针行(collection 已 drop, 残留指针
        # 会让读者去读已不存在的分区)
        try:
            with db.connect() as conn:
                conn.execute(
                    "DELETE FROM chatbi_index_revisions WHERE scope = ?",
                    (scope,))
        except Exception:
            pass
    delete_fewshot_examples(db, data_source_id)
    clear_scope(db, data_source_id)
    return scope


# ── few-shot 元数据的 PG 通道(权威行;向量侧只存检索所需 JSON)─────────

def save_fewshot_example(db: PackRelationalDB, example_id: str,
                         data_source_id: str, question: str, sql: str) -> None:
    """upsert 一条 few-shot 示例元数据(审核回流路径调用)。"""
    now = _now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_few_shot_examples "
            "(id, data_source_id, question, sql, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET "
            "question = EXCLUDED.question, sql = EXCLUDED.sql, "
            "updated_at = EXCLUDED.updated_at",
            (example_id, data_source_id, question, sql, now, now),
        )


def list_fewshot_examples(db: PackRelationalDB,
                          data_source_id: str) -> List[Dict[str, Any]]:
    """列某数据源的全部 few-shot 示例(管理端/审计用,按时间倒序)。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, data_source_id, question, sql, is_active, "
            "created_at, updated_at FROM chatbi_few_shot_examples "
            "WHERE data_source_id = ? ORDER BY created_at DESC",
            (data_source_id,),
        ).fetchall()
    return list(rows)


def delete_fewshot_examples(db: PackRelationalDB,
                            data_source_id: str) -> int:
    """按数据源删全部 few-shot 行(多租户删除;返回删除条数)。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM chatbi_few_shot_examples WHERE data_source_id = ?",
            (data_source_id,),
        ).fetchall()
        conn.execute(
            "DELETE FROM chatbi_few_shot_examples WHERE data_source_id = ?",
            (data_source_id,),
        )
    return len(rows)


# ── 装配入口(模式照抄 knowledge_graph/stores.py)────────────────────

def get_vector(app_state: Any) -> ChatBIVectorStore:
    """SDK 向量存储单例(适配壳;连接配置来自设置解析链,指纹含前缀)。"""
    from sdk.pack_api import settings_reader
    _ensure_prefix_registered()
    settings = settings_reader(app_state, PACK_NAME).all()
    return ChatBIVectorStore(
        sdk_vector.get_vector_store(settings, collection_prefix=VECTOR_PREFIX))


def get_embedder(llm: Any, stage: str = "chatbi.embed") -> ChatBIEmbedder:
    """Embedder 工厂(底层 = 引擎 llm.embeddings;无全局单例——llm 随调用方注入)。"""
    return ChatBIEmbedder(llm, stage=stage)


def reset_caches() -> None:
    """释放 SDK 存储连接与 pack 库单例(测试/pack unload 钩子调用)。

    pack 库单例已收敛到 runtime(stores.get_db 委托), 这里同步清 runtime
    一处即全清(双单例时期只清 stores._db 会漏 runtime._db)。
    """
    from domains.chatbi import runtime
    sdk_vector.reset_vector_store_cache()
    runtime.reset_runtime_cache()
    unregister_prefix(VECTOR_PREFIX, owner=PACK_NAME)
