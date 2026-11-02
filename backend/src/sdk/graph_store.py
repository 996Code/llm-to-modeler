"""PgGraphStore —— SDK 通用图存储(PostgreSQL 实现,唯一后端)。

【模块定位】
从 knowledge_graph 插件下沉的通用图谱设施:带 scope 命名空间隔离的
实体/关系存储与邻域查询。零领域知识——不认识知识库/文档/本体;实体
只是"有名字和类型的节点",关系只是"有类型的边"。任何需要图结构的
插件(知识图谱/血缘/组织关系…)都可复用。图数据落在调用方 pack 自己
的 PG schema(经 PackRelationalDB),复用平台共享连接池。

【图模型】(scope 物理隔离;表在 pack schema 内)
  graph_nodes   主键 (scope, normalized_name),UNIQUE(scope, id)
                id = "{scope}:{normalized_name}"(前端图渲染的稳定节点 ID)
  graph_entity_refs 主键 (scope, normalized_name, doc_id, chunk_id)
                实体的文档/块级贡献(含该次写入声称的 aliases)——
                delete_document 按 refs 精确清理来源,不猜数组归属
  graph_edges   主键 (scope, source_key, target_key, type, doc_id, chunk_id)
                块级留痕:同一 (source,target,type) 跨块/跨文档各存一条,
                浏览与统计按三元组聚合。键含 doc_id 是对旧图后端空块
                chunk 键跨文档互相覆盖问题的有意修复。

【scope_id 契约】所有方法首参 scope 必须是服务端签发的不可预测 ID
(scope_registry.new_scope_id() 或调用方自己的 UUID),入口防御见
check_scope_id。

【时间戳】ISO TEXT(与平台 Store 存储规约一致)。created_at 首写保留,
updated_at 每次写入刷新。
"""
import json
import logging
import threading
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from sdk.scope_registry import check_scope_id

logger = logging.getLogger(__name__)


class GraphStore(Protocol):
    """图存储协议(鸭子类型,同 PackRouter 风格)。

    方法集 = knowledge_graph 插件三层调用面(API/任务/检索)的并集,
    由测试替身(FakeGraphStore/FakeGraph/RetrievalFakeGraph)证明充分。
    scope_id 契约见模块 docstring——实现方应在入口校验。
    """

    def upsert_batch(self, scope: str, doc_id: str,
                     entities: List[Dict], relations: List[Dict]) -> Dict[str, int]: ...
    def chunk_output(self, scope: str, chunk_id: str) -> Dict[str, List[Dict]]: ...
    def list_entity_names(self, scope: str) -> set: ...
    def list_entity_aliases(self, scope: str) -> Dict[str, Dict[str, Any]]: ...
    def list_entities(self, scope: str) -> List[Dict]: ...
    def list_connected_pairs(self, scope: str) -> set: ...
    def prune_aliases(self, scope: str, names: List[str]) -> int: ...
    def merge_entities(self, scope: str, doc_id: str,
                       pairs: List[Dict]) -> Dict[str, int]: ...
    def delete_document(self, scope: str, doc_id: str) -> Dict[str, int]: ...
    def delete_scope(self, scope: str) -> Dict[str, int]: ...
    def counts(self, scope: str) -> Dict[str, int]: ...
    def document_counts(self, scope: str, doc_id: str) -> Dict[str, int]: ...
    def get_graph(self, scope: str, q: str = "",
                  node_types: Optional[List[str]] = None,
                  limit_nodes: int = 80, limit_edges: int = 150) -> Dict[str, Any]: ...
    def expand_node(self, scope: str, node_id: str,
                    limit_nodes: int = 40, limit_edges: int = 80) -> Dict[str, Any]: ...
    def find_entities(self, scope: str, terms: List[str], limit: int = 20) -> List[Dict]: ...
    def subgraph_around(self, scope: str, seed_names: List[str], hops: int = 2,
                        max_nodes: int = 80, max_edges: int = 150) -> Dict[str, Any]: ...
    def close(self) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(name: str) -> str:
    """实体名归一化:去首尾空白 + 全角转半角 + 统一小写 + 去内部空格 + 统一破折号。

    全角→半角覆盖 ASCII 区间(全角空格/字母/数字/标点),中文原样保留。
    去内部空格:标准号"JJF 1092-2002"(文档里的写法)与"JJF1092-2002"
    (用户输入/文件名)是同一个东西——不去空格 find_entities 永远对不上。
    统一破折号:文档里常用 em-dash(— U+2014)/en-dash(– U+2013),
    用户输入用普通连字符(-)——归一化后须相等。
    """
    if not name:
        return ""
    s = str(name).strip()
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            continue
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII 区
            out.append(chr(code - 0xFEE0))
        elif ch == " ":
            continue  # 半角空格也去掉(标准号/编号匹配)
        elif ch in ("\u2014", "\u2013", "\u2012", "\u2015"):  # em/en dash 统一为 -
            out.append("-")
        else:
            out.append(ch)
    return "".join(out).lower()


def entity_node_id(scope: str, normalized_name: str) -> str:
    return f"{scope}:{normalized_name}"


def _merge_ordered(base: Optional[List[str]], extra: Optional[List[str]]) -> List[str]:
    """保序去重并集(base 在前,extra 追加)——别名/来源数组的合并口径。"""
    out: List[str] = []
    for x in list(base or []) + list(extra or []):
        if x and x not in out:
            out.append(x)
    return out


_GRAPH_DDL = [
    """CREATE TABLE IF NOT EXISTS graph_nodes (
        scope TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        aliases TEXT[] NOT NULL DEFAULT '{}',
        source_docs TEXT[] NOT NULL DEFAULT '{}',
        source_chunks TEXT[] NOT NULL DEFAULT '{}',
        type_status TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        extra JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (scope, normalized_name),
        UNIQUE (scope, id)
    )""",
    "CREATE INDEX IF NOT EXISTS graph_nodes_name ON graph_nodes (scope, name)",
    "CREATE INDEX IF NOT EXISTS graph_nodes_norm_prefix "
    "ON graph_nodes (scope, normalized_name text_pattern_ops)",
    """CREATE TABLE IF NOT EXISTS graph_entity_refs (
        scope TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL DEFAULT '',
        aliases TEXT[] NOT NULL DEFAULT '{}',
        PRIMARY KEY (scope, normalized_name, doc_id, chunk_id)
    )""",
    "CREATE INDEX IF NOT EXISTS graph_entity_refs_doc "
    "ON graph_entity_refs (scope, doc_id)",
    """CREATE TABLE IF NOT EXISTS graph_edges (
        scope TEXT NOT NULL,
        source_key TEXT NOT NULL,
        target_key TEXT NOT NULL,
        type TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL DEFAULT '',
        id TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        extra JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (scope, source_key, target_key, type, doc_id, chunk_id)
    )""",
    "CREATE INDEX IF NOT EXISTS graph_edges_source "
    "ON graph_edges (scope, source_key)",
    "CREATE INDEX IF NOT EXISTS graph_edges_target "
    "ON graph_edges (scope, target_key)",
    "CREATE INDEX IF NOT EXISTS graph_edges_doc "
    "ON graph_edges (scope, doc_id)",
]


class PgGraphStore:
    """PostgreSQL 图存储(表在调用方 pack 的 schema 内;共享连接池)。

    Args:
        schema: pack 名(= PG schema 名;表建在其中,与 pack 关系表同域)。
    """

    def __init__(self, schema: str):
        from sdk.relational_store import PackRelationalDB
        self.schema = schema
        self._db = PackRelationalDB(schema)
        lock_key = 0x67726170 + (zlib.crc32(f"graph:{schema}".encode()) & 0xFFFFFFFF)
        self._db.migrate_locked(lock_key, self._run_ddl)
        logger.info("PgGraphStore[%s]: graph tables ready", schema)

    @staticmethod
    def _run_ddl(conn) -> None:
        for stmt in _GRAPH_DDL:
            conn.execute(stmt)

    def _check_scope(self, scope: str) -> None:
        check_scope_id(scope)

    # ── 生命周期 ───────────────────────────────────────────

    def ping(self) -> None:
        """连通性验证(探针复用)。"""
        with self._db.connect() as conn:
            conn.execute("SELECT 1")

    def close(self) -> None:
        """无独立连接:共享池归属平台,close 不关闭池。"""

    # ── 内部工具 ───────────────────────────────────────────

    @staticmethod
    def _node_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row["id"] or "",
            "name": row["name"] or row["normalized_name"] or "",
            "normalized": row["normalized_name"] or "",
            "type": row["type"] or "",
            "description": row["description"] or "",
            "aliases": list(row["aliases"] or []),
            "sourceDocs": list(row["source_docs"] or []),
            "typeStatus": row["type_status"] or "",
            "updatedAt": row["updated_at"] or "",
        }

    def _fetch_nodes_by_names(self, conn, scope: str,
                              names: List[str]) -> Dict[str, Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE scope = ? AND normalized_name = ANY(?)",
            (scope, list(names))).fetchall()
        return {r["normalized_name"]: self._node_dict(dict(r)) for r in rows}

    @staticmethod
    def _edge_group_rows(conn, sql: str, params: tuple) -> List[Dict[str, Any]]:
        """聚合边组查询:按 (sid,tid,rtype) 聚合块级平行边。

        数组按 (有证据优先, created_at, chunk_id) 稳定排序——"证据最全的
        那条"选取因此确定(旧后端 collect 顺序未定义,这里补稳定口径)。
        """
        _ORDER = "(e.evidence <> '') DESC, e.created_at ASC, e.chunk_id ASC"
        full = sql.format(order=_ORDER)
        return [dict(r) for r in conn.execute(full, params).fetchall()]

    @staticmethod
    def _edge_from_group(g: Dict[str, Any]) -> Dict[str, Any]:
        ids = list(g["ids"] or [])
        descs = list(g["descs"] or [])
        evids = list(g["evids"] or [])
        docs = list(g["docs"] or [])
        best = next((i for i, ev in enumerate(evids) if ev), 0)
        evidences = [x for x in evids if x][:5]
        return {
            "id": ids[best] if ids else "",
            "source": g["sid"], "target": g["tid"], "type": g["rtype"],
            "description": descs[best] if descs else "",
            "evidence": evids[best] if evids else "",
            "evidences": evidences,
            "supportChunks": g["cnt"],
            "docId": docs[best] if docs else "",
        }

    # ── 写入(导入流水线调用) ───────────────────────────────

    def upsert_batch(
        self,
        scope: str,
        doc_id: str,
        entities: List[Dict[str, Any]],
        relations: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """幂等写入一批抽取结果(单事务:节点 upsert → refs → 边 upsert)。

        entities: [{name, normalized_name, type, description, aliases,
                    type_status, chunk_id, chunk_ids?}]
        relations: [{source(normalized), target(normalized), type,
                     description, evidence, chunk_id}]
        Returns: {"entities": 本批实体数, "relations": 本批关系数}
        (按批次计数;管理端统计以 counts() 实时查询为准)
        """
        self._check_scope(scope)
        now = _now()
        # 同名实体多块贡献先在批内聚合(保序去重;首名保留,类型/状态
        # 后写覆盖——与旧后端 UNWIND SET 语义一致)
        agg: Dict[str, Dict[str, Any]] = {}
        for e in entities:
            n = str(e.get("normalized_name") or "").strip()
            if not n:
                continue
            a = agg.setdefault(n, {"name": e.get("name") or n, "type": "",
                                   "desc": "", "status": "", "aliases": [],
                                   "occ": []})
            a["type"] = e.get("type") or ""
            a["status"] = e.get("type_status") or ""
            if e.get("description"):
                a["desc"] = e["description"]
            for al in (e.get("aliases") or []):
                if al and al not in a["aliases"]:
                    a["aliases"].append(al)
            for cid in (e.get("chunk_ids") or [e.get("chunk_id") or ""]):
                if cid not in a["occ"]:
                    a["occ"].append(cid)

        with self._db.connect() as conn:
            for n, a in agg.items():
                self._upsert_node(conn, scope, n, a, doc_id, now)
                for cid in a["occ"]:
                    conn.execute(
                        "INSERT INTO graph_entity_refs "
                        "(scope, normalized_name, doc_id, chunk_id, aliases) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (scope, normalized_name, doc_id, chunk_id) "
                        "DO UPDATE SET aliases = COALESCE("
                        "(SELECT array_agg(DISTINCT x) FROM unnest("
                        "graph_entity_refs.aliases || EXCLUDED.aliases) AS t(x)), '{}')",
                        (scope, n, doc_id, cid, a["aliases"]))
            if relations:
                keys = set()
                clean: List[Dict[str, str]] = []
                for r in relations:
                    s_ = str(r.get("source") or "").strip()
                    t_ = str(r.get("target") or "").strip()
                    if not s_ or not t_:
                        continue
                    keys.add(s_)
                    keys.add(t_)
                    clean.append({"s": s_, "t": t_,
                                  "type": r.get("type") or "",
                                  "desc": r.get("description") or "",
                                  "ev": (r.get("evidence") or "")[:500],
                                  "cid": r.get("chunk_id") or ""})
                if keys:
                    existing = {r["normalized_name"] for r in conn.execute(
                        "SELECT normalized_name FROM graph_nodes "
                        "WHERE scope = ? AND normalized_name = ANY(?)",
                        (scope, sorted(keys))).fetchall()}
                    for r in clean:
                        # MATCH 语义:端点不存在的边静默跳过(节点先于边写
                        # 入,同事务内刚 upsert 的节点可见)
                        if r["s"] not in existing or r["t"] not in existing:
                            continue
                        conn.execute(
                            "INSERT INTO graph_edges "
                            "(scope, source_key, target_key, type, doc_id, "
                            " chunk_id, id, description, evidence, "
                            " created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (scope, source_key, target_key, type, "
                            " doc_id, chunk_id) DO UPDATE SET "
                            "description = EXCLUDED.description, "
                            "evidence = EXCLUDED.evidence, "
                            "updated_at = EXCLUDED.updated_at",
                            (scope, r["s"], r["t"], r["type"], doc_id, r["cid"],
                             f"{scope}:{r['s']}>{r['type']}>{r['t']}:{r['cid']}",
                             r["desc"], r["ev"], now, now))
        return {"entities": len(entities), "relations": len(relations)}

    def _upsert_node(self, conn, scope: str, n: str, a: Dict[str, Any],
                     doc_id: str, now: str) -> None:
        """节点 MERGE 语义:首写建(name/created_at/id),重写并集来源。"""
        for _attempt in (0, 1):
            row = conn.execute(
                "SELECT name, type, description, aliases, source_docs, "
                "source_chunks, type_status FROM graph_nodes "
                "WHERE scope = ? AND normalized_name = ? FOR UPDATE",
                (scope, n)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO graph_nodes "
                    "(scope, normalized_name, id, name, type, description, "
                    " aliases, source_docs, source_chunks, type_status, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (scope, normalized_name) DO NOTHING",
                    (scope, n, entity_node_id(scope, n), a["name"], a["type"],
                     a["desc"], a["aliases"], [doc_id],
                     [c for c in a["occ"] if c], a["status"], now, now))
                if cur.rowcount:
                    return
                continue   # 并发已插入:重读合并
            docs = list(row["source_docs"] or [])
            if doc_id not in docs:
                docs.append(doc_id)
            chunks = [c for c in (row["source_chunks"] or []) if c]
            for c in a["occ"]:
                if c and c not in chunks:
                    chunks.append(c)
            conn.execute(
                "UPDATE graph_nodes SET type = ?, type_status = ?, "
                "description = ?, aliases = ?, source_docs = ?, "
                "source_chunks = ?, updated_at = ? "
                "WHERE scope = ? AND normalized_name = ?",
                (a["type"], a["status"], row["description"] or a["desc"],
                 _merge_ordered(row["aliases"], a["aliases"]), docs, chunks,
                 now, scope, n))
            return

    # ── 溯源与词表 ─────────────────────────────────────────

    def chunk_output(self, scope: str, chunk_id: str) -> Dict[str, List[Dict]]:
        """块级抽取产出(块明细"查看产出"用)。

        实体按 source_chunks 累积列表包含该块判定(实体跨块合并,溯源是
        多对多);关系按 chunk_id 精确匹配(写入时逐条带块 id)。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            ent_rows = conn.execute(
                "SELECT * FROM graph_nodes WHERE scope = ? AND ? = ANY(source_chunks)",
                (scope, chunk_id)).fetchall()
            rel_rows = conn.execute(
                "SELECT sn.name AS source, tn.name AS target, e.type, "
                "e.description, e.evidence FROM graph_edges e "
                "JOIN graph_nodes sn ON sn.scope = e.scope "
                " AND sn.normalized_name = e.source_key "
                "JOIN graph_nodes tn ON tn.scope = e.scope "
                " AND tn.normalized_name = e.target_key "
                "WHERE e.scope = ? AND e.chunk_id = ?",
                (scope, chunk_id)).fetchall()
        entities = [self._node_dict(dict(r)) for r in ent_rows]
        relations = [{
            "source": r["source"], "target": r["target"],
            "type": r["type"] or "",
            "description": r["description"] or "",
            "evidence": (r["evidence"] or "")[:200],
        } for r in rel_rows]
        return {"entities": entities, "relations": relations}

    def list_entity_names(self, scope: str) -> set:
        """库内全部实体 normalized_name(悬空关系过滤的 known 集合扩充用)。"""
        self._check_scope(scope)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT normalized_name FROM graph_nodes WHERE scope = ?",
                (scope,)).fetchall()
        return {r["normalized_name"] for r in rows if r["normalized_name"]}

    def list_entity_aliases(self, scope: str) -> Dict[str, Dict[str, Any]]:
        """库内全部实体的别名+类型+描述表:normalized_name -> {type, aliases[], description}。

        导入流水线批次间用它做实体消歧:图里某实体已经积累了别名(如
        张小凡 的别名含"鬼厉"),后续批次抽到"鬼厉"时就有据可依地把
        它并回张小凡,而不是另立节点。type 随附,合并前做同类型校验;
        description 随附,词表渲染属性提示(指称消解的特征对卡材料)。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT normalized_name, type, aliases, description "
                "FROM graph_nodes WHERE scope = ? ORDER BY normalized_name",
                (scope,)).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            n = r["normalized_name"]
            if n:
                out[n] = {
                    "type": r["type"] or "",
                    "aliases": [x for x in (r["aliases"] or []) if x],
                    "description": r["description"] or "",
                }
        return out

    def list_entities(self, scope: str) -> List[Dict]:
        """库内全部实体(含 type/aliases/description/块溯源量/创建时间)。

        created_at 供消歧时选 canonical:最早入库 = 角色首次登场的名字
        (张小凡 先于 鬼厉 入库,合并后保留张小凡);description 供收尾
        描述连边做点名匹配。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT normalized_name, name, type, aliases, source_chunks, "
                "description, created_at FROM graph_nodes "
                "WHERE scope = ? ORDER BY normalized_name",
                (scope,)).fetchall()
        out = []
        for r in rows:
            n = r["normalized_name"]
            if not n:
                continue
            out.append({
                "normalized": n,
                "name": r["name"] or n,
                "type": r["type"] or "",
                "aliases": [x for x in (r["aliases"] or []) if x],
                "source_chunks": list(r["source_chunks"] or []),
                "description": r["description"] or "",
                "created_at": r["created_at"] or "",
            })
        return out

    def list_connected_pairs(self, scope: str) -> set:
        """库内全部有边相连的实体对(无向 normalized_name 二元组)。

        收尾描述连边去重用:两实体间已有任何关系就不再叠加兜底弱边。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_key AS a, target_key AS b "
                "FROM graph_edges WHERE scope = ? "
                "AND source_key <> target_key", (scope,)).fetchall()
        out = set()
        for r in rows:
            a, b = r["a"], r["b"]
            if a and b:
                out.add((a, b))
                out.add((b, a))
        return out

    def prune_aliases(self, scope: str, names: List[str]) -> int:
        """从所有实体的 aliases 里移除指定名字(收尾去争议)。

        入库前的独占性校验只保证"本批不扩散",历史遗留/合并吸收进来的
        泛称别名仍在图里——词表从图渲染,它们会被持续喂给后续抽取
        (回音室通道)。收尾时把争议别名集清掉,反馈即断。幂等。

        同步清理 graph_entity_refs 里的别名声称,防止下次合并聚合时
        争议别名从贡献记录复活。

        Returns: 实际移除的别名声称条数(节点级)。
        """
        self._check_scope(scope)
        clean = [n for n in (names or []) if n]
        if not clean:
            return 0
        now = _now()
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT aliases FROM graph_nodes WHERE scope = ? "
                "AND EXISTS (SELECT 1 FROM unnest(aliases) AS t(x) "
                "WHERE x = ANY(?))", (scope, clean)).fetchall()
            removed = sum(1 for r in rows for a in (r["aliases"] or [])
                          if a in clean)
            conn.execute(
                "UPDATE graph_nodes SET aliases = COALESCE("
                "(SELECT array_agg(x) FROM unnest(aliases) AS t(x) "
                "WHERE x <> ALL(?)), '{}'), updated_at = ? WHERE scope = ? "
                "AND EXISTS (SELECT 1 FROM unnest(aliases) AS t(x) "
                "WHERE x = ANY(?))", (clean, now, scope, clean))
            conn.execute(
                "UPDATE graph_entity_refs SET aliases = COALESCE("
                "(SELECT array_agg(x) FROM unnest(aliases) AS t(x) "
                "WHERE x <> ALL(?)), '{}') WHERE scope = ? "
                "AND EXISTS (SELECT 1 FROM unnest(aliases) AS t(x) "
                "WHERE x = ANY(?))", (clean, scope, clean))
        return removed

    def merge_entities(self, scope: str, doc_id: str,
                       pairs: List[Dict]) -> Dict[str, int]:
        """实体消歧合并(单事务):把碎片实体并进 canonical 实体。

        pairs: [{"canonical": 标准名(normalized), "fragment": 碎片名(normalized)}]
        只处理 pairs 明确声明的对——绝不启发式猜测,避免误并(如
        "青云山/青云门"这种真·不同实体)。合并语义:
          1. 碎片 aliases + 碎片名 并入 canonical 的 aliases(去重)——
             碎片名必须保留,否则碎片节点删除后按别名检索就丢了;
          2. canonical 并集碎片的 source_docs / source_chunks(溯源不丢),
             碎片的 refs 贡献记录一并迁移;
          3. 碎片的进出边**迁移**到 canonical(按本实现的贡献键
             (source,target,type,doc,chunk) upsert,证据补齐后删旧边
             ——只改关系归属不移端点);
          4. canonical↔碎片 之间的直连边是合并后的自环,直接删;
          5. 删除碎片节点。

        Returns: {"merged": 成功合并的对数}
        """
        self._check_scope(scope)
        if not pairs:
            return {"merged": 0}
        now = _now()
        merged = 0
        with self._db.connect() as conn:
            for p in pairs:
                canon = str((p or {}).get("canonical") or "").strip()
                frag = str((p or {}).get("fragment") or "").strip()
                if not canon or not frag or canon == frag:
                    continue
                # 按名字序锁定双方(防并发同对合并死锁)
                rows = conn.execute(
                    "SELECT normalized_name, aliases, source_docs, "
                    "source_chunks FROM graph_nodes WHERE scope = ? "
                    "AND normalized_name IN (?, ?) "
                    "ORDER BY normalized_name FOR UPDATE",
                    (scope, canon, frag)).fetchall()
                by = {r["normalized_name"]: r for r in rows}
                if canon not in by or frag not in by:
                    continue
                # 0) canonical↔碎片 直连边 = 合并后自环,删
                conn.execute(
                    "DELETE FROM graph_edges WHERE scope = ? "
                    "AND ((source_key = ? AND target_key = ?) "
                    "OR (source_key = ? AND target_key = ?))",
                    (scope, canon, frag, frag, canon))
                # 1) 出边迁移:f→x 落成 c→x
                out_edges = [dict(r) for r in conn.execute(
                    "SELECT * FROM graph_edges WHERE scope = ? AND source_key = ?",
                    (scope, frag)).fetchall()]
                for e in out_edges:
                    self._migrate_edge(conn, scope, e, new_source=canon,
                                       new_target=e["target_key"], now=now)
                conn.execute(
                    "DELETE FROM graph_edges WHERE scope = ? AND source_key = ?",
                    (scope, frag))
                # 2) 入边迁移:x→f 落成 x→c
                in_edges = [dict(r) for r in conn.execute(
                    "SELECT * FROM graph_edges WHERE scope = ? AND target_key = ?",
                    (scope, frag)).fetchall()]
                for e in in_edges:
                    self._migrate_edge(conn, scope, e,
                                       new_source=e["source_key"],
                                       new_target=canon, now=now)
                conn.execute(
                    "DELETE FROM graph_edges WHERE scope = ? AND target_key = ?",
                    (scope, frag))
                # 3) refs 迁移(别名声称并集)
                refs = [dict(r) for r in conn.execute(
                    "SELECT doc_id, chunk_id, aliases FROM graph_entity_refs "
                    "WHERE scope = ? AND normalized_name = ?",
                    (scope, frag)).fetchall()]
                for r in refs:
                    conn.execute(
                        "INSERT INTO graph_entity_refs "
                        "(scope, normalized_name, doc_id, chunk_id, aliases) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (scope, normalized_name, doc_id, chunk_id) "
                        "DO UPDATE SET aliases = COALESCE("
                        "(SELECT array_agg(DISTINCT x) FROM unnest("
                        "graph_entity_refs.aliases || EXCLUDED.aliases) AS t(x)), '{}')",
                        (scope, canon, r["doc_id"], r["chunk_id"],
                         list(r["aliases"] or [])))
                conn.execute(
                    "DELETE FROM graph_entity_refs WHERE scope = ? "
                    "AND normalized_name = ?", (scope, frag))
                # 4) 属性并集:别名(含碎片名,不含 canonical 本名)+溯源
                c, f = by[canon], by[frag]
                aliases = [x for x in _merge_ordered(
                    _merge_ordered(c["aliases"], f["aliases"]), [frag])
                    if x != canon]
                conn.execute(
                    "UPDATE graph_nodes SET aliases = ?, source_docs = ?, "
                    "source_chunks = ?, updated_at = ? "
                    "WHERE scope = ? AND normalized_name = ?",
                    (aliases,
                     _merge_ordered(c["source_docs"], f["source_docs"]),
                     _merge_ordered(c["source_chunks"], f["source_chunks"]),
                     now, scope, canon))
                conn.execute(
                    "DELETE FROM graph_nodes WHERE scope = ? "
                    "AND normalized_name = ?", (scope, frag))
                merged += 1
        return {"merged": merged}

    @staticmethod
    def _migrate_edge(conn, scope: str, e: Dict[str, Any],
                      new_source: str, new_target: str, now: str) -> None:
        """合并时的单条边迁移:新键 upsert(已有非空证据优先),旧边由调用方删。"""
        new_id = f"{scope}:{new_source}>{e['type']}>{new_target}:{e['chunk_id']}"
        conn.execute(
            "INSERT INTO graph_edges "
            "(scope, source_key, target_key, type, doc_id, chunk_id, id, "
            " description, evidence, created_at, updated_at, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb) "
            "ON CONFLICT (scope, source_key, target_key, type, doc_id, chunk_id) "
            "DO UPDATE SET "
            "description = CASE WHEN graph_edges.description = '' "
            "THEN EXCLUDED.description ELSE graph_edges.description END, "
            "evidence = CASE WHEN graph_edges.evidence = '' "
            "THEN EXCLUDED.evidence ELSE graph_edges.evidence END, "
            "updated_at = EXCLUDED.updated_at",
            (scope, new_source, new_target, e["type"], e["doc_id"],
             e["chunk_id"], new_id, e["description"] or "",
             e["evidence"] or "", e["created_at"], now,
             json.dumps(e["extra"] or {}, ensure_ascii=False)))

    def delete_document(self, scope: str, doc_id: str) -> Dict[str, int]:
        """删除某文档的全部图谱贡献。

        边按 doc_id 删;实体经 refs 精确去源(该文档声称的块引用同步撤
        出;存量迁移节点无法归因的旧块引用保持原样,不猜),无剩余来源
        的实体连同其余关联边一并删除。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM graph_edges WHERE scope = ? AND doc_id = ?",
                (scope, doc_id))
            edges = cur.rowcount
            ref_rows = [dict(r) for r in conn.execute(
                "SELECT normalized_name, chunk_id FROM graph_entity_refs "
                "WHERE scope = ? AND doc_id = ?", (scope, doc_id)).fetchall()]
            conn.execute(
                "DELETE FROM graph_entity_refs WHERE scope = ? AND doc_id = ?",
                (scope, doc_id))
            drop_chunks: Dict[str, set] = {}
            for r in ref_rows:
                drop_chunks.setdefault(r["normalized_name"], set()).add(
                    r["chunk_id"])
            nodes = [dict(r) for r in conn.execute(
                "SELECT normalized_name, source_docs, source_chunks "
                "FROM graph_nodes WHERE scope = ? AND ? = ANY(source_docs)",
                (scope, doc_id)).fetchall()]
            orphans = []
            for n in nodes:
                docs = [d for d in (n["source_docs"] or []) if d != doc_id]
                if not docs:
                    orphans.append(n["normalized_name"])
                    continue
                drop = drop_chunks.get(n["normalized_name"], set())
                chunks = [c for c in (n["source_chunks"] or [])
                          if c not in drop]
                conn.execute(
                    "UPDATE graph_nodes SET source_docs = ?, source_chunks = ? "
                    "WHERE scope = ? AND normalized_name = ?",
                    (docs, chunks, scope, n["normalized_name"]))
            if orphans:
                conn.execute(
                    "DELETE FROM graph_edges WHERE scope = ? "
                    "AND (source_key = ANY(?) OR target_key = ANY(?))",
                    (scope, orphans, orphans))
                conn.execute(
                    "DELETE FROM graph_nodes WHERE scope = ? "
                    "AND normalized_name = ANY(?)", (scope, orphans))
        return {"edges": int(edges), "orphanEntities": len(orphans)}

    def delete_scope(self, scope: str) -> Dict[str, int]:
        """整库删除(三表同事务清除)。"""
        self._check_scope(scope)
        with self._db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM graph_nodes WHERE scope = ?", (scope,))
            n = cur.rowcount
            conn.execute("DELETE FROM graph_edges WHERE scope = ?", (scope,))
            conn.execute(
                "DELETE FROM graph_entity_refs WHERE scope = ?", (scope,))
        return {"entities": int(n)}

    # ── 查询(在线浏览 + 检索) ─────────────────────────────

    def counts(self, scope: str) -> Dict[str, int]:
        """库级统计。关系数按 (source,type,target) 去重(平行边=块级留痕,
        不去重会虚增数倍,与 document_counts 同语义)。"""
        self._check_scope(scope)
        with self._db.connect() as conn:
            entities = conn.execute(
                "SELECT count(*) AS c FROM graph_nodes WHERE scope = ?",
                (scope,)).fetchone()["c"]
            relations = conn.execute(
                "SELECT count(DISTINCT (source_key, type, target_key)) AS c "
                "FROM graph_edges WHERE scope = ?", (scope,)).fetchone()["c"]
        return {"entities": int(entities), "relations": int(relations)}

    def document_counts(self, scope: str, doc_id: str) -> Dict[str, int]:
        """某文档在图谱中的贡献数(实体按 source_docs 引用,关系按 doc_id 归属)。

        关系计数按 (source,type,target) 去重——平行边是块级留痕(每块一条),
        重复计入会让文档的 relation_count 虚增数倍,与用户直觉的"关系数"不符。
        """
        self._check_scope(scope)
        with self._db.connect() as conn:
            entities = conn.execute(
                "SELECT count(*) AS c FROM graph_nodes WHERE scope = ? "
                "AND ? = ANY(source_docs)", (scope, doc_id)).fetchone()["c"]
            relations = conn.execute(
                "SELECT count(DISTINCT (source_key, type, target_key)) AS c "
                "FROM graph_edges WHERE scope = ? AND doc_id = ?",
                (scope, doc_id)).fetchone()["c"]
        return {"entities": int(entities), "relations": int(relations)}

    def get_graph(
        self,
        scope: str,
        q: str = "",
        node_types: Optional[List[str]] = None,
        limit_nodes: int = 80,
        limit_edges: int = 150,
    ) -> Dict[str, Any]:
        """图谱浏览首页数据:限量节点(可按名称/类型过滤)+ 其邻接边。"""
        self._check_scope(scope)
        clauses, params = ["scope = ?"], [scope]
        if q:
            clauses.append("(strpos(lower(name), lower(?)) > 0 "
                           "OR strpos(lower(description), lower(?)) > 0)")
            params += [q, q]
        if node_types:
            clauses.append("type = ANY(?)")
            params.append(list(node_types))
        where = " AND ".join(clauses)
        with self._db.connect() as conn:
            node_rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM graph_nodes WHERE {where} "
                "ORDER BY updated_at DESC, normalized_name LIMIT ?",
                params + [limit_nodes]).fetchall()]
            nodes = [self._node_dict(r) for r in node_rows]
            ids = [n["id"] for n in nodes]
            groups = self._edge_group_rows(conn, (
                "SELECT sn.id AS sid, tn.id AS tid, e.type AS rtype, "
                "count(*) AS cnt, "
                "array_agg(e.id ORDER BY {order}) AS ids, "
                "array_agg(e.description ORDER BY {order}) AS descs, "
                "array_agg(e.evidence ORDER BY {order}) AS evids, "
                "array_agg(e.doc_id ORDER BY {order}) AS docs "
                "FROM graph_edges e "
                "JOIN graph_nodes sn ON sn.scope = e.scope "
                " AND sn.normalized_name = e.source_key "
                "JOIN graph_nodes tn ON tn.scope = e.scope "
                " AND tn.normalized_name = e.target_key "
                "WHERE e.scope = ? AND sn.id = ANY(?) AND tn.id = ANY(?) "
                "GROUP BY sn.id, tn.id, e.type "
                "ORDER BY cnt DESC, sid, tid, rtype LIMIT ?"),
                (scope, ids, ids, limit_edges)) if ids else []
        return {"nodes": nodes,
                "edges": [self._edge_from_group(g) for g in groups]}

    def expand_node(
        self, scope: str, node_id: str, limit_nodes: int = 40, limit_edges: int = 80,
    ) -> Dict[str, Any]:
        """点击节点增量展开:1 跳邻域(双向)。边按 (source,type,target)
        聚合(跨块平行边合并,与 get_graph 同语义)。"""
        self._check_scope(scope)
        with self._db.connect() as conn:
            center_row = conn.execute(
                "SELECT * FROM graph_nodes WHERE scope = ? AND id = ?",
                (scope, node_id)).fetchone()
            center = self._node_dict(dict(center_row)) if center_row else None
            if center is None:
                return {"nodes": [], "edges": []}
            groups = self._edge_group_rows(conn, (
                "SELECT sn.id AS sid, tn.id AS tid, e.type AS rtype, "
                "sn.normalized_name AS skey, tn.normalized_name AS tkey, "
                "count(*) AS cnt, "
                "array_agg(e.id ORDER BY {order}) AS ids, "
                "array_agg(e.description ORDER BY {order}) AS descs, "
                "array_agg(e.evidence ORDER BY {order}) AS evids, "
                "array_agg(e.doc_id ORDER BY {order}) AS docs "
                "FROM graph_edges e "
                "JOIN graph_nodes sn ON sn.scope = e.scope "
                " AND sn.normalized_name = e.source_key "
                "JOIN graph_nodes tn ON tn.scope = e.scope "
                " AND tn.normalized_name = e.target_key "
                "WHERE e.scope = ? AND (sn.id = ? OR tn.id = ?) "
                "GROUP BY sn.id, tn.id, e.type, sn.normalized_name, "
                " tn.normalized_name "
                "ORDER BY cnt DESC, sid, tid, rtype LIMIT ?"),
                (scope, node_id, node_id, limit_edges))
            neighbor_keys = []
            center_norm = center["normalized"]
            for g in groups:
                for key in (g["skey"], g["tkey"]):
                    if key != center_norm and key not in neighbor_keys:
                        neighbor_keys.append(key)
            by_name = self._fetch_nodes_by_names(conn, scope, neighbor_keys) \
                if neighbor_keys else {}
        nodes = [center]
        seen = {center["id"]}
        for key in neighbor_keys:
            node = by_name.get(key)
            if node and node["id"] not in seen and len(nodes) < limit_nodes + 1:
                nodes.append(node)
                seen.add(node["id"])
        return {"nodes": nodes,
                "edges": [self._edge_from_group(g) for g in groups]}

    def find_entities(
        self, scope: str, terms: List[str], limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按名称词找种子实体:精确(normalized/别名小写)优先,
        前缀次之,包含兜底——上层有结果即不降层(与旧口径一致)。"""
        self._check_scope(scope)
        if not terms:
            return []
        normalized = [normalize_name(t) for t in terms if t.strip()]
        if not normalized:
            return []
        lowers = [t.lower() for t in normalized]
        _COLS = ("id, normalized_name, name, type, description, aliases, "
                 "source_docs, type_status, updated_at")
        with self._db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT {_COLS} FROM graph_nodes WHERE scope = ? AND ("
                "normalized_name = ANY(?) OR EXISTS ("
                "SELECT 1 FROM unnest(aliases) AS t(a) "
                "WHERE lower(a) = ANY(?))) "
                "ORDER BY normalized_name LIMIT ?",
                (scope, normalized, lowers, limit)).fetchall()]
            if not rows:
                prefixes = [t + "%" for t in normalized]
                rows = [dict(r) for r in conn.execute(
                    f"SELECT {_COLS} FROM graph_nodes WHERE scope = ? AND ("
                    "normalized_name LIKE ANY(?) OR EXISTS ("
                    "SELECT 1 FROM unnest(aliases) AS t(a) "
                    "WHERE lower(a) LIKE ANY(?))) "
                    "ORDER BY normalized_name LIMIT ?",
                    (scope, prefixes,
                     [p.lower() for p in prefixes], limit)).fetchall()]
            if not rows:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT {_COLS} FROM graph_nodes WHERE scope = ? AND ("
                    "strpos(normalized_name, ?) > 0 OR EXISTS ("
                    "SELECT 1 FROM unnest(aliases) AS t(a) "
                    "WHERE strpos(lower(a), ?) > 0)) "
                    "ORDER BY normalized_name LIMIT ?",
                    (scope, normalized[0], normalized[0], limit)).fetchall()] \
                    if len(normalized) == 1 else self._contains_all_terms(
                        conn, scope, normalized, limit, _COLS)
        return [self._node_dict(r) for r in rows]

    @staticmethod
    def _contains_all_terms(conn, scope: str, terms: List[str],
                            limit: int, cols: str) -> List[Dict[str, Any]]:
        """多词包含层:UNWIND 语义——任一词命中即返回(去重)。"""
        out: Dict[str, Dict[str, Any]] = {}
        for t in terms:
            for r in conn.execute(
                f"SELECT {cols} FROM graph_nodes WHERE scope = ? AND ("
                "strpos(normalized_name, ?) > 0 OR EXISTS ("
                "SELECT 1 FROM unnest(aliases) AS t(a) "
                "WHERE strpos(lower(a), ?) > 0)) "
                "ORDER BY normalized_name LIMIT ?",
                (scope, t, t, limit)).fetchall():
                out.setdefault(r["normalized_name"], dict(r))
                if len(out) >= limit:
                    return list(out.values())
        return list(out.values())

    def subgraph_around(
        self,
        scope: str,
        seed_names: List[str],
        hops: int = 2,
        max_nodes: int = 80,
        max_edges: int = 150,
    ) -> Dict[str, Any]:
        """BFS 扩展种子实体的邻域子图(Python 侧逐跳,便于限量)。

        seed_names 传 normalized_name 列表。边按 (source,type,target)
        聚合占限额(块级平行边不重复占位);只保留两端都在结果内的边。
        """
        self._check_scope(scope)
        visited: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        edge_keys = set()
        frontier = list(dict.fromkeys(
            n for n in seed_names if n))[:max_nodes]
        with self._db.connect() as conn:
            if frontier:
                seeds = self._fetch_nodes_by_names(conn, scope, frontier)
                for node in seeds.values():
                    visited[node["id"]] = node
                frontier = [n for n in frontier
                            if entity_node_id(scope, n) in visited]
            for _hop in range(max(1, hops)):
                if not frontier or len(visited) >= max_nodes \
                        or len(edges) >= max_edges:
                    break
                groups = self._edge_group_rows(conn, (
                    "SELECT sn.id AS sid, tn.id AS tid, e.type AS rtype, "
                    "sn.normalized_name AS skey, tn.normalized_name AS tkey, "
                    "count(*) AS cnt, "
                    "array_agg(e.id ORDER BY {order}) AS ids, "
                    "array_agg(e.description ORDER BY {order}) AS descs, "
                    "array_agg(e.evidence ORDER BY {order}) AS evids "
                    "FROM graph_edges e "
                    "JOIN graph_nodes sn ON sn.scope = e.scope "
                    " AND sn.normalized_name = e.source_key "
                    "JOIN graph_nodes tn ON tn.scope = e.scope "
                    " AND tn.normalized_name = e.target_key "
                    "WHERE e.scope = ? AND (sn.normalized_name = ANY(?) "
                    "OR tn.normalized_name = ANY(?)) "
                    "GROUP BY sn.id, tn.id, e.type, sn.normalized_name, "
                    " tn.normalized_name "
                    "ORDER BY cnt DESC, sid, tid, rtype LIMIT ?"),
                    (scope, frontier, frontier, max_edges - len(edges)))
                if not groups:
                    break
                frontier_set = set(frontier)
                next_frontier = []
                neighbor_keys = []
                for g in groups:
                    ekey = (g["sid"], g["rtype"], g["tid"])
                    if ekey not in edge_keys:
                        edge_keys.add(ekey)
                        best = next((i for i, ev in enumerate(g["evids"] or [])
                                     if ev), 0)
                        ids = list(g["ids"] or [])
                        descs = list(g["descs"] or [])
                        evids = list(g["evids"] or [])
                        edges.append({
                            "id": ids[best] if ids else f"{g['sid']}->{g['tid']}",
                            "source": g["sid"], "target": g["tid"],
                            "type": g["rtype"],
                            "description": descs[best] if descs else "",
                            "evidence": evids[best] if evids else "",
                        })
                    # 邻居 = 端点中不在 frontier 侧的那一端(两端都在
                    # frontier 时两端都可成为邻居,与旧无向匹配一致)
                    for a, other in ((g["skey"], g["tkey"]),
                                     (g["tkey"], g["skey"])):
                        if a in frontier_set and other not in neighbor_keys:
                            neighbor_keys.append(other)
                by_name = self._fetch_nodes_by_names(conn, scope, neighbor_keys) \
                    if neighbor_keys else {}
                for key in neighbor_keys:
                    node = by_name.get(key)
                    if node and node["id"] not in visited \
                            and len(visited) < max_nodes:
                        visited[node["id"]] = node
                        # BFS 下一跳按 normalized_name(大写/全角实体在
                        # hop≥2 不断链)
                        next_frontier.append(node["normalized"])
                frontier = next_frontier
        valid_ids = set(visited.keys())
        edges = [e for e in edges
                 if e["source"] in valid_ids and e["target"] in valid_ids]
        return {"nodes": list(visited.values()), "edges": edges[:max_edges]}


# ── 进程级单例(按 schema 多槽;连接共享平台池,无独立驱动) ────

_cached_stores: Dict[str, "PgGraphStore"] = {}
_cache_lock = threading.Lock()


def get_graph_store(schema: str) -> "PgGraphStore":
    """按 schema 取/建图存储单例(轻对象:仅缓存表就绪的句柄)。"""
    with _cache_lock:
        hit = _cached_stores.get(schema)
        if hit is not None:
            return hit
    store = PgGraphStore(schema)
    with _cache_lock:
        return _cached_stores.setdefault(schema, store)


def reset_graph_store_cache() -> None:
    """测试辅助:清单例(共享池不在此关闭)。"""
    with _cache_lock:
        _cached_stores.clear()
