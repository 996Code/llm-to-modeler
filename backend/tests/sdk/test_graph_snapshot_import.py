"""PG 图快照导入/对账工具契约(Neo4j 存量迁移的唯一入库工具)。"""
import hashlib
import json
import uuid

import pytest

from sdk.graph_store import PgGraphStore, entity_node_id


@pytest.fixture()
def store():
    from sdk.graph_store import reset_graph_store_cache
    reset_graph_store_cache()
    return PgGraphStore("knowledge_graph")


def _node(kb, name, **over):
    base = {"kb_id": kb, "id": f"{kb}:{name}", "name": name,
            "normalized_name": name, "type": "person",
            "description": "描述", "aliases": ["别名甲", "别名乙"],
            "source_docs": ["doc1", "doc2"], "source_chunks": ["c1", "c2"],
            "type_status": "approved", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00", "custom_prop": "保留我"}
    base.update(over)
    return base


def _edge(kb, src, tgt, **over):
    p = {"kb_id": kb, "id": f"{kb}:{src}>相关>{tgt}:c1",
         "doc_id": "doc1", "chunk_id": "c1", "source_key": src,
         "target_key": tgt, "type": "相关", "description": "d",
         "evidence": "e" * 600, "created_at": "2026-01-01T00:00:00+00:00",
         "updated_at": "2026-01-01T00:00:00+00:00"}
    p.update(over)
    return {"properties": p,
            "start": {"kb_id": kb, "normalized_name": src},
            "end": {"kb_id": kb, "normalized_name": tgt}}


def _write_snapshot(tmp_path, nodes, edges, mutate_manifest=None,
                    corrupt_sha=False):
    d = tmp_path / "snap"
    d.mkdir(exist_ok=True)
    nodes_file = d / "nodes.jsonl"
    edges_file = d / "edges.jsonl"
    nodes_file.write_text(
        "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in nodes),
        encoding="utf-8")
    edges_file.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in edges),
        encoding="utf-8")

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    manifest = {
        "format": "graph-snapshot-v1", "exported_at": "2026-11-02T00:00:00+00:00",
        "source": "neo4j", "labels": ["Entity"], "rel_types": ["RELATES"],
        "counts": {"nodes": len(nodes), "raw_edges": len(edges),
                   "anomalies": 0},
        "anomaly_list": [],
        "files": {
            "nodes.jsonl": {"sha256": sha(nodes_file),
                            "bytes": nodes_file.stat().st_size},
            "edges.jsonl": {"sha256": sha(edges_file),
                            "bytes": edges_file.stat().st_size},
        },
        "scope_nodes": {}, "scope_raw_edges": {},
    }
    if mutate_manifest:
        mutate_manifest(manifest)
    if corrupt_sha:
        manifest["files"]["nodes.jsonl"]["sha256"] = "0" * 64
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def _tool():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" \
        / "import_graph_snapshot.py"
    spec = importlib.util.spec_from_file_location("import_graph_snapshot", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KB = "11111111-1111-4111-8111-111111111111"


def test_check_empty_target_and_apply_imports_verbatim(store, tmp_path):
    tool = _tool()
    kb = KB
    snap = _write_snapshot(
        tmp_path,
        [_node(kb, "甲"), _node(kb, "乙"),
         _node(kb, "丙", source_docs=["doc9"], source_chunks=["desc-mention"])],
        [_edge(kb, "甲", "乙"),
         _edge(kb, "甲", "丙", chunk_id="desc-mention", doc_id="doc9"),
         _edge(kb, "甲", "乙", doc_id="doc2", chunk_id="",
               evidence="第二文档空块边")])
    report = tool.run(str(snap), schema="knowledge_graph", apply=False)
    assert report["ok"] is True
    assert report["scopes"][kb]["nodes"] == 3
    assert report["scopes"][kb]["raw_edges"] == 3

    applied = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert applied["applied_scopes"] == [kb]

    with store._db.connect() as conn:
        rows = {r["normalized_name"]: dict(r) for r in conn.execute(
            "SELECT * FROM graph_nodes WHERE scope = ?", (kb,)).fetchall()}
        edge_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM graph_edges WHERE scope = ?", (kb,)).fetchall()]
        refs = conn.execute(
            "SELECT count(*) AS c FROM graph_entity_refs WHERE scope = ?",
            (kb,)).fetchone()["c"]
    n = rows["甲"]
    assert n["id"] == entity_node_id(kb, "甲")
    assert n["aliases"] == ["别名甲", "别名乙"]
    assert n["source_docs"] == ["doc1", "doc2"]
    assert n["source_chunks"] == ["c1", "c2"]
    assert n["created_at"] == "2026-01-01T00:00:00+00:00"
    # 未知原始属性全量保留在 extra.original_properties(保真,不丢弃)
    assert n["extra"]["original_properties"]["custom_prop"] == "保留我"
    # 迁移不套用写入截断:600 字证据原样入册
    long_edge = next(e for e in edge_rows if len(e["evidence"]) == 600)
    assert long_edge["source_key"] == "甲"
    # 空块 + 不同文档的同型边共存(新键含 doc_id)
    assert len([e for e in edge_rows
                if e["source_key"] == "甲" and e["target_key"] == "乙"]) == 2
    assert {e["chunk_id"] for e in edge_rows} >= {"c1", "", "desc-mention"}
    # 存量导入不伪造 refs(docs×chunks 对应关系不可知,保持空)
    assert refs == 0
    # 读回可用
    assert store.counts(kb) == {"entities": 3, "relations": 2}


def test_reapply_is_noop(store, tmp_path):
    tool = _tool()
    snap = _write_snapshot(tmp_path, [_node(KB, "甲")], [])
    tool.run(str(snap), schema="knowledge_graph", apply=True)
    with store._db.connect() as conn:
        before = dict(conn.execute(
            "SELECT updated_at, created_at FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '甲'", (KB,)).fetchone())
    again = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert again["applied_scopes"] == []       # 已一致 → 不再写入
    with store._db.connect() as conn:
        after = dict(conn.execute(
            "SELECT updated_at, created_at FROM graph_nodes "
            "WHERE scope = ? AND normalized_name = '甲'", (KB,)).fetchone())
    assert after == before


def test_conflicting_existing_content_refused(store, tmp_path):
    tool = _tool()
    store.upsert_batch(KB, "other-doc", [
        {"name": "甲", "normalized_name": "甲", "type": "person",
         "description": "", "aliases": [], "type_status": "approved",
         "chunk_id": "x"}], [])
    snap = _write_snapshot(tmp_path, [_node(KB, "甲")], [])
    report = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert report["ok"] is False
    assert any(c["scope"] == KB for c in report["conflicts"])
    # 拒绝即零写入:冲突 scope 的既有行原样
    names = store.list_entity_names(KB)
    assert names == {"甲"}
    with store._db.connect() as conn:
        kept = conn.execute(
            "SELECT source_docs FROM graph_nodes WHERE scope = ? "
            "AND normalized_name = '甲'", (KB,)).fetchone()
    assert kept["source_docs"] == ["other-doc"]


def test_validation_failures_block_write(store, tmp_path):
    tool = _tool()
    bad_kb = "not-a-uuid"
    snap = _write_snapshot(
        tmp_path, [_node(bad_kb, "甲")], [])
    report = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert report["ok"] is False and report["errors"]
    with store._db.connect() as conn:
        c = conn.execute("SELECT count(*) AS c FROM graph_nodes",
                         ()).fetchone()["c"]
    assert c == 0

    snap2 = _write_snapshot(
        tmp_path,
        [_node(KB, "甲")],
        [_edge(KB, "甲", "缺端点")])
    report2 = tool.run(str(snap2), schema="knowledge_graph", apply=True)
    assert report2["ok"] is False
    assert store.list_entity_names(KB) == set()   # 校验失败零写入

    snap3 = _write_snapshot(
        tmp_path, [_node(KB, "甲"), _node(KB, "乙")],
        [_edge(KB, "甲", "乙", chunk_id="c1"),
         _edge(KB, "甲", "乙", chunk_id="c1", evidence="重复键")],
        corrupt_sha=False)
    # 同 (scope,src,tgt,type,doc,chunk) 两条 = 快照级损坏 → 拒绝
    report3 = tool.run(str(snap3), schema="knowledge_graph", apply=True)
    assert report3["ok"] is False
    assert store.list_entity_names(KB) == set()


def test_sha_mismatch_refused(store, tmp_path):
    tool = _tool()
    snap = _write_snapshot(tmp_path, [_node(KB, "甲")], [], corrupt_sha=True)
    report = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert report["ok"] is False
    assert store.list_entity_names(KB) == set()


def test_cross_scope_edge_reported(store, tmp_path):
    tool = _tool()
    other = str(uuid.uuid4())
    edge = _edge(KB, "甲", "乙")
    edge["end"]["kb_id"] = other          # 端点落在另一 scope
    snap = _write_snapshot(
        tmp_path, [_node(KB, "甲"), _node(other, "乙")], [edge])
    report = tool.run(str(snap), schema="knowledge_graph", apply=True)
    assert report["ok"] is False
    assert any("cross-scope" in e for e in report["errors"])
