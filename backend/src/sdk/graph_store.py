"""Neo4jGraphStore —— SDK 通用图存储(Neo4j 5,官方 Python driver)。

【模块定位】
从 knowledge_graph 插件下沉的通用图谱设施:带 scope 命名空间隔离的
实体/关系存储与邻域查询。零领域知识——不认识知识库/文档/本体;实体
只是"有名字和类型的节点",关系只是"有类型的边"。任何需要图结构的
插件(知识图谱/血缘/组织关系…)都可复用。

【图模型】(scope 物理隔离;标签/属性名可由构造参数定制)
  (:{node_label} {id, {scope_prop}, name, normalized_name, type,
                  description, aliases[], source_docs[], type_status,
                  created_at, updated_at})
    - ({scope_prop}, normalized_name) IS UNIQUE = MERGE 幂等锚点
    - id = "{scope}:{normalized_name}"(前端图渲染的稳定节点 ID)
    - source_docs = 引用该实体的"来源组"集合(删除时做引用计数)
  [:{rel_type} {id, {scope_prop}, doc_id, chunk_id, source_key, target_key,
                type, description, evidence, created_at, updated_at}]
    - MERGE 键 = (scope, source_key, target_key, type, chunk_id):
      同一来源组重导先清理后写入,天然幂等;不同来源组对同一对节点的
      同型关系各自保留(带各自 doc/chunk 溯源)

【命名空间隔离(与 scope_registry 契约配套)】
  - prefix:约束名/索引名前缀({prefix}entity_key / {prefix}entity_name),
    每个使用方插件声明自己的前缀并 register_prefix 登记;
  - scope_id 契约:所有方法首参 scope 必须是服务端签发的不可预测 ID
    (scope_registry.new_scope_id() 或调用方自己的 UUID),禁止用户输入
    直传——入口用 is_scope_id_safe 防御。

【连接管理】进程级单例 + 指纹缓存(含 prefix):配置热改时重建 driver、
关闭旧连接;双检锁纪律——缓存命中只拿轻锁,连接构建在锁外。所有
方法线程安全(driver 自带连接池)。

【测试替身】单测用内存 Fake 替换(见 tests/),不要求真实 Neo4j;
GraphStore Protocol 声明了替身需实现的完整方法集。
"""
import logging
import threading
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


def entity_node_id(scope: str, normalized_name: str) -> str:
    return f"{scope}:{normalized_name}"


class Neo4jGraphStore:
    """Neo4j 图存储(driver 直连;地址/凭证由调用方解析注入)。

    Args:
        prefix: 约束/索引名前缀(命名空间边界;调用方需先在
            scope_registry 登记该前缀)。
        node_label / rel_type / scope_prop: 图模型元素名,必传——
            使用方声明自己的图模型。
    """

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j",
                 prefix: str = "", node_label: str = "",
                 rel_type: str = "", scope_prop: str = ""):
        if not (prefix and node_label and rel_type and scope_prop):
            raise ValueError(
                "prefix/node_label/rel_type/scope_prop 均为必传,"
                "由使用方声明自己的图模型")
        from neo4j import GraphDatabase
        self._database = database or "neo4j"
        self._driver = GraphDatabase.driver(
            uri, auth=(user or "neo4j", password or "")
        )
        # 图模型参数(值只来自构造参数,非用户输入——Cypher 拼接无注入面)
        self._prefix = prefix
        self._label = node_label
        self._rel = rel_type
        self._sp = scope_prop

    def _check_scope(self, scope: str) -> None:
        """scope_id 契约入口防御(公共实现见 scope_registry.check_scope_id)。"""
        check_scope_id(scope)

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
                f"CREATE CONSTRAINT {self._prefix}entity_key IF NOT EXISTS "
                f"FOR (e:{self._label}) REQUIRE (e.{self._sp}, e.normalized_name) IS UNIQUE"
            ).consume()
            # name 前缀匹配走 range 索引即可满足 v1 检索;全文索引留待需要时加
            s.run(
                f"CREATE INDEX {self._prefix}entity_name IF NOT EXISTS "
                f"FOR (e:{self._label}) ON (e.{self._sp}, e.name)"
            ).consume()

    def _session(self):
        return self._driver.session(database=self._database)

    # ── 写入(导入流水线调用) ───────────────────────────────

    def upsert_batch(
        self,
        scope: str,
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
        self._check_scope(scope)
        now = _now()
        with self._session() as s:
            def _tx(tx):
                if entities:
                    tx.run(
                        f"""
                        UNWIND $rows AS ent
                        MERGE (e:{self._label} {{{self._sp}: $kb, normalized_name: ent.normalized_name}})
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
                            e.source_chunks = CASE WHEN ent.chunk_id = ''
                                         OR ent.chunk_id IN coalesce(e.source_chunks, [])
                                                 THEN coalesce(e.source_chunks, [])
                                                 ELSE coalesce(e.source_chunks, []) + ent.chunk_id END,
                            e.type_status = ent.type_status,
                            e.updated_at = $now
                        """,
                        kb=scope, now=now,
                        # 实体带 chunk_ids 列表(批内同名实体的多块溯源并集):
                        # 按来源块爆炸成多行,MERGE 同一节点逐块累积 source_chunks
                        rows=[{
                            "normalized_name": e["normalized_name"],
                            "id": entity_node_id(scope, e["normalized_name"]),
                            "name": e.get("name") or e["normalized_name"],
                            "type": e.get("type") or "",
                            "description": e.get("description") or "",
                            "aliases": list(dict.fromkeys(e.get("aliases") or [])),
                            "type_status": e.get("type_status") or "",
                            "chunk_id": cid,
                            "doc_id": doc_id,
                        } for e in entities
                          for cid in (e.get("chunk_ids") or [e.get("chunk_id") or ""])],
                    ).consume()
                if relations:
                    tx.run(
                        f"""
                        UNWIND $rows AS r
                        MATCH (s:{self._label} {{{self._sp}: $kb, normalized_name: r.source}})
                        MATCH (t:{self._label} {{{self._sp}: $kb, normalized_name: r.target}})
                        MERGE (s)-[rel:{self._rel} {{
                            {self._sp}: $kb, source_key: r.source, target_key: r.target,
                            type: r.type, chunk_id: r.chunk_id}}]->(t)
                        ON CREATE SET rel.created_at = $now, rel.id = r.id
                        SET rel.doc_id = $doc, rel.description = r.description,
                            rel.evidence = r.evidence, rel.updated_at = $now
                        """,
                        kb=scope, doc=doc_id, now=now,
                        rows=[{
                            "source": r["source"], "target": r["target"],
                            "type": r.get("type") or "",
                            "description": r.get("description") or "",
                            "evidence": (r.get("evidence") or "")[:500],
                            "chunk_id": r.get("chunk_id") or "",
                            "id": f"{scope}:{r['source']}>{r.get('type')}>{r['target']}:{r.get('chunk_id')}",
                        } for r in relations],
                    ).consume()

            created = s.execute_write(_tx)

        # execute_write 返回回调返回值;这里再查一次计数(轻量,管理端/流水线统计用)
        return {"entities": len(entities), "relations": len(relations)}

    def chunk_output(self, scope: str, chunk_id: str) -> Dict[str, List[Dict]]:
        """块级抽取产出(块明细"查看产出"用)。

        实体按 source_chunks 累积列表包含该块判定(实体跨块合并,溯源是
        多对多);关系按 chunk_id 精确匹配(写入时逐条带块 id)。
        """
        self._check_scope(scope)
        with self._session() as s:
            ent_rows = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) "
                f"WHERE $cid IN coalesce(e.source_chunks, []) RETURN e",
                kb=scope, cid=chunk_id,
            ).data()
            rel_rows = s.run(
                f"MATCH (a:{self._label})-[r:{self._rel} {{{self._sp}: $kb, chunk_id: $cid}}]"
                f"->(b:{self._label}) "
                f"RETURN a.name AS source, b.name AS target, properties(r) AS rel",
                kb=scope, cid=chunk_id,
            ).data()
        entities = [self._node_dict(r["e"]) for r in ent_rows]
        relations = [{
            "source": r["source"], "target": r["target"],
            "type": r["rel"].get("type") or "",
            "description": r["rel"].get("description") or "",
            "evidence": (r["rel"].get("evidence") or "")[:200],
        } for r in rel_rows]
        return {"entities": entities, "relations": relations}

    def list_entity_names(self, scope: str) -> set:
        """库内全部实体 normalized_name(悬空关系过滤的 known 集合扩充用)。"""
        self._check_scope(scope)
        with self._session() as s:
            rows = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) "
                f"RETURN e.normalized_name AS n",
                kb=scope,
            ).data()
        return {r["n"] for r in rows if r.get("n")}

    def list_entity_aliases(self, scope: str) -> Dict[str, Dict[str, Any]]:
        """库内全部实体的别名+类型+描述表:normalized_name -> {type, aliases[], description}。

        导入流水线批次间用它做实体消歧:图里某实体已经积累了别名(如
        张小凡 的别名含"鬼厉"),后续批次抽到"鬼厉"时就有据可依地把
        它并回张小凡,而不是另立节点。type 随附,合并前做同类型校验;
        description 随附,词表渲染属性提示(指称消解的特征对卡材料)。
        """
        self._check_scope(scope)
        with self._session() as s:
            rows = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) "
                f"RETURN e.normalized_name AS n, coalesce(e.type, '') AS t, "
                f"       coalesce(e.aliases, []) AS a, "
                f"       coalesce(e.description, '') AS d",
                kb=scope,
            ).data()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if r.get("n"):
                out[r["n"]] = {
                    "type": r.get("t") or "",
                    "aliases": list(dict.fromkeys(x for x in (r.get("a") or []) if x)),
                    "description": r.get("d") or "",
                }
        return out

    def prune_aliases(self, scope: str, names: List[str]) -> int:
        """从所有实体的 aliases 里移除指定名字(收尾去争议)。

        入库前的独占性校验只保证"本批不扩散",历史遗留/合并吸收进来的
        泛称别名仍在图里——词表从图渲染,它们会被持续喂给后续抽取
        (回音室通道)。收尾时把争议别名集清掉,反馈即断。幂等。

        Returns: 实际移除的别名声称条数。
        """
        self._check_scope(scope)
        if not names:
            return 0
        now = _now()
        with self._session() as s:
            rows = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) "
                f"WHERE any(a IN coalesce(e.aliases, []) WHERE a IN $names) "
                f"WITH e, [a IN coalesce(e.aliases, []) WHERE a IN $names] AS rm "
                f"SET e.aliases = [a IN coalesce(e.aliases, []) WHERE NOT a IN $names], "
                f"    e.updated_at = $now "
                f"RETURN sum(size(rm)) AS removed",
                kb=scope, names=names, now=now,
            ).data()
        return int((rows[0] or {}).get("removed") or 0) if rows else 0

    def list_entities(self, scope: str) -> List[Dict]:
        """库内全部实体(含 type/aliases/description/块溯源量/创建时间)。

        created_at 供消歧时选 canonical:最早入库 = 角色首次登场的名字
        (张小凡 先于 鬼厉 入库,合并后保留张小凡);description 供收尾
        描述连边做点名匹配。
        """
        self._check_scope(scope)
        with self._session() as s:
            rows = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) "
                f"RETURN e.normalized_name AS n, e.name AS name, e.type AS type, "
                f"       coalesce(e.aliases, []) AS a, e.source_chunks AS sc, "
                f"       coalesce(e.description, '') AS d, "
                f"       coalesce(e.created_at, '') AS cat",
                kb=scope,
            ).data()
        out = []
        for r in rows:
            if not r.get("n"):
                continue
            out.append({
                "normalized": r["n"], "name": r.get("name") or r["n"],
                "type": r.get("type") or "",
                "aliases": list(dict.fromkeys(x for x in (r.get("a") or []) if x)),
                "source_chunks": list(r.get("sc") or []),
                "description": r.get("d") or "",
                "created_at": r.get("cat") or "",
            })
        return out

    def list_connected_pairs(self, scope: str) -> set:
        """库内全部有边相连的实体对(无向 normalized_name 二元组)。

        收尾描述连边去重用:两实体间已有任何关系就不再叠加兜底弱边。
        """
        self._check_scope(scope)
        with self._session() as s:
            rows = s.run(
                f"MATCH (a:{self._label} {{{self._sp}: $kb}})"
                f"-[:{self._rel} {{{self._sp}: $kb}}]->"
                f"(b:{self._label} {{{self._sp}: $kb}}) "
                f"WHERE a.normalized_name <> b.normalized_name "
                f"RETURN DISTINCT a.normalized_name AS a, b.normalized_name AS b",
                kb=scope,
            ).data()
        out = set()
        for r in rows:
            a, b = r.get("a"), r.get("b")
            if a and b:
                out.add((a, b))
                out.add((b, a))
        return out

    def merge_entities(self, scope: str, doc_id: str,
                       pairs: List[Dict]) -> Dict[str, int]:
        """实体消歧合并(单事务):把碎片实体并进 canonical 实体。

        pairs: [{"canonical": 标准名(normalized), "fragment": 碎片名(normalized)}]
        只处理 pairs 明确声明的对——绝不启发式猜测,避免误并(如
        "青云山/青云门"这种真·不同实体)。合并语义:
          1. 碎片 aliases + 碎片名 并入 canonical 的 aliases(去重)——
             碎片名必须保留,否则碎片节点删除后按别名检索就丢了;
          2. canonical 并集碎片的 source_docs / source_chunks(溯源不丢);
          3. 碎片的进出边**迁移**到 canonical(按 upsert 同款 MERGE 键
             (source_key,target_key,type,chunk_id) 落新边,证据补齐后删
             旧边——只改关系属性不移端点,DETACH DELETE 会连边一起删,
             那是丢关系不是合并);
          4. canonical↔碎片 之间的直连边是合并后的自环,直接删;
          5. DETACH DELETE 碎片节点。

        Returns: {"merged": 成功合并的对数}
        """
        self._check_scope(scope)
        if not pairs:
            return {"merged": 0}
        now = _now()
        with self._session() as s:
            def _tx(tx):
                n = 0
                for p in pairs:
                    canon = str((p or {}).get("canonical") or "").strip()
                    frag = str((p or {}).get("fragment") or "").strip()
                    if not canon or not frag or canon == frag:
                        continue
                    # ── 0) canonical↔碎片 直连边 = 合并后自环,删 ──
                    tx.run(
                        f"MATCH (c:{self._label} {{{self._sp}: $kb, normalized_name: $canon}})"
                        f"-[self_r:{self._rel} {{{self._sp}: $kb}}]-"
                        f"(f:{self._label} {{{self._sp}: $kb, normalized_name: $frag}}) "
                        f"DELETE self_r",
                        kb=scope, canon=canon, frag=frag,
                    ).consume()
                    # ── 1) 出边迁移:f→x 落成 c→x(MERGE 键同 upsert,幂等) ──
                    tx.run(
                        f"""
                        MATCH (f:{self._label} {{{self._sp}: $kb, normalized_name: $frag}})
                        MATCH (f)-[r:{self._rel} {{{self._sp}: $kb}}]->(x:{self._label})
                        MATCH (c:{self._label} {{{self._sp}: $kb, normalized_name: $canon}})
                        MERGE (c)-[r2:{self._rel} {{
                            {self._sp}: $kb, source_key: $canon,
                            target_key: x.normalized_name, type: r.type,
                            chunk_id: r.chunk_id}}]->(x)
                        ON CREATE SET r2.created_at = r.created_at,
                                      r2.id = $kb + ':' + $canon + '>' + r.type
                                              + '>' + x.normalized_name + ':' + r.chunk_id
                        SET r2.doc_id = coalesce(r2.doc_id, r.doc_id),
                            r2.description = CASE WHEN coalesce(r2.description,'') = ''
                                                  THEN r.description ELSE r2.description END,
                            r2.evidence = CASE WHEN coalesce(r2.evidence,'') = ''
                                               THEN r.evidence ELSE r2.evidence END,
                            r2.updated_at = $now
                        DELETE r
                        """,
                        kb=scope, canon=canon, frag=frag, now=now,
                    ).consume()
                    # ── 2) 入边迁移:x→f 落成 x→c ──
                    tx.run(
                        f"""
                        MATCH (f:{self._label} {{{self._sp}: $kb, normalized_name: $frag}})
                        MATCH (x:{self._label})-[r:{self._rel} {{{self._sp}: $kb}}]->(f)
                        MATCH (c:{self._label} {{{self._sp}: $kb, normalized_name: $canon}})
                        MERGE (x)-[r2:{self._rel} {{
                            {self._sp}: $kb, source_key: x.normalized_name,
                            target_key: $canon, type: r.type,
                            chunk_id: r.chunk_id}}]->(c)
                        ON CREATE SET r2.created_at = r.created_at,
                                      r2.id = $kb + ':' + x.normalized_name + '>' + r.type
                                              + '>' + $canon + ':' + r.chunk_id
                        SET r2.doc_id = coalesce(r2.doc_id, r.doc_id),
                            r2.description = CASE WHEN coalesce(r2.description,'') = ''
                                                  THEN r.description ELSE r2.description END,
                            r2.evidence = CASE WHEN coalesce(r2.evidence,'') = ''
                                               THEN r.evidence ELSE r2.evidence END,
                            r2.updated_at = $now
                        DELETE r
                        """,
                        kb=scope, canon=canon, frag=frag, now=now,
                    ).consume()
                    # ── 3) 属性并集:别名(含碎片名)+ 溯源 ──
                    ok = tx.run(
                        f"""
                        MATCH (c:{self._label} {{{self._sp}: $kb, normalized_name: $canon}})
                        MATCH (f:{self._label} {{{self._sp}: $kb, normalized_name: $frag}})
                        SET c.aliases = reduce(acc = coalesce(c.aliases, []),
                                               a IN coalesce(f.aliases, []) + [$frag] |
                                               CASE WHEN a = $canon OR a IN acc THEN acc
                                                    ELSE acc + a END),
                            c.source_docs = reduce(acc = coalesce(c.source_docs, []),
                                                   d IN coalesce(f.source_docs, []) |
                                                   CASE WHEN d IN acc THEN acc ELSE acc + d END),
                            c.source_chunks = reduce(acc = coalesce(c.source_chunks, []),
                                                     ch IN coalesce(f.source_chunks, []) |
                                                     CASE WHEN ch IN acc THEN acc ELSE acc + ch END),
                            c.updated_at = $now
                        DETACH DELETE f
                        RETURN count(c) AS ok
                        """,
                        kb=scope, canon=canon, frag=frag, now=now,
                    ).single()
                    n += int((ok or {}).get("ok") or 0)
                return n
            merged = s.execute_write(_tx)
        return {"merged": int(merged)}

    def delete_document(self, scope: str, doc_id: str) -> Dict[str, int]:
        """删除某文档的全部图谱贡献:边按 doc_id 删,实体去引用,孤立实体删。"""
        self._check_scope(scope)
        with self._session() as s:
            def _tx(tx):
                edges = tx.run(
                    f"MATCH (:{self._label} {{{self._sp}: $kb}})-[r:{self._rel} {{{self._sp}: $kb, doc_id: $doc}}]->(:{self._label}) "
                    f"DELETE r RETURN count(r) AS c",
                    kb=scope, doc=doc_id,
                ).single()["c"]
                nodes = tx.run(
                    f"""
                    MATCH (e:{self._label} {{{self._sp}: $kb}})
                    WHERE $doc IN e.source_docs
                    SET e.source_docs = [d IN e.source_docs WHERE d <> $doc]
                    WITH e WHERE size(e.source_docs) = 0
                    DETACH DELETE e RETURN count(e) AS c
                    """,
                    kb=scope, doc=doc_id,
                ).single()["c"]
                return {"edges": int(edges), "orphanEntities": int(nodes)}
            return s.execute_write(_tx)

    def delete_scope(self, scope: str) -> Dict[str, int]:
        """整库删除(一句子图清除)。"""
        self._check_scope(scope)
        with self._session() as s:
            def _tx(tx):
                nodes = tx.run(
                    f"MATCH (e:{self._label} {{{self._sp}: $kb}}) DETACH DELETE e RETURN count(e) AS c",
                    kb=scope,
                ).single()["c"]
                return {"entities": int(nodes)}
            return s.execute_write(_tx)

    # ── 查询(在线浏览 + 检索) ─────────────────────────────

    def counts(self, scope: str) -> Dict[str, int]:
        """库级统计。关系数按 (source,type,target) 去重(平行边=块级留痕,
        不去重会虚增数倍,与 document_counts 同语义)。"""
        self._check_scope(scope)
        with self._session() as s:
            entities = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) RETURN count(e) AS c", kb=scope
            ).single()["c"]
            relations = s.run(
                f"MATCH (a:{self._label} {{{self._sp}: $kb}})"
                f"-[r:{self._rel} {{{self._sp}: $kb}}]->"
                f"(b:{self._label} {{{self._sp}: $kb}}) "
                f"RETURN count(DISTINCT [a.id, r.type, b.id]) AS c",
                kb=scope,
            ).single()["c"]
        return {"entities": int(entities), "relations": int(relations)}

    def document_counts(self, scope: str, doc_id: str) -> Dict[str, int]:
        """某文档在图谱中的贡献数(实体按 source_docs 引用,关系按 doc_id 归属)。

        关系计数按 (source,type,target) 去重——平行边是块级留痕(每块一条),
        重复计入会让文档的 relation_count 虚增数倍,与用户直觉的"关系数"不符。
        """
        self._check_scope(scope)
        with self._session() as s:
            entities = s.run(
                f"MATCH (e:{self._label} {{{self._sp}: $kb}}) WHERE $doc IN e.source_docs "
                f"RETURN count(e) AS c", kb=scope, doc=doc_id,
            ).single()["c"]
            relations = s.run(
                f"MATCH (a:{self._label} {{{self._sp}: $kb}})"
                f"-[r:{self._rel} {{{self._sp}: $kb, doc_id: $doc}}]->"
                f"(b:{self._label} {{{self._sp}: $kb}}) "
                f"RETURN count(DISTINCT [a.id, r.type, b.id]) AS c",
                kb=scope, doc=doc_id,
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
            "typeStatus": p.get("type_status") or "",
            "updatedAt": p.get("updated_at") or "",
        }

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
        clauses = [f"e.{self._sp} = $kb"]
        params: Dict[str, Any] = {"kb": scope}
        if q:
            clauses.append("(toLower(e.name) CONTAINS toLower($q) OR toLower(e.description) CONTAINS toLower($q))")
            params["q"] = q
        if node_types:
            clauses.append("e.type IN $types")
            params["types"] = node_types
        where = " AND ".join(clauses)

        with self._session() as s:
            node_rows = s.run(
                f"MATCH (e:{self._label}) WHERE {where} "
                f"RETURN e ORDER BY e.updated_at DESC LIMIT $n",
                n=limit_nodes, **params,
            ).data()
            nodes = [self._node_dict(r["e"]) for r in node_rows]
            ids = [n["id"] for n in nodes]
            # 同一对节点的同类型关系跨块会存多条(块级留痕:MERGE 键含
            # chunk_id)。浏览视图按 (source,type,target) 聚合为一条:
            # evidence 收集全部、count 记录被多少块支持——图里块级数据
            # 原样保留,只是不再平行画 N 条
            edge_rows = s.run(
                f"MATCH (a:{self._label})-[r:{self._rel} {{{self._sp}: $kb}}]->(b:{self._label}) "
                f"WHERE a.id IN $ids AND b.id IN $ids "
                # collect(properties(r)) 而非 collect(r):关系对象经 .data()
                # 序列化后不是 dict,下游 .get() 直接 TypeError(get_graph
                # 线上 500 的根因;expand_node 返回的是 Node 走 _node_dict 无此问题)
                f"WITH a.id AS source, b.id AS target, r.type AS rtype, "
                f"collect(properties(r)) AS rs "
                f"RETURN source, target, rtype, rs, size(rs) AS cnt "
                f"ORDER BY cnt DESC LIMIT $m",
                kb=scope, ids=ids, m=limit_edges,
            ).data()

        edges = []
        for e in edge_rows:
            rs = e["rs"]
            # 展示字段取"证据最全"的那条(有 evidence 的优先),其余证据并入列表
            best = next((r for r in rs if r.get("evidence")), rs[0])
            evidences = [r.get("evidence") for r in rs if r.get("evidence")]
            edges.append({
                "id": best.get("id") or "",
                "source": e["source"], "target": e["target"],
                "type": e["rtype"],
                "description": best.get("description") or "",
                "evidence": best.get("evidence") or "",
                "evidences": evidences[:5],   # 跨块支持的全部证据(截前 5 条)
                "supportChunks": e["cnt"],    # 被多少个块抽出——关系强度的天然信号
                "docId": best.get("doc_id") or "",
            })
        return {"nodes": nodes, "edges": edges}

    def expand_node(
        self, scope: str, node_id: str, limit_nodes: int = 40, limit_edges: int = 80,
    ) -> Dict[str, Any]:
        """点击节点增量展开:1 跳邻域(双向)。边按 (source,type,target)
        聚合(跨块平行边合并,与 get_graph 同语义)。"""
        self._check_scope(scope)
        with self._session() as s:
            rows = s.run(
                f"MATCH (a:{self._label} {{{self._sp}: $kb}})-[r:{self._rel} {{{self._sp}: $kb}}]-(b:{self._label}) "
                f"WHERE a.id = $nid "
                f"WITH a AS center, b AS neighbor, "
                f"     startNode(r).id AS sid, endNode(r).id AS tid, r.type AS rtype, "
                f"     collect(properties(r)) AS rs "
                f"RETURN center, neighbor, sid, tid, rtype, rs, size(rs) AS cnt "
                f"LIMIT $m",
                kb=scope, nid=node_id, m=limit_edges,
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
            rs = r["rs"]
            best = next((x for x in rs if x.get("evidence")), rs[0])
            edges.append({
                "id": best.get("id") or "",
                "source": r["sid"], "target": r["tid"],
                "type": r["rtype"],
                "description": best.get("description") or "",
                "evidence": best.get("evidence") or "",
                "evidences": [x.get("evidence") for x in rs if x.get("evidence")][:5],
                "supportChunks": r["cnt"],
                "docId": best.get("doc_id") or "",
            })
        return {"nodes": nodes, "edges": edges}

    def find_entities(
        self, scope: str, terms: List[str], limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按名称词找种子实体:精确(normalized)优先,包含匹配兜底。"""
        self._check_scope(scope)
        if not terms:
            return []
        normalized = [normalize_name(t) for t in terms if t.strip()]
        if not normalized:
            return []
        with self._session() as s:
            rows = s.run(
                f"""
                MATCH (e:{self._label} {{{self._sp}: $kb}})
                WHERE e.normalized_name IN $terms
                   OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) IN $terms)
                RETURN e LIMIT $n
                """,
                kb=scope, terms=normalized, n=limit,
            ).data()
            if not rows:
                # 前缀匹配先试:能吃 (scope, name) 上的 range 索引
                # (kg_entity_name),不用全表扫
                rows = s.run(
                    f"""
                    UNWIND $terms AS t
                    MATCH (e:{self._label} {{{self._sp}: $kb}})
                    WHERE e.normalized_name STARTS WITH t
                       OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) STARTS WITH t)
                    RETURN DISTINCT e LIMIT $n
                    """,
                    kb=scope, terms=normalized, n=limit,
                ).data()
            if not rows:
                # 子串兜底:CONTAINS 无法用索引,按 kb 限定扫描(仅当前库,
                # 库内实体量级可控;大库场景应优先命中前两层)
                rows = s.run(
                    f"""
                    UNWIND $terms AS t
                    MATCH (e:{self._label} {{{self._sp}: $kb}})
                    WHERE e.normalized_name CONTAINS t
                       OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS t)
                    RETURN DISTINCT e LIMIT $n
                    """,
                    kb=scope, terms=normalized, n=limit,
                ).data()
        return [self._node_dict(r["e"]) for r in rows]

    def subgraph_around(
        self,
        scope: str,
        seed_names: List[str],
        hops: int = 2,
        max_nodes: int = 80,
        max_edges: int = 150,
    ) -> Dict[str, Any]:
        """BFS 扩展种子实体的邻域子图(Python 侧逐跳,便于限量)。

        seed_names 传 normalized_name 列表。
        """
        self._check_scope(scope)
        visited: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        edge_keys = set()
        frontier = list(dict.fromkeys(seed_names))[:max_nodes]
        # 种子节点先落盘(可能有的名字查不到实体,查不到就跳过)
        if frontier:
            with self._session() as s:
                rows = s.run(
                    f"MATCH (e:{self._label} {{{self._sp}: $kb}}) WHERE e.normalized_name IN $names RETURN e",
                    kb=scope, names=frontier,
                ).data()
            for r in rows:
                node = self._node_dict(r["e"])
                visited[node["id"]] = node
            frontier = [n for n in frontier
                        if entity_node_id(scope, n) in visited]

        for _hop in range(max(1, hops)):
            if not frontier or len(visited) >= max_nodes or len(edges) >= max_edges:
                break
            with self._session() as s:
                rows = s.run(
                    f"""
                    MATCH (a:{self._label} {{{self._sp}: $kb}})-[r:{self._rel} {{{self._sp}: $kb}}]-(b:{self._label} {{{self._sp}: $kb}})
                    WHERE a.normalized_name IN $frontier
                    WITH b AS nb, startNode(r).id AS sid, endNode(r).id AS tid,
                         r.type AS rtype, collect(properties(r)) AS rs
                    RETURN nb, sid, tid, rtype, rs
                    LIMIT $m
                    """,
                    kb=scope, frontier=frontier,
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
                # 平行边聚合键 = (source,type,target):跨块的块级留痕边
                # 不聚合的话会占满 max_edges 限额,把真正的多跳关系挤出去
                ekey = (r["sid"], r["rtype"], r["tid"])
                if ekey not in edge_keys:
                    edge_keys.add(ekey)
                    rs = r["rs"]
                    best = next((x for x in rs if x.get("evidence")), rs[0] if rs else {})
                    edges.append({
                        "id": best.get("id") or f"{r['sid']}->{r['tid']}",
                        "source": r["sid"], "target": r["tid"],
                        "type": r["rtype"],
                        "description": best.get("description") or "",
                        "evidence": best.get("evidence") or "",
                        "docId": best.get("doc_id") or "",
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


def get_graph_store(settings: Dict[str, Any], **model_kwargs: Any) -> Neo4jGraphStore:
    """按解析后的配置取/建图存储单例(指纹 = 连接四元组 + 图模型参数)。

    model_kwargs 透传 Neo4jGraphStore 构造参数(prefix/node_label/
    rel_type/scope_prop)——不同插件的图模型不同,指纹必须含它们,
    否则同连接下两个前缀的插件会互相拿到对方的缓存对象。

    锁纪律:缓存命中路径只拿 _cache_lock(微秒级);连接构建(含
    ensure_constraints 的网络往返,Neo4j 慢时秒级)在锁外做——否则一个
    重建动作会让所有并发 /search 与导入线程在锁上排队。
    失败纪律:构建失败清缓存,绝不留下"已 close 却仍被缓存"的 driver
    (否则配置回退到旧指纹时会持续返回坏连接)。
    """
    global _cached_store, _cached_fp
    fp = (
        settings.get("neo4j_uri"), settings.get("neo4j_user"),
        settings.get("neo4j_password"), settings.get("neo4j_database"),
        tuple(sorted(model_kwargs.items())),
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
                database=fp[3] or "neo4j", **model_kwargs,
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
