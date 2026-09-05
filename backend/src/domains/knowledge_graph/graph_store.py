"""Neo4jGraphStore - 知识图谱的图谱存储(Neo4j 5,官方 Python driver)。

【图模型】(kb_id 物理隔离)
  (:Entity {id, kb_id, name, normalized_name, type, description,
            aliases[], source_docs[], type_status, created_at, updated_at})
    - (kb_id, normalized_name) NODE KEY 约束 = MERGE 幂等锚点
    - id = "{kb_id}:{normalized_name}"(前端图渲染的稳定节点 ID)
    - source_docs = 引用该实体的文档 id 集合(文档删除时做引用计数)
  [:RELATES {id, kb_id, doc_id, chunk_id, source_key, target_key,
             type, description, evidence, created_at, updated_at}]
    - MERGE 键 = (kb_id, source_key, target_key, type, chunk_id):
      同一文档重导先清理后写入,天然幂等;不同文档对同一对实体的
      同型关系各自保留(带各自 doc/chunk 溯源)

【连接管理】进程级单例 + 指纹缓存:配置(设置页热改)变化时重建 driver、
关闭旧连接。所有方法线程安全(driver 自带连接池)。

【测试替身】本模块只依赖 neo4j driver;单测用 FakeGraphStore(内存实现,
见 tests/)替换,不在测试里要求真实 Neo4j。
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(name: str) -> str:
    """实体名归一化:去首尾空白 + 全角转半角 + 统一小写(merge 锚点)。

    全角→半角覆盖 ASCII 区间(全角空格/字母/数字/标点),中文原样保留。
    """
    if not name:
        return ""
    s = str(name).strip()
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:  # 全角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII 区
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).lower()


def entity_node_id(kb_id: str, normalized_name: str) -> str:
    return f"{kb_id}:{normalized_name}"


class Neo4jGraphStore:
    """Neo4j 图谱存储(driver 直连;地址/凭证来自插件设置解析链)。"""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        from neo4j import GraphDatabase
        self._database = database or "neo4j"
        self._driver = GraphDatabase.driver(
            uri, auth=(user or "neo4j", password or "")
        )

    # ── 生命周期 ───────────────────────────────────────────

    def ping(self) -> None:
        """连通性 + 鉴权验证(探针复用)。"""
        self._driver.verify_connectivity()

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            logger.warning("neo4j driver close failed", exc_info=True)

    def ensure_constraints(self) -> None:
        """幂等建约束与索引(库级,一次性成本)。

        唯一性用 IS UNIQUE 而非 NODE KEY:NODE KEY(唯一 + 非空)是企业版
        功能,社区版只有 UNIQUE;MERGE 幂等锚点只需唯一性。
        """
        with self._driver.session(database=self._database) as s:
            s.run(
                "CREATE CONSTRAINT kg_entity_key IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE (e.kb_id, e.normalized_name) IS UNIQUE"
            ).consume()
            # name 前缀匹配走 range 索引即可满足 v1 检索;全文索引留待需要时加
            s.run(
                "CREATE INDEX kg_entity_name IF NOT EXISTS "
                "FOR (e:Entity) ON (e.kb_id, e.name)"
            ).consume()

    def _session(self):
        return self._driver.session(database=self._database)

    # ── 写入(导入流水线调用) ───────────────────────────────

    def upsert_batch(
        self,
        kb_id: str,
        doc_id: str,
        entities: List[Dict[str, Any]],
        relations: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """幂等写入一批抽取结果(单事务:实体 MERGE → 关系 MERGE)。

        entities: [{name, normalized_name, type, description, aliases,
                    type_status, chunk_id}]
        relations: [{source(normalized), target(normalized), type,
                     description, evidence, chunk_id}]
        Returns: {"entities": 本批实体数, "relations": 本批关系数}
        (按批次计数;管理端统计以 counts() 实时查询为准)
        """
        now = _now()
        with self._session() as s:
            def _tx(tx):
                if entities:
                    tx.run(
                        """
                        UNWIND $rows AS ent
                        MERGE (e:Entity {kb_id: $kb, normalized_name: ent.normalized_name})
                        ON CREATE SET e.id = ent.id, e.created_at = $now, e.name = ent.name,
                                      e.description = ent.description, e.aliases = ent.aliases
                        SET e.type = ent.type,
                            e.description = CASE WHEN e.description IS NULL OR e.description = ''
                                                THEN ent.description ELSE e.description END,
                            e.aliases = CASE WHEN size(ent.aliases) > 0
                                THEN [a IN coalesce(e.aliases, []) WHERE NOT a IN ent.aliases] + ent.aliases
                                ELSE coalesce(e.aliases, []) END,
                            e.source_docs = CASE WHEN ent.doc_id IN e.source_docs
                                                 THEN e.source_docs ELSE coalesce(e.source_docs, []) + ent.doc_id END,
                            e.type_status = ent.type_status,
                            e.updated_at = $now
                        """,
                        kb=kb_id, now=now,
                        rows=[{
                            "normalized_name": e["normalized_name"],
                            "id": entity_node_id(kb_id, e["normalized_name"]),
                            "name": e.get("name") or e["normalized_name"],
                            "type": e.get("type") or "concept",
                            "description": e.get("description") or "",
                            "aliases": list(dict.fromkeys(e.get("aliases") or [])),
                            "type_status": e.get("type_status") or "approved",
                            "doc_id": doc_id,
                        } for e in entities],
                    ).consume()
                if relations:
                    tx.run(
                        """
                        UNWIND $rows AS r
                        MATCH (s:Entity {kb_id: $kb, normalized_name: r.source})
                        MATCH (t:Entity {kb_id: $kb, normalized_name: r.target})
                        MERGE (s)-[rel:RELATES {
                            kb_id: $kb, source_key: r.source, target_key: r.target,
                            type: r.type, chunk_id: r.chunk_id}]->(t)
                        ON CREATE SET rel.created_at = $now, rel.id = r.id
                        SET rel.doc_id = $doc, rel.description = r.description,
                            rel.evidence = r.evidence, rel.updated_at = $now
                        """,
                        kb=kb_id, doc=doc_id, now=now,
                        rows=[{
                            "source": r["source"], "target": r["target"],
                            "type": r.get("type") or "相关",
                            "description": r.get("description") or "",
                            "evidence": (r.get("evidence") or "")[:500],
                            "chunk_id": r.get("chunk_id") or "",
                            "id": f"{kb_id}:{r['source']}>{r.get('type')}>{r['target']}:{r.get('chunk_id')}",
                        } for r in relations],
                    ).consume()

            created = s.execute_write(_tx)

        # execute_write 返回回调返回值;这里再查一次计数(轻量,管理端/流水线统计用)
        return {"entities": len(entities), "relations": len(relations)}

    def delete_document(self, kb_id: str, doc_id: str) -> Dict[str, int]:
        """删除某文档的全部图谱贡献:边按 doc_id 删,实体去引用,孤立实体删。"""
        with self._session() as s:
            def _tx(tx):
                edges = tx.run(
                    "MATCH (:Entity {kb_id: $kb})-[r:RELATES {kb_id: $kb, doc_id: $doc}]->(:Entity) "
                    "DELETE r RETURN count(r) AS c",
                    kb=kb_id, doc=doc_id,
                ).single()["c"]
                nodes = tx.run(
                    """
                    MATCH (e:Entity {kb_id: $kb})
                    WHERE $doc IN e.source_docs
                    SET e.source_docs = [d IN e.source_docs WHERE d <> $doc]
                    WITH e WHERE size(e.source_docs) = 0
                    DETACH DELETE e RETURN count(e) AS c
                    """,
                    kb=kb_id, doc=doc_id,
                ).single()["c"]
                return {"edges": int(edges), "orphanEntities": int(nodes)}
            return s.execute_write(_tx)

    def delete_kb(self, kb_id: str) -> Dict[str, int]:
        """整库删除(一句子图清除)。"""
        with self._session() as s:
            def _tx(tx):
                nodes = tx.run(
                    "MATCH (e:Entity {kb_id: $kb}) DETACH DELETE e RETURN count(e) AS c",
                    kb=kb_id,
                ).single()["c"]
                return {"entities": int(nodes)}
            return s.execute_write(_tx)

    # ── 查询(在线浏览 + 检索) ─────────────────────────────

    def counts(self, kb_id: str) -> Dict[str, int]:
        with self._session() as s:
            entities = s.run(
                "MATCH (e:Entity {kb_id: $kb}) RETURN count(e) AS c", kb=kb_id
            ).single()["c"]
            relations = s.run(
                "MATCH (:Entity {kb_id: $kb})-[r:RELATES {kb_id: $kb}]->(:Entity) "
                "RETURN count(r) AS c", kb=kb_id,
            ).single()["c"]
        return {"entities": int(entities), "relations": int(relations)}

    def document_counts(self, kb_id: str, doc_id: str) -> Dict[str, int]:
        """某文档在图谱中的贡献数(实体按 source_docs 引用,关系按 doc_id 归属)。"""
        with self._session() as s:
            entities = s.run(
                "MATCH (e:Entity {kb_id: $kb}) WHERE $doc IN e.source_docs "
                "RETURN count(e) AS c", kb=kb_id, doc=doc_id,
            ).single()["c"]
            relations = s.run(
                "MATCH (:Entity {kb_id: $kb})-[r:RELATES {kb_id: $kb, doc_id: $doc}]->(:Entity) "
                "RETURN count(r) AS c", kb=kb_id, doc=doc_id,
            ).single()["c"]
        return {"entities": int(entities), "relations": int(relations)}

    @staticmethod
    def _node_dict(record_node) -> Dict[str, Any]:
        """Neo4j Node → 前端友好的 dict(驼峰;normalized 供检索层做种子)。"""
        p = dict(record_node)
        return {
            "id": p.get("id") or "",
            "name": p.get("name") or p.get("normalized_name") or "",
            "normalized": p.get("normalized_name") or "",
            "type": p.get("type") or "",
            "description": p.get("description") or "",
            "aliases": list(p.get("aliases") or []),
            "sourceDocs": list(p.get("source_docs") or []),
            "typeStatus": p.get("type_status") or "approved",
            "updatedAt": p.get("updated_at") or "",
        }

    def get_graph(
        self,
        kb_id: str,
        q: str = "",
        node_types: Optional[List[str]] = None,
        limit_nodes: int = 80,
        limit_edges: int = 150,
    ) -> Dict[str, Any]:
        """图谱浏览首页数据:限量节点(可按名称/类型过滤)+ 其邻接边。"""
        clauses = ["e.kb_id = $kb"]
        params: Dict[str, Any] = {"kb": kb_id}
        if q:
            clauses.append("(toLower(e.name) CONTAINS toLower($q) OR toLower(e.description) CONTAINS toLower($q))")
            params["q"] = q
        if node_types:
            clauses.append("e.type IN $types")
            params["types"] = node_types
        where = " AND ".join(clauses)

        with self._session() as s:
            node_rows = s.run(
                f"MATCH (e:Entity) WHERE {where} "
                f"RETURN e ORDER BY e.updated_at DESC LIMIT $n",
                n=limit_nodes, **params,
            ).data()
            nodes = [self._node_dict(r["e"]) for r in node_rows]
            ids = [n["id"] for n in nodes]
            edge_rows = s.run(
                "MATCH (a:Entity)-[r:RELATES {kb_id: $kb}]->(b:Entity) "
                "WHERE a.id IN $ids AND b.id IN $ids "
                "RETURN a.id AS source, b.id AS target, properties(r) AS r "
                "LIMIT $m",
                kb=kb_id, ids=ids, m=limit_edges,
            ).data()

        edges = [{
            "id": e["r"].get("id") or "",
            "source": e["source"], "target": e["target"],
            "type": e["r"].get("type") or "",
            "description": e["r"].get("description") or "",
            "evidence": e["r"].get("evidence") or "",
            "docId": e["r"].get("doc_id") or "",
        } for e in edge_rows]
        return {"nodes": nodes, "edges": edges}

    def expand_node(
        self, kb_id: str, node_id: str, limit_nodes: int = 40, limit_edges: int = 80,
    ) -> Dict[str, Any]:
        """点击节点增量展开:1 跳邻域(双向)。"""
        with self._session() as s:
            rows = s.run(
                "MATCH (a:Entity {kb_id: $kb})-[r:RELATES {kb_id: $kb}]-(b:Entity) "
                "WHERE a.id = $nid "
                "RETURN a AS center, b AS neighbor, type(r) AS ignored, "
                "       startNode(r).id AS sid, endNode(r).id AS tid, properties(r) AS props "
                "LIMIT $m",
                kb=kb_id, nid=node_id, m=limit_edges,
            ).data()
        center = self._node_dict(rows[0]["center"]) if rows else None
        nodes = [center] if center else []
        seen = {n["id"] for n in nodes}
        edges = []
        for r in rows:
            neighbor = self._node_dict(r["neighbor"])
            if neighbor["id"] not in seen and len(nodes) < limit_nodes + 1:
                nodes.append(neighbor)
                seen.add(neighbor["id"])
            edges.append({
                "id": r["props"].get("id") or "",
                "source": r["sid"], "target": r["tid"],
                "type": r["props"].get("type") or "",
                "description": r["props"].get("description") or "",
                "evidence": r["props"].get("evidence") or "",
                "docId": r["props"].get("doc_id") or "",
            })
        return {"nodes": nodes, "edges": edges}

    def find_entities(
        self, kb_id: str, terms: List[str], limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按名称词找种子实体:精确(normalized)优先,包含匹配兜底。"""
        if not terms:
            return []
        normalized = [normalize_name(t) for t in terms if t.strip()]
        if not normalized:
            return []
        with self._session() as s:
            rows = s.run(
                """
                MATCH (e:Entity {kb_id: $kb})
                WHERE e.normalized_name IN $terms
                RETURN e LIMIT $n
                """,
                kb=kb_id, terms=normalized, n=limit,
            ).data()
            if not rows:
                # 前缀匹配先试:能吃 (kb_id, name) 上的 range 索引
                # (kg_entity_name),不用全表扫
                rows = s.run(
                    """
                    UNWIND $terms AS t
                    MATCH (e:Entity {kb_id: $kb})
                    WHERE e.normalized_name STARTS WITH t
                    RETURN DISTINCT e LIMIT $n
                    """,
                    kb=kb_id, terms=normalized, n=limit,
                ).data()
            if not rows:
                # 子串兜底:CONTAINS 无法用索引,按 kb 限定扫描(仅当前库,
                # 库内实体量级可控;大库场景应优先命中前两层)
                rows = s.run(
                    """
                    UNWIND $terms AS t
                    MATCH (e:Entity {kb_id: $kb})
                    WHERE e.normalized_name CONTAINS t
                    RETURN DISTINCT e LIMIT $n
                    """,
                    kb=kb_id, terms=normalized, n=limit,
                ).data()
        return [self._node_dict(r["e"]) for r in rows]

    def subgraph_around(
        self,
        kb_id: str,
        seed_names: List[str],
        hops: int = 2,
        max_nodes: int = 80,
        max_edges: int = 150,
    ) -> Dict[str, Any]:
        """BFS 扩展种子实体的邻域子图(Python 侧逐跳,便于限量)。

        seed_names 传 normalized_name 列表。
        """
        visited: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        edge_keys = set()
        frontier = list(dict.fromkeys(seed_names))[:max_nodes]
        # 种子节点先落盘(可能有的名字查不到实体,查不到就跳过)
        if frontier:
            with self._session() as s:
                rows = s.run(
                    "MATCH (e:Entity {kb_id: $kb}) WHERE e.normalized_name IN $names RETURN e",
                    kb=kb_id, names=frontier,
                ).data()
            for r in rows:
                node = self._node_dict(r["e"])
                visited[node["id"]] = node
            frontier = [n for n in frontier
                        if entity_node_id(kb_id, n) in visited]

        for _hop in range(max(1, hops)):
            if not frontier or len(visited) >= max_nodes or len(edges) >= max_edges:
                break
            with self._session() as s:
                rows = s.run(
                    """
                    MATCH (a:Entity {kb_id: $kb})-[r:RELATES {kb_id: $kb}]-(b:Entity {kb_id: $kb})
                    WHERE a.normalized_name IN $frontier
                    RETURN b AS nb, startNode(r).id AS sid, endNode(r).id AS tid,
                           properties(r) AS props
                    LIMIT $m
                    """,
                    kb=kb_id, frontier=frontier,
                    m=max_edges - len(edges),
                ).data()
            next_frontier = []
            for r in rows:
                node = self._node_dict(r["nb"])
                if node["id"] not in visited:
                    if len(visited) < max_nodes:
                        visited[node["id"]] = node
                        # BFS 下一跳按 normalized_name 匹配(查询侧匹配的就是
                        # normalized_name;用原始 name 会让大写/全角实体在
                        # hop≥2 时静默匹配落空,多跳检索被截断成一跳)
                        next_frontier.append(node["normalized"])
                eid = r["props"].get("id") or f"{r['sid']}->{r['tid']}"
                if eid not in edge_keys:
                    edge_keys.add(eid)
                    edges.append({
                        "id": eid, "source": r["sid"], "target": r["tid"],
                        "type": r["props"].get("type") or "",
                        "description": r["props"].get("description") or "",
                        "evidence": r["props"].get("evidence") or "",
                        "docId": r["props"].get("doc_id") or "",
                    })
            frontier = next_frontier

        # 只保留两端都在 visited 里的边(限量丢弃的节点对应边不成环)
        valid_ids = set(visited.keys())
        edges = [e for e in edges if e["source"] in valid_ids and e["target"] in valid_ids]
        return {"nodes": list(visited.values()), "edges": edges[:max_edges]}


# ── 进程级单例(设置热改时重建) ────────────────────────────────

_cached_store: Optional[Neo4jGraphStore] = None
_cached_fp: tuple = ()
_cache_lock = threading.Lock()     # 只保护缓存指针读写(锁内零网络 IO)
_build_lock = threading.Lock()     # 串行化连接构建(网络 IO 在锁外做)


def get_graph_store(settings: Dict[str, Any]) -> Neo4jGraphStore:
    """按解析后的插件配置取/建图存储单例(指纹 = 连接四元组)。

    锁纪律:缓存命中路径只拿 _cache_lock(微秒级);连接构建(含
    ensure_constraints 的网络往返,Neo4j 慢时秒级)在锁外做——否则一个
    重建动作会让所有并发 /search 与导入线程在锁上排队。
    失败纪律:构建失败清空缓存,绝不留下"已 close 却仍被缓存"的 driver
    (否则配置回退到旧指纹时会持续返回坏连接)。
    """
    global _cached_store, _cached_fp
    fp = (
        settings.get("neo4j_uri"), settings.get("neo4j_user"),
        settings.get("neo4j_password"), settings.get("neo4j_database"),
    )
    with _cache_lock:
        if _cached_store is not None and _cached_fp == fp:
            return _cached_store

    with _build_lock:
        with _cache_lock:   # 双检:排队期间可能已被同指纹线程建好
            if _cached_store is not None and _cached_fp == fp:
                return _cached_store
        try:
            store = Neo4jGraphStore(
                uri=fp[0], user=fp[1] or "neo4j", password=fp[2] or "",
                database=fp[3] or "neo4j",
            )
            store.ensure_constraints()
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


def reset_graph_store_cache() -> None:
    """测试辅助:清单例。"""
    global _cached_store, _cached_fp
    with _cache_lock:
        if _cached_store is not None:
            _cached_store.close()
        _cached_store, _cached_fp = None, ()
