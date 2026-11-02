#!/usr/bin/env python3
"""图存量快照导入/对账工具(Neo4j → PostgreSQL 一次性迁移的入库侧)。

快照格式(graph-snapshot-v1,由切换前的一次性导出器生成,导出器不入库):
  nodes.jsonl  每行一个节点的完整原始属性(含未知属性)
  edges.jsonl  每行 {"properties": 关系完整属性, "start"/"end": 端点定位}
  manifest.json 计数、SHA-256、异常清单

行为约定:
  - 全属性保真导入:原始 created_at/updated_at/id/别名顺序/来源数组/evidence
    长度原样入册(不套用写入期截断);未知属性存入 extra.original_properties。
  - 存量不伪造 refs:旧库只有独立的 docs/chunks 数组,doc↔chunk 对应关系
    不可知——graph_entity_refs 保持为空,后续新写入才产生精确贡献记录。
  - 默认 --check 只校验;--apply 按库(schema)整事务导入。
  - 幂等:目标内容与快照一致的 scope 重跑为 no-op;同键不同内容 → 整 scope
    拒绝并列冲突清单,不做部分写入。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sdk.graph_store import _GRAPH_DDL, entity_node_id  # noqa: E402
from sdk.relational_store import PackRelationalDB  # noqa: E402
from sdk.scope_registry import is_scope_id_safe  # noqa: E402

_NODE_REQUIRED = ("kb_id", "normalized_name")


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if line:
                yield lineno, json.loads(line)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def load_snapshot(snapshot_dir: str):
    """读快照并做完整性/一致性校验,返回 (nodes, edges, errors)。"""
    d = Path(snapshot_dir)
    errors: list[str] = []
    manifest_path = d / "manifest.json"
    if not manifest_path.exists():
        return None, None, [f"缺少 manifest.json: {manifest_path}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "graph-snapshot-v1":
        errors.append(f"不支持的快照格式: {manifest.get('format')!r}")
    for name, meta in (manifest.get("files") or {}).items():
        p = d / name
        if not p.exists():
            errors.append(f"快照缺文件: {name}")
            continue
        if _sha256(p) != meta.get("sha256"):
            errors.append(f"快照文件校验不符(sha256): {name}")

    nodes, node_keys = [], set()
    for lineno, raw in _read_jsonl(d / "nodes.jsonl"):
        tag = f"nodes.jsonl:{lineno}"
        missing = [k for k in _NODE_REQUIRED if not raw.get(k)]
        if missing:
            errors.append(f"{tag} 节点缺字段 {missing}")
            continue
        if not is_scope_id_safe(raw["kb_id"]):
            errors.append(f"{tag} 非法 kb_id: {raw['kb_id']!r}")
            continue
        key = (raw["kb_id"], raw["normalized_name"])
        if key in node_keys:
            errors.append(f"{tag} 重复节点自然键 {key}")
            continue
        node_keys.add(key)
        nodes.append(raw)

    edges, edge_keys = [], set()
    for lineno, raw in _read_jsonl(d / "edges.jsonl"):
        tag = f"edges.jsonl:{lineno}"
        p = raw.get("properties") or {}
        start, end = raw.get("start") or {}, raw.get("end") or {}
        kb = p.get("kb_id") or start.get("kb_id") or end.get("kb_id")
        s_, t_ = start.get("normalized_name"), end.get("normalized_name")
        missing = [k for k in (kb, s_, t_) if not k]
        if missing:
            errors.append(f"{tag} 边缺端点/库字段")
            continue
        if start.get("kb_id") != end.get("kb_id"):
            errors.append(f"{tag} cross-scope 边: {start.get('kb_id')} → {end.get('kb_id')}")
            continue
        if (kb, s_) not in node_keys or (kb, t_) not in node_keys:
            errors.append(f"{tag} 边端点不在节点集: {s_!r}/{t_!r}")
            continue
        key = (kb, s_, t_, p.get("type") or "", p.get("doc_id") or "",
               p.get("chunk_id") or "")
        if key in edge_keys:
            errors.append(f"{tag} 重复边自然键(含 doc): {key}")
            continue
        edge_keys.add(key)
        edges.append({"kb": kb, "src": s_, "tgt": t_, "p": p})
    return nodes, edges, errors


def _node_row(n: dict, exported_at: str):
    known = {"kb_id", "normalized_name", "id", "name", "type", "description",
             "aliases", "source_docs", "source_chunks", "type_status",
             "created_at", "updated_at"}
    original = {k: v for k, v in n.items() if k not in known}
    return {
        "scope": n["kb_id"],
        "normalized_name": n["normalized_name"],
        "id": n.get("id") or entity_node_id(n["kb_id"], n["normalized_name"]),
        "name": n.get("name") or n["normalized_name"],
        "type": n.get("type") or "",
        "description": n.get("description") or "",
        "aliases": list(n.get("aliases") or []),
        "source_docs": list(n.get("source_docs") or []),
        "source_chunks": list(n.get("source_chunks") or []),
        "type_status": n.get("type_status") or "",
        "created_at": n.get("created_at") or exported_at,
        "updated_at": n.get("updated_at") or exported_at,
        "extra": json.dumps({"legacy_import": True,
                             "original_properties": original},
                            ensure_ascii=False),
    }


def _edge_row(e: dict, exported_at: str):
    p = e["p"]
    known = {"kb_id", "id", "doc_id", "chunk_id", "source_key", "target_key",
             "type", "description", "evidence", "created_at", "updated_at"}
    original = {k: v for k, v in p.items() if k not in known}
    return {
        "scope": e["kb"],
        "source_key": p.get("source_key") or e["src"],
        "target_key": p.get("target_key") or e["tgt"],
        "type": p.get("type") or "",
        "doc_id": p.get("doc_id") or "",
        "chunk_id": p.get("chunk_id") or "",
        "id": p.get("id") or f"{e['kb']}:{e['src']}>{p.get('type') or ''}>{e['tgt']}:{p.get('chunk_id') or ''}",
        "description": p.get("description") or "",
        "evidence": p.get("evidence") or "",
        "created_at": p.get("created_at") or exported_at,
        "updated_at": p.get("updated_at") or exported_at,
        "extra": json.dumps({"legacy_import": True,
                             "original_properties": original},
                            ensure_ascii=False),
    }


def _existing_matches(conn, kb: str, want_nodes: list,
                      want_edges: list) -> bool:
    """目标 scope 现存内容是否与快照期望逐行一致(幂等 no-op 判定)。"""
    node_cols = ("normalized_name, id, name, type, description, aliases, "
                 "source_docs, source_chunks, type_status, created_at, "
                 "updated_at, extra")
    rows = conn.execute(
        f"SELECT {node_cols} FROM graph_nodes WHERE scope = ? "
        "ORDER BY normalized_name", (kb,)).fetchall()
    if len(rows) != len(want_nodes):
        return False
    want_sorted = sorted(want_nodes, key=lambda r: r["normalized_name"])
    for row, want in zip(rows, want_sorted):
        if (row["normalized_name"] != want["normalized_name"]
                or row["id"] != want["id"] or row["name"] != want["name"]
                or row["type"] != want["type"]
                or row["description"] != want["description"]
                or list(row["aliases"] or []) != want["aliases"]
                or list(row["source_docs"] or []) != want["source_docs"]
                or list(row["source_chunks"] or []) != want["source_chunks"]
                or row["type_status"] != want["type_status"]
                or row["created_at"] != want["created_at"]
                or row["updated_at"] != want["updated_at"]
                or (dict(row["extra"] or {}) != json.loads(want["extra"]))):
            return False
    edge_cols = ("source_key, target_key, type, doc_id, chunk_id, id, "
                 "description, evidence, created_at, updated_at, extra")
    erows = conn.execute(
        f"SELECT {edge_cols} FROM graph_edges WHERE scope = ? "
        "ORDER BY source_key, target_key, type, doc_id, chunk_id",
        (kb,)).fetchall()
    if len(erows) != len(want_edges):
        return False
    ewant_sorted = sorted(want_edges, key=lambda r: (r["source_key"],
                                                     r["target_key"],
                                                     r["type"], r["doc_id"],
                                                     r["chunk_id"]))
    for row, want in zip(erows, ewant_sorted):
        if (row["source_key"] != want["source_key"]
                or row["target_key"] != want["target_key"]
                or row["type"] != want["type"] or row["doc_id"] != want["doc_id"]
                or row["chunk_id"] != want["chunk_id"] or row["id"] != want["id"]
                or row["description"] != want["description"]
                or row["evidence"] != want["evidence"]
                or row["created_at"] != want["created_at"]
                or row["updated_at"] != want["updated_at"]
                or (dict(row["extra"] or {}) != json.loads(want["extra"]))):
            return False
    return True


def run(snapshot_dir: str, schema: str = "knowledge_graph",
        apply: bool = False) -> dict:
    """校验并(可选)导入快照;返回报告 dict(ok/applied_scopes/conflicts)。"""
    nodes, edges, errors = load_snapshot(snapshot_dir)
    report = {"ok": False, "errors": errors or [], "conflicts": [],
              "applied_scopes": [], "scopes": {}}
    if nodes is None:
        return report

    by_scope: dict[str, dict] = {}
    for n in nodes:
        by_scope.setdefault(n["kb_id"], {"nodes": [], "edges": []})
    for e in edges:
        by_scope.setdefault(e["kb"], {"nodes": [], "edges": []})["edges"].append(e)
    for n in nodes:
        by_scope[n["kb_id"]]["nodes"].append(n)
    for kb, payload in by_scope.items():
        report["scopes"][kb] = {"nodes": len(payload["nodes"]),
                                "raw_edges": len(payload["edges"])}
    if errors:
        return report

    manifest = json.loads((Path(snapshot_dir) / "manifest.json")
                          .read_text(encoding="utf-8"))
    exported_at = manifest.get("exported_at") or ""
    db = PackRelationalDB(schema)
    db.migrate_locked(
        0x67726170 + (hashlib.sha256(f"graph:{schema}".encode()).digest()[0]
                      | (hashlib.sha256(f"graph:{schema}".encode()).digest()[1] << 8)),
        _run_ddl)

    for kb, payload in sorted(by_scope.items()):
        want_nodes = [_node_row(n, exported_at) for n in payload["nodes"]]
        want_edges = [_edge_row(e, exported_at) for e in payload["edges"]]
        with db.connect() as conn:
            existing_nodes = conn.execute(
                "SELECT count(*) AS c FROM graph_nodes WHERE scope = ?", (kb,)
            ).fetchone()["c"]
            existing_edges = conn.execute(
                "SELECT count(*) AS c FROM graph_edges WHERE scope = ?", (kb,)
            ).fetchone()["c"]
            existing_refs = conn.execute(
                "SELECT count(*) AS c FROM graph_entity_refs WHERE scope = ?",
                (kb,)).fetchone()["c"]
            if existing_nodes == 0 and existing_edges == 0 and existing_refs == 0:
                if apply:
                    for r in want_nodes:
                        conn.execute(
                            "INSERT INTO graph_nodes (scope, normalized_name, "
                            "id, name, type, description, aliases, "
                            "source_docs, source_chunks, type_status, "
                            "created_at, updated_at, extra) VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb)",
                            (r["scope"], r["normalized_name"], r["id"],
                             r["name"], r["type"], r["description"],
                             r["aliases"], r["source_docs"], r["source_chunks"],
                             r["type_status"], r["created_at"], r["updated_at"],
                             r["extra"]))
                    for r in want_edges:
                        conn.execute(
                            "INSERT INTO graph_edges (scope, source_key, "
                            "target_key, type, doc_id, chunk_id, id, "
                            "description, evidence, created_at, updated_at, "
                            "extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?::jsonb)",
                            (r["scope"], r["source_key"], r["target_key"],
                             r["type"], r["doc_id"], r["chunk_id"], r["id"],
                             r["description"], r["evidence"], r["created_at"],
                             r["updated_at"], r["extra"]))
                    report["applied_scopes"].append(kb)
                continue
            # 非空 scope:内容与快照逐行一致 → no-op;不一致 → 拒绝
            if _existing_matches(conn, kb, want_nodes, want_edges):
                continue
            report["conflicts"].append(
                {"scope": kb, "existing_nodes": existing_nodes,
                 "snapshot_nodes": len(want_nodes),
                 "existing_edges": existing_edges,
                 "snapshot_edges": len(want_edges)})
    if report["conflicts"]:
        report["errors"].append(
            f"{len(report['conflicts'])} 个 scope 已有不同内容,拒绝导入(零写入)")
        return report
    report["ok"] = True
    return report


def _run_ddl(conn) -> None:
    for stmt in _GRAPH_DDL:
        conn.execute(stmt)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="快照目录")
    parser.add_argument("--schema", default="knowledge_graph")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验(默认)")
    mode.add_argument("--apply", action="store_true", help="校验通过后导入")
    args = parser.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("缺少 DATABASE_URL", file=sys.stderr)
        return 2
    report = run(args.snapshot, schema=args.schema, apply=args.apply)
    print(json.dumps(
        {k: v for k, v in report.items() if k != "scopes"} |
        {"scope_count": len(report["scopes"])},
        ensure_ascii=False, indent=2))
    for kb, c in sorted(report["scopes"].items()):
        print(f"  {kb}  nodes={c['nodes']} raw_edges={c['raw_edges']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
