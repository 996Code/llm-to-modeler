"""PgVectorStore —— SDK 通用向量存储(PostgreSQL + pgvector 扩展)。

【模块定位】
与 MilvusVectorStore 同构的 VectorStore Protocol 实现(N1:向量库从
Milvus 切换到 PG,VECTOR_BACKEND 默认 pg)。零领域知识铁律不变——
只收"文本 + 向量"对,不认识 embedding 模型/知识库/文档;psycopg 直连
DATABASE_URL,不 import services/domains(与 relational_store 的
依赖倒置不同:向量存储无共享池需求,自建小池自管生命周期)。

【数据模型】(每 scope 一张表,物理隔离;表名沿用 collection_name 规则)
  表名 = {collection_prefix}_{scope 的 - 转 _}_v1(小写+下划线,PG 安全
  标识符;入口正则校验——表名无法参数化,校验是注入的唯一防线)
  列:   chunk_id VARCHAR(64) PRIMARY KEY / doc_id VARCHAR(64) /
        seq BIGINT / text VARCHAR / embedding vector(dim)
  索引: HNSW(embedding vector_cosine_ops)——余弦距离检索

【检索语义(与 Milvus 适配层 COSINE 对齐)】
  pgvector `<=>` 是余弦距离(0=同向,2=反向);score = 1 - 距离,即
  余弦相似度,越高越相似——与 Milvus COSINE 适配层返回的分数同口径,
  领域侧的阈值后置过滤(宁缺毋滥)无需任何改动。
  ORDER BY embedding <=> %s::vector LIMIT k 可走 HNSW 索引。

【表缺失语义(与 Milvus collection 缺失行为对齐)】
  count/search/delete/existing_chunk_ids → 空结果静默(检索路径
  "库还没建"不是错误);upsert → RuntimeError fail-fast(写入路径
  漏建表是调用方 bug,静默丢数据比报错更有害)。

【连接管理】psycopg_pool 小连接池(min 1 / max 4)——FastAPI 并发请求
下 psycopg 连接不可跨线程共用,池化是线程安全的最低成本方案;close()
关池。进程级单例 + 指纹缓存(含 backend 维度)见 vector_store.get_vector_store。

【老数据】不做 Milvus→PG 自动迁移(用户裁决:可重新写入);存量部署
如需搬数据,可选跑 backend/scripts/migrate_milvus_to_pg.py(幂等)。
"""
import logging
import re
from typing import Any, Dict, List, Optional

from psycopg import sql
from psycopg_pool import ConnectionPool
import psycopg.errors as psycopg_errors

from sdk.scope_registry import check_scope_id
from sdk.vector_store import collection_name

logger = logging.getLogger(__name__)

# 表名白名单:小写字母开头,仅小写字母/数字/下划线(PG 标识符上限 63)。
# 表名由 prefix + scope 拼接后无法参数化,严格校验是 SQL 注入的唯一防线。
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# pgvector 维度护栏:HNSW 索引上限 2000 维(存储上限 16000,取索引可用值)
_MAX_DIM = 2000

_POOL_MIN = 1
_POOL_MAX = 4


def _vec_literal(vec: List[float]) -> str:
    """向量 → pgvector 字面量 '[1.0,2.0,...]'(参数化占位符配 ::vector 强转)。"""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorStore:
    """每 scope 一张表的向量存取(PostgreSQL + pgvector HNSW)。

    Args:
        dsn: PG 连接串(psycopg DSN);SDK 直连,不依赖平台连接池。
        collection_prefix: 表名前缀(命名空间边界;调用方需先在
            scope_registry 登记该前缀),必传——由使用方声明。
    """

    def __init__(self, dsn: str, collection_prefix: str = ""):
        if not collection_prefix:
            raise ValueError("collection_prefix 必传,由使用方声明命名空间")
        if not (dsn or "").strip():
            raise ValueError("dsn 必传(PG 向量存储直连连接串)")
        self._dsn = dsn
        self._prefix = collection_prefix
        self._pool = self._new_pool()

    def _new_pool(self) -> ConnectionPool:
        return ConnectionPool(
            conninfo=self._dsn, min_size=_POOL_MIN, max_size=_POOL_MAX,
            open=True, timeout=10,
            name=f"pg_vector_store[{self._prefix}]",
        )

    def _check_scope(self, scope_id: str) -> None:
        """scope_id 契约入口防御(公共实现见 scope_registry.check_scope_id)。"""
        check_scope_id(scope_id)

    def _table_name(self, scope: str) -> str:
        """scope → 表名(collection_name 规则 + 白名单校验)。"""
        name = collection_name(self._prefix, scope).lower()
        if not _TABLE_NAME_RE.match(name):
            raise ValueError(
                f"表名 {name!r} 不是合法 PG 标识符(^[a-z][a-z0-9_]*$)——"
                "表名无法参数化,此校验是注入的唯一防线")
        return name

    def _table(self, scope: str) -> sql.Identifier:
        return sql.Identifier(self._table_name(scope))

    # ── 生命周期 ───────────────────────────────────────────

    def ping(self) -> None:
        self._with_reconnect(self._ping_raw)

    def _ping_raw(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            logger.warning("pg vector pool close failed", exc_info=True)

    def _with_reconnect(self, fn, *args, **kwargs):
        """连接失效自愈(与 MilvusVectorStore._with_reconnect 同款语义):
        PG 重启/网络断后池内连接失效,OperationalError 时重建池重试一次。
        仅对连接类异常重试,其余原样上抛。"""
        try:
            return fn(*args, **kwargs)
        except psycopg_errors.OperationalError as e:
            logger.warning(f"pg vector 连接失效,重建池后重试一次: {e}")
            try:
                self._pool.close()
            except Exception:
                pass
            self._pool = self._new_pool()
            return fn(*args, **kwargs)

    # ── 表管理 ─────────────────────────────────────────────

    @staticmethod
    def _existing_dim(conn, name: str) -> Optional[int]:
        """读表 embedding 列的已声明维度(pgvector typmod = dim);表不在 → None。"""
        row = conn.execute(
            "SELECT a.atttypmod FROM pg_attribute a "
            "WHERE a.attrelid = (SELECT to_regclass(%s)) "
            "AND a.attname = 'embedding'",
            (name,)).fetchone()
        return int(row[0]) if row and row[0] else None

    def ensure_collection(self, scope: str, dim: int) -> None:
        """建表(幂等)。dim 来自建库时的一次 embedding 探测。

        同 scope 二次 ensure 维度不一致 → fail-fast(向量维度是建库时
        定死的契约,静默混维会让检索结果错乱且难归因)。
        """
        self._check_scope(scope)
        dim = int(dim)
        if not 1 <= dim <= _MAX_DIM:
            raise ValueError(f"dim={dim} 非法(合法范围 1..{_MAX_DIM},"
                             "HNSW 索引维度上限)")
        name = self._table_name(scope)
        table = self._table(scope)
        idx = sql.Identifier(f"{name}_embedding_hnsw")
        with self._pool.connection() as conn:
            cur_dim = self._existing_dim(conn, name)
            if cur_dim is not None:
                if cur_dim != dim:
                    raise ValueError(
                        f"维度不一致: 表 {name} 已是 vector({cur_dim}),"
                        f"收到 dim={dim}——重建需先 drop_collection")
                return   # 已存在且维度一致,幂等
            # 扩展库级幂等(PG 16 + pgvector 0.8 验证可用)
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "chunk_id VARCHAR(64) PRIMARY KEY, "
                "doc_id VARCHAR(64) NOT NULL, "
                "seq BIGINT NOT NULL DEFAULT 0, "
                "text VARCHAR, "
                "embedding vector({}))").format(table, sql.Literal(dim)))
            conn.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} "
                "USING hnsw (embedding vector_cosine_ops)").format(idx, table))
        logger.info("pg vector table ready: %s (dim=%d)", name, dim)

    def drop_collection(self, scope: str) -> None:
        self._check_scope(scope)
        name = self._table_name(scope)
        with self._pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}")
                         .format(self._table(scope)))
        logger.info("pg vector table dropped: %s", name)

    @staticmethod
    def _table_exists(conn, name: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                            (name,)).fetchone()
        return bool(row and row[0])

    # ── 数据操作 ───────────────────────────────────────────

    def upsert_chunks(self, scope: str, items: List[Dict[str, Any]]) -> int:
        """写入/覆盖 chunk 向量(INSERT .. ON CONFLICT 主键 upsert)。"""
        return self._with_reconnect(self._upsert_raw, scope, items)

    def _upsert_raw(self, scope: str, items: List[Dict[str, Any]]) -> int:
        """items: [{chunk_id, doc_id, seq, text, vector}]
        """
        self._check_scope(scope)
        if not items:
            return 0
        name = self._table_name(scope)
        table = self._table(scope)
        rows = [(
            str(it["chunk_id"])[:64],
            str(it["doc_id"])[:64],
            int(it.get("seq") or 0),
            str(it["text"] or ""),
            _vec_literal(it["vector"]),
        ) for it in items]
        with self._pool.connection() as conn:
            if not self._table_exists(conn, name):
                raise RuntimeError(
                    f"向量表 {name} 不存在——先 ensure_collection(scope, dim) 再写入")
            conn.cursor().executemany(
                sql.SQL(
                    "INSERT INTO {} (chunk_id, doc_id, seq, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "doc_id = EXCLUDED.doc_id, seq = EXCLUDED.seq, "
                    "text = EXCLUDED.text, embedding = EXCLUDED.embedding"
                ).format(table),
                rows,
            )
        return len(rows)

    def delete_by_doc(self, scope: str, doc_id: str) -> None:
        """删除某文档的全部向量(重导入清理路径)。表不在 → 静默。"""
        self._check_scope(scope)
        name = self._table_name(scope)
        with self._pool.connection() as conn:
            if not self._table_exists(conn, name):
                return
            conn.execute(sql.SQL("DELETE FROM {} WHERE doc_id = %s")
                         .format(self._table(scope)), (doc_id,))

    def existing_chunk_ids(self, scope: str, doc_id: str) -> set:
        """查某文档已存在向量的 chunk_id 集合(向量缺口补偿的判定依据)。"""
        self._check_scope(scope)
        name = self._table_name(scope)
        with self._pool.connection() as conn:
            if not self._table_exists(conn, name):
                return set()
            rows = conn.execute(
                sql.SQL("SELECT chunk_id FROM {} WHERE doc_id = %s")
                .format(self._table(scope)), (doc_id,)).fetchall()
        return {r[0] for r in rows}

    def search(
        self, scope: str, query_vector: List[float], top_k: int = 5,
        doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """相似度检索(余弦;score = 1 - 余弦距离,越高越相似)。"""
        return self._with_reconnect(
            self._search_raw, scope, query_vector, top_k, doc_id)

    def _search_raw(
        self, scope: str, query_vector: List[float], top_k: int = 5,
        doc_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """

        ORDER BY embedding <=> %s::vector LIMIT k 可走 HNSW 索引;
        doc_id 过滤在索引扫描后施加(与 Milvus 标量过滤同语义)。
        表不在 → 空结果(检索路径"库还没建"不是错误)。
        """
        self._check_scope(scope)
        name = self._table_name(scope)
        qvec = _vec_literal(query_vector)
        with self._pool.connection() as conn:
            if not self._table_exists(conn, name):
                # 行为与 Milvus"缺 collection 抛异常→调用方降级"殊途同归
                # (chatbi 单 scope 跳过/kg 向量路降级),但补 warning 留痕:
                # 静默零痕迹会让"为什么没召回"在排查时无线索(功能对齐审查)
                logger.warning("pg vector search: 表不存在(库还没建?) scope=%s",
                                scope[:8])
                return []
            query = sql.SQL(
                "SELECT chunk_id, doc_id, seq, text, "
                "1 - (embedding <=> {qv}::vector) AS score FROM {t}"
                " {where}"
                " ORDER BY embedding <=> {qv}::vector LIMIT {k}"
            ).format(qv=sql.Placeholder("qv"), t=self._table(scope),
                     where=sql.SQL("WHERE doc_id = {d}").format(
                         d=sql.Placeholder("d")) if doc_id else sql.SQL(""),
                     k=sql.Placeholder("k"))
            params: Dict[str, Any] = {"qv": qvec, "k": int(top_k)}
            if doc_id:
                params["d"] = doc_id
            rows = conn.execute(query, params).fetchall()
        return [{
            "chunkId": r[0],
            "docId": r[1],
            "seq": int(r[2]),
            "text": r[3] or "",
            "score": float(r[4]),
        } for r in rows]

    def count(self, scope: str) -> int:
        self._check_scope(scope)
        name = self._table_name(scope)
        with self._pool.connection() as conn:
            if not self._table_exists(conn, name):
                return 0
            row = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}")
                               .format(self._table(scope))).fetchone()
        return int(row[0]) if row else 0
