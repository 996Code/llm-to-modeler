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
import uuid
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
        """VectorRecord → SDK chunk_id(物理主键, ≤64 字符)。

        十八审 P0-2/P1: chunk_id 是 Milvus 全局主键, doc_id 只是过滤字段——
        主键必须包含 revision 与 metric 的 owner model, 否则:
          - 新 revision upsert 直接覆盖旧 active 行(隔离不存在);
          - 跨表同名 metric 相互覆盖(121 表库 312 实例仅 190 唯一名)。

        格式:
          model:   model:{name}[@rN]
          metric:  metric:{owner_model}:{name}[@rN]   (owner 进键)
          few-shot: rec.id(md5 hex, 无冒号)
        超长: 截断 name 尾部 + 8 位短哈希保唯一(解码仍能还原 type/owner,
        name 退化为前缀+哈希——极少见, 检索主通道 text 不受影响)。
        """
        rtype = rec.metadata.get("type")
        name = rec.metadata.get("name")
        if not (rtype and name):
            return rec.id
        owner = rec.metadata.get("owner_model")
        rev = rec.metadata.get("rev")
        parts = [rtype]
        if rtype == "metric":
            parts.append(owner or "_")
        parts.append(name)
        cid = ":".join(parts)
        if rev:
            cid = f"{cid}@r{rev}"
        if len(cid) > 64:
            import hashlib
            digest = hashlib.sha256(cid.encode()).hexdigest()[:16]
            cid = f"{cid[:47]}#{digest}"
        return cid

    @staticmethod
    def decode_chunk_id(chunk_id: str) -> Dict[str, Any]:
        """chunk_id → metadata(检索命中反解; 与 _chunk_id 互逆)。

        兼容三种形态: 新格式(带 @rN / metric 三段)、legacy 两段、
        few-shot 无冒号(返回空 metadata, 由 fewshot 层解析 text)。
        """
        if ":" not in chunk_id:
            return {}
        core, sep, rev = chunk_id.rpartition("@")
        if not sep:              # rpartition 无匹配时整串落第三段, 先判 sep
            core, rev = chunk_id, ""
        parts = core.split(":")
        meta: Dict[str, Any] = {"type": parts[0]}
        if chunk_id.startswith("metric:") and len(parts) >= 3:
            meta["owner_model"] = parts[1]
            meta["name"] = ":".join(parts[2:])
        else:
            meta["name"] = ":".join(parts[1:])
        if rev.startswith("r"):
            meta["rev"] = rev
        return meta

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
            metadata = self.decode_chunk_id(chunk_id)
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
    """幂等建 active 指针表 + builds 台账 + chunk 身份表(老部署升级路径)。"""
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chatbi_index_revisions ("
            "scope TEXT PRIMARY KEY, "
            "active_doc_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chatbi_index_builds ("
            "scope TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "doc_id TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'building', "
            "updated_at TEXT NOT NULL, "
            "PRIMARY KEY (scope, version))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chatbi_chunk_identities ("
            "scope TEXT NOT NULL, "
            "chunk_id TEXT NOT NULL, "
            "doc_id TEXT NOT NULL, "
            "type TEXT NOT NULL, "
            "name TEXT NOT NULL, "
            "owner_model TEXT, "
            "revision INTEGER, "
            "PRIMARY KEY (scope, chunk_id))")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chatbi_chunk_ident_doc "
            "ON chatbi_chunk_identities(scope, doc_id)")


def _ensure_identity_revision_column(db: PackRelationalDB) -> None:
    """身份表 revision 列迁移(二十审 9.7)——独立事务+预检。

    同 chatbi.tasks.ensure_lease_token_column 的教训: ALTER 在列已存在
    时报错会毒化所在事务, 吞异常后同事务后续语句全部 aborted; 预检
    information_schema 替代 try/except。
    """
    try:
        with db.connect() as conn:
            has = conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'chatbi_chunk_identities' "
                "AND column_name = 'revision'").fetchone()
            if not has:
                conn.execute("ALTER TABLE chatbi_chunk_identities "
                             "ADD COLUMN revision INTEGER")
    except Exception as e:
        logger.warning("身份表 revision 列迁移失败: %s", e)


def register_chunk_identities(db: Optional[PackRelationalDB], scope: str,
                              records: List[VectorRecord], doc_id: str,
                              revision: Optional[int] = None
                              ) -> tuple[bool, list]:
    """索引写入时登记 chunk → 业务身份(十九审 6.1 的身份真源)。

    二十审 9.1: 身份表是强真源——登记失败不再被调用方吞掉:
      Returns:
        (ok, truncated_ids): ok=False 表示登记抛错(调用方必须使构建失败,
        禁止发布含截断主键的索引); truncated_ids 是发生截断的 chunk_id
        (这些键不可逆, 检索侧必须依赖身份表, 不允许反解猜测)。

    失败告警保留(诊断), 但不再静默吞错。
    """
    if db is None or not records:
        # 无 db: 短键身份可由主键可逆解码恢复, 允许发布; 但含截断键时
        # 身份无处登记 → 禁止(检索将无法恢复其身份)
        has_trunc = any("#" in ChatBIVectorStore._chunk_id(r)
                        for r in records) if records else False
        return (not has_trunc), []
    truncated: list = []
    try:
        ensure_index_revision_schema(db)
        _ensure_identity_revision_column(db)
        with db.connect() as conn:
            for rec in records:
                rtype = rec.metadata.get("type")
                name = rec.metadata.get("name")
                if not (rtype and name):
                    continue
                cid = ChatBIVectorStore._chunk_id(rec)
                if "#" in cid:      # 截断标记(_chunk_id 的短哈希后缀)
                    truncated.append(cid)
                conn.execute(
                    "INSERT INTO chatbi_chunk_identities "
                    "(scope, chunk_id, doc_id, type, name, owner_model, revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (scope, chunk_id) DO UPDATE SET "
                    "doc_id = EXCLUDED.doc_id, type = EXCLUDED.type, "
                    "name = EXCLUDED.name, owner_model = EXCLUDED.owner_model, "
                    "revision = EXCLUDED.revision",
                    (scope, cid, doc_id, rtype, name,
                     rec.metadata.get("owner_model"), revision))
        return True, truncated
    except Exception as e:
        logger.warning("chunk 身份登记失败(scope=%s): %s", scope[:8], e)
        return False, truncated


def lookup_chunk_identities(db: Optional[PackRelationalDB], scope: str,
                            chunk_ids: List[str]) -> Dict[str, dict]:
    """批量反查 chunk 身份(检索命中回填 metadata 用)。

    二十审 9.1/9.7: 返回值区分"查到的身份行"; 检索侧对**截断键**缺失
    身份行必须丢弃命中(fail-closed), 不允许用截断主键反解猜测——
    判断依据由调用方完成(本函数只忠实返回"有没有")。失败抛给调用方,
    不再静默返回空(与登记同口径: 身份链路故障必须可见)。
    """
    if db is None or not chunk_ids:
        return {}
    with db.connect() as conn:
        out: Dict[str, dict] = {}
        # chunk_ids 来自单次召回(top_k ≤ 100 量级), 分批 IN 查询
        for i in range(0, len(chunk_ids), 200):
            batch = chunk_ids[i:i + 200]
            marks = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT chunk_id, type, name, owner_model, revision "
                f"FROM chatbi_chunk_identities "
                f"WHERE scope = ? AND chunk_id IN ({marks})",
                [scope, *batch]).fetchall()
            for r in rows:
                meta = {"type": r["type"], "name": r["name"]}
                if r["owner_model"]:
                    meta["owner_model"] = r["owner_model"]
                if r["revision"] is not None:
                    meta["rev"] = f"r{int(r['revision'])}"
                out[r["chunk_id"]] = meta
        return out


def delete_chunk_identities(db: Optional[PackRelationalDB], scope: str,
                            doc_id: Optional[str] = None) -> None:
    """清理 chunk 身份行(GC 按分区删; 数据源删除按整 scope 删)。"""
    if db is None:
        return
    try:
        with db.connect() as conn:
            if doc_id is None:
                conn.execute(
                    "DELETE FROM chatbi_chunk_identities WHERE scope = ?",
                    (scope,))
            else:
                conn.execute(
                    "DELETE FROM chatbi_chunk_identities "
                    "WHERE scope = ? AND doc_id = ?", (scope, doc_id))
    except Exception as e:
        logger.warning("chunk 身份清理失败(scope=%s): %s", scope[:8], e)


def get_active_doc_id(db: Optional[PackRelationalDB],
                      scope: str) -> Optional[str]:
    """读 scope 当前生效的语义索引分区 doc_id(无记录 → None, 调用方
    回退 legacy "schema" 分区)。

    十八审 6.7: 表不存在(未迁移老部署)按 None 处理; 其它数据库异常
    上抛——读方必须区分"没有指针"与"读不到指针", 不得静默回退旧分区
    伪装成未迁移。
    """
    if db is None:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions "
                "WHERE scope = ?", (scope,)).fetchone()
        return (row or {}).get("active_doc_id") or None
    except Exception as e:
        msg = str(e).lower()
        if ("does not exist" in msg or "undefined table" in msg
                or "no such table" in msg):
            return None
        raise


def _set_active_on_conn(conn, scope: str, doc_id: str, version: int) -> bool:
    """指针翻转核心(指定连接; 供 fenced 发布在同一租约事务内执行)。"""
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
    rowcount = getattr(cur, "rowcount", None)
    if rowcount:
        return True
    row = conn.execute(
        "SELECT active_doc_id FROM chatbi_index_revisions "
        "WHERE scope = ?", (scope,)).fetchone()
    return bool(row and row["active_doc_id"] == doc_id)


def set_active_doc_id(db: PackRelationalDB, scope: str, doc_id: str,
                      version: int) -> bool:
    """原子翻转 active 指针; 仅当 version 高于现存值才生效。

    Returns:
        True = 本次写入成为 active;False = 已有更新的 revision(晚完成的
        旧任务让路, 不覆盖)。调用方仅在 True 时清理旧分区。
    """
    ensure_index_revision_schema(db)
    with db.connect() as conn:
        return _set_active_on_conn(conn, scope, doc_id, version)


def record_index_build(db: PackRelationalDB, scope: str, version: int,
                       doc_id: str, status: str,
                       build_id: str | None = None) -> None:
    """记录/更新一次索引构建(building → published/yielded)。

    GC 的台账: 让路、崩溃、半成品的分区都按 (scope, version) 记录,
    延迟回收时按 doc_id 删向量; 失败上抛由调用方降级。
    二十审 9.3: updated_at 由数据库 now() 生成(与 GC 比较同源),
    不再用工作进程本地时钟——时钟漂移不再影响宽限/TTL 判定。
    """
    ensure_index_revision_schema(db)
    build_id = build_id or f"{version}-{uuid.uuid4().hex[:8]}"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_index_builds "
            "(scope, version, doc_id, status, updated_at) "
            "VALUES (?, ?, ?, ?, to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF')) "
            "ON CONFLICT (scope, version) DO UPDATE SET "
            "doc_id = EXCLUDED.doc_id, status = EXCLUDED.status, "
            "updated_at = EXCLUDED.updated_at",
            (scope, version, doc_id, status))
        # 二十四审 7: append-only 事件流——(scope,version) 状态表 UPSERT
        # 折叠同版本重复重建, 事件表让每次 started/published/yielded
        # 各留一条(同版本×10 重建 = 10 条事件, 不再被折叠成 1 行)
        try:
            conn.execute(
                "INSERT INTO chatbi_index_build_events "
                "(scope, build_id, version, event, created_at) "
                "VALUES (?, ?, ?, ?, "
                "to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF'))",
                (scope, build_id, version, status))
        except Exception as e:
            logger.warning("构建事件写入失败(监控计数将缺失): %s", e)


def _ensure_build_ledger(db: PackRelationalDB, scope: str, version: int,
                         doc_id: str, status: str) -> None:
    """幂等补台账行(不存在才插入; 不覆盖已有状态)。

    十九审 6.5: 升级前已有 active 指针(如 schema_r17)但无台账行——
    首次新式发布时按指针回填, 否则旧 revision 向量永远无法被 GC。
    时间同 record_index_build 用数据库时钟(二十审 9.3)。
    """
    ensure_index_revision_schema(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_index_builds "
            "(scope, version, doc_id, status, updated_at) "
            "VALUES (?, ?, ?, ?, to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF')) "
            "ON CONFLICT (scope, version) DO NOTHING",
            (scope, version, doc_id, status))


def gc_index_builds(db: PackRelationalDB, scope: str, store: Any,
                    keep_generations: int = 2,
                    published_grace_seconds: int = 600,
                    unfinished_ttl_seconds: int = 86400) -> int:
    """回收过旧分区(十九审 6.3 重设计)。

    保留集合 = active 分区 ∪ 最近 keep_generations 个 status='published'
    的成功代际——按台账成功记录算代际, 不按版本号减法(失败版本造成
    间隔时, 旧算法会删掉最后一个成功旧代、留下失败半成品)。

    删除条件(时间宽限, 数据库时钟):
      - 保留集之外 published: updated_at 早于 now-grace 才删——刚翻出
        保留窗的分区, 给已读到旧指针的在途查询一个真实时间窗;
      - building/yielded(崩溃/让路半成品): 早于 now-TTL 才删。
    时间基准取数据库 now()(不信任各 worker 本地时钟); 台账时间不可解析
    时宁可保留(不误删)。
    返回删除的向量条数。
    """
    ensure_index_revision_schema(db)
    with db.connect() as conn:
        active = conn.execute(
            "SELECT active_doc_id FROM chatbi_index_revisions "
            "WHERE scope = ?", (scope,)).fetchone()
        if not active:
            return 0
        active_doc = active["active_doc_id"]
        pub = conn.execute(
            "SELECT doc_id FROM chatbi_index_builds "
            "WHERE scope = ? AND status = 'published' "
            "ORDER BY version DESC LIMIT ?",
            (scope, keep_generations)).fetchall()
        keep = {active_doc, *(r["doc_id"] for r in pub)}
        now_row = conn.execute(
            "SELECT to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF') AS n"
        ).fetchone()
        candidates = conn.execute(
            "SELECT version, doc_id, status, updated_at FROM chatbi_index_builds "
            "WHERE scope = ?",
            (scope,)).fetchall()
        db_now = _parse_iso(now_row["n"] if now_row else None)
        victims = []
        for row in candidates:
            if row["doc_id"] in keep:
                continue
            ts = _parse_iso(row["updated_at"])
            if db_now is None or ts is None:
                continue   # 时钟不可得/时间不可解析 → 宁可留(不误删)
            age = (db_now - ts).total_seconds()
            limit = (unfinished_ttl_seconds
                     if row["status"] in ("building", "yielded")
                     else published_grace_seconds)
            if age >= limit:
                victims.append(row)
        deleted = 0
        for row in victims:
            try:
                deleted += store.delete_doc(scope, row["doc_id"])
                conn.execute(
                    "DELETE FROM chatbi_index_builds "
                    "WHERE scope = ? AND version = ?",
                    (scope, int(row["version"])))
                # 身份表同步清理(同分区不再可命中)
                delete_chunk_identities(db, scope, row["doc_id"])
            except Exception as e:
                logger.info("GC 分区 %s 失败(下次重试): %s", row["doc_id"], e)
        return deleted


def _parse_iso(value):
    """容错解析台账时间戳(ISO; 失败返回 None → 调用方宁可保留)。"""
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")
                                      .replace("+00", "+00:00")
                                      .replace("Z", "+00:00"))
    except Exception:
        return None


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
                conn.execute(
                    "DELETE FROM chatbi_index_builds WHERE scope = ?",
                    (scope,))
        except Exception:
            pass
        # 十九审 6.1: chunk 身份表按 scope 全清(collection 已不存在)
        delete_chunk_identities(db, scope, doc_id=None)
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
