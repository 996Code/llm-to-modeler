"""PgGraphStore 真实 PG 契约测试(替代 Neo4j 适配器;不用内存 Fake)。

覆盖 GraphStore 协议 17 方法在 PostgreSQL 上的行为:写入幂等/合并/原子性、
块级平行边、实体合并迁移、删除清理、三层名称匹配、BFS 限量与返回形状。
边物理键 = (scope,source,target,type,doc_id,chunk_id):相对旧后端补上了
doc_id(旧键空 chunk 跨文档互相覆盖,属有意修复,单独断言)。
"""
import uuid

import pytest

from sdk.graph_store import entity_node_id, normalize_name


@pytest.fixture()
def store():
    from sdk.graph_store import PgGraphStore, reset_graph_store_cache
    reset_graph_store_cache()
    s = PgGraphStore("knowledge_graph")
    yield s
    s.close()


def _scope() -> str:
    return str(uuid.uuid4())


def _ent(name, typ="person", chunk="c1", aliases=(), desc="", status="approved"):
    return {"name": name, "normalized_name": normalize_name(name), "type": typ,
            "description": desc, "aliases": list(aliases), "type_status": status,
            "chunk_id": chunk}


def _rel(src, tgt, chunk="c1", typ="相关", desc="", evidence="", doc=None):
    return {"source": normalize_name(src), "target": normalize_name(tgt),
            "type": typ, "description": desc, "evidence": evidence,
            "chunk_id": chunk, **({"doc_id": doc} if doc else {})}


def _rows(store, sql, params=()):
    with store._db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── scope 契约与基础写入 ─────────────────────────────────────


def test_rejects_invalid_scope(store):
    with pytest.raises(ValueError):
        store.upsert_batch("not-a-uuid", "d1", [], [])


def test_upsert_creates_nodes_and_edges(store):
    kb = _scope()
    out = store.upsert_batch(kb, "doc1", [_ent("甲"), _ent("乙")],
                             [_rel("甲", "乙", evidence="甲认识乙")])
    assert out == {"entities": 2, "relations": 1}
    nodes = {n["normalized"]: n for n in store.list_entities(kb)}
    assert set(nodes) == {"甲", "乙"}
    g = store.get_graph(kb)
    assert entity_node_id(kb, "甲") in {n["id"] for n in g["nodes"]}
    edges = _rows(store, "SELECT * FROM graph_edges WHERE scope = ?", (kb,))
    assert len(edges) == 1
    assert edges[0]["evidence"] == "甲认识乙"
    assert edges[0]["doc_id"] == "doc1"


def test_upsert_is_idempotent_and_accumulates(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲", aliases=["A1"]), _ent("乙")], [])
    store.upsert_batch(kb, "doc1", [_ent("甲", aliases=["A2"], chunk="c2")], [])
    nodes = {n["normalized"]: n for n in store.list_entities(kb)}
    assert nodes["甲"]["aliases"] == ["A1", "A2"]
    with store._db.connect() as conn:
        row = conn.execute(
            "SELECT source_docs, source_chunks FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '甲'", (kb,)).fetchone()
    assert row["source_docs"] == ["doc1"]
    assert sorted(row["source_chunks"]) == ["c1", "c2"]
    refs = _rows(store, "SELECT chunk_id FROM graph_entity_refs "
                        "WHERE scope = ? AND normalized_name = '甲'", (kb,))
    assert {r["chunk_id"] for r in refs} == {"c1", "c2"}


def test_reupsert_preserves_created_at_and_first_name(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲")], [])
    with store._db.connect() as conn:
        first = conn.execute(
            "SELECT created_at, name, id FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '甲'", (kb,)).fetchone()
    store.upsert_batch(kb, "d2", [_ent("甲")], [])
    with store._db.connect() as conn:
        second = conn.execute(
            "SELECT created_at, name, id FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '甲'", (kb,)).fetchone()
    assert second["created_at"] == first["created_at"]
    assert second["name"] == first["name"] == "甲"
    assert second["id"] == first["id"]


def test_type_overwrites_and_description_fills_only_empty(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲", typ="person", desc="原始描述")], [])
    store.upsert_batch(kb, "d1", [_ent("甲", typ="organization", desc="新描述")], [])
    node = {n["normalized"]: n for n in store.list_entities(kb)}["甲"]
    assert node["type"] == "organization"
    with store._db.connect() as conn:
        row = conn.execute("SELECT description FROM graph_nodes "
                           "WHERE scope = ? AND normalized_name = '甲'",
                           (kb,)).fetchone()
    assert row["description"] == "原始描述"


def test_edge_to_missing_endpoint_is_skipped_but_batch_commits(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲")],
                       [_rel("甲", "不存在"), _rel("甲", "甲")])
    edges = _rows(store, "SELECT * FROM graph_edges WHERE scope = ?", (kb,))
    assert {(e["source_key"], e["target_key"]) for e in edges} == {("甲", "甲")}


def test_parallel_chunk_edges_kept_and_doc_scoped_key(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲"), _ent("乙")],
                       [_rel("甲", "乙", chunk="c1", evidence="e1"),
                        _rel("甲", "乙", chunk="c2", evidence="e2")])
    # 同 (src,tgt,type) 不同文档、空 chunk:旧 Neo4j 键会互相覆盖,
    # PG 键补 doc_id 后必须各自保留(有意修复)
    store.upsert_batch(kb, "doc2", [_ent("甲"), _ent("乙")],
                       [_rel("甲", "乙", chunk="", evidence="doc2-e")])
    edges = _rows(store, "SELECT * FROM graph_edges WHERE scope = ?", (kb,))
    assert len(edges) == 3
    assert store.counts(kb) == {"entities": 2, "relations": 1}
    assert store.document_counts(kb, "doc1") == {"entities": 2, "relations": 1}
    assert store.document_counts(kb, "doc2") == {"entities": 2, "relations": 1}


def test_evidence_truncated_at_500_on_write(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲"), _ent("乙")],
                       [_rel("甲", "乙", evidence="x" * 600)])
    edges = _rows(store, "SELECT evidence FROM graph_edges WHERE scope = ?", (kb,))
    assert len(edges[0]["evidence"]) == 500


def test_scopes_are_isolated(store):
    kb1, kb2 = _scope(), _scope()
    store.upsert_batch(kb1, "d1", [_ent("甲")], [])
    store.upsert_batch(kb2, "d1", [_ent("乙")], [])
    assert store.list_entity_names(kb1) == {"甲"}
    assert store.list_entity_names(kb2) == {"乙"}
    assert store.delete_scope(kb1) == {"entities": 1}
    assert store.list_entity_names(kb1) == set()
    assert store.list_entity_names(kb2) == {"乙"}


# ── 词表 / 消歧查询 ─────────────────────────────────────────


def test_list_entity_aliases_and_connected_pairs(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲", aliases=["A1"], typ="person")],
                       [])
    store.upsert_batch(kb, "d1", [_ent("乙")],
                       [_rel("甲", "乙")])
    aliases = store.list_entity_aliases(kb)
    assert aliases["甲"]["type"] == "person"
    assert aliases["甲"]["aliases"] == ["A1"]
    pairs = store.list_connected_pairs(kb)
    assert pairs == {("甲", "乙"), ("乙", "甲")}


def test_prune_aliases_removes_claims_and_ref_rows(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲", aliases=["争议", "保留"])],
                       [_rel("甲", "乙")])
    assert store.prune_aliases(kb, ["争议"]) == 1
    node = {n["normalized"]: n for n in store.list_entities(kb)}["甲"]
    assert node["aliases"] == ["保留"]
    refs = _rows(store, "SELECT aliases FROM graph_entity_refs WHERE scope = ?",
                 (kb,))
    assert all("争议" not in (r["aliases"] or []) for r in refs)
    assert store.prune_aliases(kb, ["争议"]) == 0


def test_chunk_output_matches_entities_and_relations(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲", chunk="c1"), _ent("乙", chunk="c2")],
                       [_rel("甲", "乙", chunk="c1", evidence="e" * 300)])
    out = store.chunk_output(kb, "c1")
    assert [e["normalized"] for e in out["entities"]] == ["甲"]
    rel = out["relations"][0]
    assert (rel["source"], rel["target"]) == ("甲", "乙")
    assert len(rel["evidence"]) == 200   # 块明细展示截 200(旧口径保留)


# ── 实体合并 ────────────────────────────────────────────────


def _seed_merge_fixture(store, kb):
    store.upsert_batch(kb, "doc1",
                       [_ent("孙悟空", chunk="c1"), _ent("齐天大圣", chunk="c2",
                                                         aliases=["美猴王"]),
                        _ent("唐僧", chunk="c1"), _ent("天庭", chunk="c3")],
                       [_rel("孙悟空", "唐僧", chunk="c1", evidence="护送"),
                        _rel("天庭", "齐天大圣", chunk="c3", evidence="招安"),
                        _rel("孙悟空", "齐天大圣", chunk="c1", evidence="自称")])


def test_merge_entities_migrates_edges_aliases_and_refs(store):
    kb = _scope()
    _seed_merge_fixture(store, kb)
    out = store.merge_entities(kb, "doc1",
                               [{"canonical": "孙悟空", "fragment": "齐天大圣"}])
    assert out == {"merged": 1}
    names = store.list_entity_names(kb)
    assert "齐天大圣" not in names and "孙悟空" in names
    node = {n["normalized"]: n for n in store.list_entities(kb)}["孙悟空"]
    assert "齐天大圣" in node["aliases"] and "美猴王" in node["aliases"]
    edges = _rows(store, "SELECT * FROM graph_edges WHERE scope = ?", (kb,))
    by_pair = {(e["source_key"], e["target_key"]): e for e in edges}
    # 出边迁移:孙悟空→唐僧(原有) + 孙悟空→唐僧? 原 齐天大圣→唐僧 无;
    # 自环(孙悟空-齐天大圣)已删;入边迁移:天庭→孙悟空(原 →齐天大圣)
    assert ("天庭", "孙悟空") in by_pair
    assert ("孙悟空", "齐天大圣") not in by_pair
    assert ("齐天大圣", "唐僧") not in by_pair
    migrated = by_pair[("天庭", "孙悟空")]
    assert migrated["evidence"] == "招安"
    assert migrated["chunk_id"] == "c3"
    with store._db.connect() as conn:
        docs = conn.execute(
            "SELECT source_docs FROM graph_nodes WHERE scope = ? "
            "AND normalized_name = '孙悟空'", (kb,)).fetchone()
        frag_refs = conn.execute(
            "SELECT count(*) FROM graph_entity_refs WHERE scope = ? "
            "AND normalized_name = '齐天大圣'", (kb,)).fetchone()
    assert docs["source_docs"] == ["doc1"]
    assert frag_refs["count"] == 0
    # 幂等:已合并的碎片不存在 → merged 0
    assert store.merge_entities(kb, "doc1",
                                [{"canonical": "孙悟空", "fragment": "齐天大圣"}]) == {"merged": 0}


def test_merge_entities_missing_side_counts_zero(store):
    kb = _scope()
    store.upsert_batch(kb, "d1", [_ent("甲")], [])
    assert store.merge_entities(kb, "d1", [
        {"canonical": "甲", "fragment": "不存在"},
        {"canonical": "也不存在", "fragment": "甲"},
        {"canonical": "", "fragment": "甲"},
        {"canonical": "甲", "fragment": "甲"},
    ]) == {"merged": 0}


# ── 删除 ────────────────────────────────────────────────────


def test_delete_document_shared_entity_survives_orphan_removed(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1",
                       [_ent("共享", chunk="c1"), _ent("独占", chunk="c1")],
                       [_rel("共享", "独占", chunk="c1", evidence="d1")])
    store.upsert_batch(kb, "doc2", [_ent("共享", chunk="k1"), _ent("乙", chunk="k1")],
                       [_rel("共享", "乙", chunk="k1", evidence="d2")])
    out = store.delete_document(kb, "doc1")
    assert out["edges"] == 1 and out["orphanEntities"] == 1
    names = store.list_entity_names(kb)
    assert names == {"共享", "乙"}          # 独占 随 doc1 孤儿删除
    edges = _rows(store, "SELECT doc_id FROM graph_edges WHERE scope = ?", (kb,))
    assert {e["doc_id"] for e in edges} == {"doc2"}
    refs = _rows(store, "SELECT doc_id FROM graph_entity_refs WHERE scope = ?",
                 (kb,))
    assert {r["doc_id"] for r in refs} == {"doc2"}
    with store._db.connect() as conn:
        row = conn.execute(
            "SELECT source_docs, source_chunks FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '共享'", (kb,)).fetchone()
    assert row["source_docs"] == ["doc2"]
    assert row["source_chunks"] == ["k1"]


def test_delete_document_cleans_orphan_remaining_edges(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲", chunk="c1")], [])
    store.upsert_batch(kb, "doc2", [_ent("甲", chunk="k1"), _ent("乙", chunk="k1")],
                       [_rel("甲", "乙", chunk="k1")])
    out = store.delete_document(kb, "doc2")
    # 甲 仍持有 doc1 来源(仅实体) → 非孤儿;乙随 doc2 孤儿删除,
    # 其关联边(甲→乙)一并清理
    assert out["orphanEntities"] == 1
    assert store.counts(kb) == {"entities": 1, "relations": 0}
    assert _rows(store, "SELECT * FROM graph_edges WHERE scope = ?", (kb,)) == []


# ── 统计 / 浏览 / 检索 ──────────────────────────────────────


def test_counts_and_document_counts_dedupe_triples(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲"), _ent("乙"), _ent("丙")],
                       [_rel("甲", "乙", chunk="c1"),
                        _rel("甲", "乙", chunk="c2"),
                        _rel("乙", "丙", chunk="c1")])
    assert store.counts(kb) == {"entities": 3, "relations": 2}


def test_get_graph_aggregates_and_filters(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1",
                       [_ent("甲", typ="person", desc="人物甲"),
                        _ent("乙", typ="product"), _ent("丙", typ="person")],
                       [_rel("甲", "乙", chunk="c1", evidence="e1", desc="d1"),
                        _rel("甲", "乙", chunk="c2", evidence="e2"),
                        _rel("甲", "丙", chunk="c1")])
    g = store.get_graph(kb)
    assert len(g["nodes"]) == 3
    edge = next(e for e in g["edges"] if e["target"] == entity_node_id(kb, "乙"))
    assert edge["supportChunks"] == 2
    assert edge["evidences"] == ["e1", "e2"]
    assert edge["evidence"] in ("e1", "e2")
    assert edge["docId"] == "doc1"
    g2 = store.get_graph(kb, q="人物")
    assert {n["name"] for n in g2["nodes"]} == {"甲"}
    g3 = store.get_graph(kb, node_types=["product"])
    assert {n["name"] for n in g3["nodes"]} == {"乙"}


def test_get_graph_limits_edges_after_aggregation(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent(f"乙{i}") for i in range(5)] + [_ent("甲")],
                       [_rel("甲", f"乙{i}", chunk="c") for i in range(5)])
    g = store.get_graph(kb, limit_nodes=80, limit_edges=3)
    assert len(g["edges"]) == 3


def test_expand_node_is_undirected_and_aggregated(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1",
                       [_ent("甲"), _ent("乙"), _ent("丙")],
                       [_rel("乙", "甲", chunk="c1", evidence="入边"),
                        _rel("甲", "丙", chunk="c1", evidence="出边"),
                        _rel("甲", "丙", chunk="c2")])
    out = store.expand_node(kb, entity_node_id(kb, "甲"))
    ids = {n["id"] for n in out["nodes"]}
    assert entity_node_id(kb, "甲") in ids
    assert entity_node_id(kb, "乙") in ids and entity_node_id(kb, "丙") in ids
    assert len(out["edges"]) == 2
    e = next(x for x in out["edges"]
             if x["source"] == entity_node_id(kb, "乙"))
    assert e["evidence"] == "入边"
    e2 = next(x for x in out["edges"] if x["target"] == entity_node_id(kb, "丙"))
    assert e2["supportChunks"] == 2


def test_find_entities_exact_prefix_contains_layers(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1",
                       [_ent("JJF1092-2002"), _ent("JJF1094-2002"),
                        _ent("压力表", aliases=["压力计"]),
                        _ent("弹簧管式一般压力表")],
                       [])
    exact = store.find_entities(kb, ["JJF1092-2002"])
    assert [n["normalized"] for n in exact] == ["jjf1092-2002"]
    by_alias = store.find_entities(kb, ["压力计"])
    assert [n["normalized"] for n in by_alias] == ["压力表"]
    prefix = store.find_entities(kb, ["jjf109"])
    assert {n["normalized"] for n in prefix} == {"jjf1092-2002", "jjf1094-2002"}
    # 精确层命中即不再降层:"压力表"精确命中 → 不返回包含匹配结果
    exact_priority = store.find_entities(kb, ["压力表"])
    assert {n["normalized"] for n in exact_priority} == {"压力表"}
    # 包含层仅在精确/前缀皆空时触发:"管式" 只被包含层命中
    contains = store.find_entities(kb, ["管式"])
    assert {n["normalized"] for n in contains} == {"弹簧管式一般压力表"}


def test_subgraph_around_bfs_limits_and_no_dangling_edges(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1",
                       [_ent(n) for n in ["甲", "乙", "丙", "丁", "戊"]],
                       [_rel("甲", "乙"), _rel("乙", "丙"),
                        _rel("丙", "丁"), _rel("丁", "戊")])
    out = store.subgraph_around(kb, ["甲"], hops=1)
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {entity_node_id(kb, n) for n in ("甲", "乙")}
    assert len(out["edges"]) == 1
    out2 = store.subgraph_around(kb, ["甲"], hops=3)
    ids2 = {n["id"] for n in out2["nodes"]}
    assert entity_node_id(kb, "丙") in ids2
    for e in out2["edges"]:
        assert e["source"] in ids2 and e["target"] in ids2
    # 限量:节点上限收紧后不得出现悬空边
    out3 = store.subgraph_around(kb, ["甲"], hops=3, max_nodes=3, max_edges=10)
    ids3 = {n["id"] for n in out3["nodes"]}
    for e in out3["edges"]:
        assert e["source"] in ids3 and e["target"] in ids3


def test_subgraph_around_cycle_and_missing_seed(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲"), _ent("乙")],
                       [_rel("甲", "乙"), _rel("乙", "甲")])
    out = store.subgraph_around(kb, ["甲", "不存在的种子"], hops=2)
    assert {n["name"] for n in out["nodes"]} == {"甲", "乙"}
    assert len(out["edges"]) >= 1


def test_subgraph_aggregates_parallel_edges(store):
    kb = _scope()
    store.upsert_batch(kb, "doc1", [_ent("甲"), _ent("乙")],
                       [_rel("甲", "乙", chunk="c1", evidence="e1"),
                        _rel("甲", "乙", chunk="c2", evidence="e2")])
    out = store.subgraph_around(kb, ["甲"], hops=2, max_edges=1)
    assert len(out["edges"]) == 1


# ── 工厂 ────────────────────────────────────────────────────


def test_factory_returns_cached_instance_per_schema(store):
    from sdk import graph_store as sdk_graph
    a = sdk_graph.get_graph_store("knowledge_graph")
    b = sdk_graph.get_graph_store("knowledge_graph")
    assert a is b
    assert isinstance(a, sdk_graph.PgGraphStore)
