#!/usr/bin/env python3
"""Milvus → PostgreSQL(pgvector) 向量数据迁移脚本(幂等,可重复执行)。

【用途】
向量库默认后端切 pg(N1:VECTOR_BACKEND=pg)后,把存量 Milvus
collection 里的向量数据搬进 PG。表名规则与 Milvus collection 名同款
({prefix}_{scope 转下划线}_v1),迁移后一一对应,上层(kg/chatbi)零改动。

    # 1. 停 app(防在线写)  2. 迁移  3. 起服务(VECTOR_BACKEND=pg)
    cd backend
    MILVUS_URI=http://localhost:19530 \
    DATABASE_URL=postgresql://llm:密码@localhost:15432/llm_modeler \
    python3 scripts/migrate_milvus_to_pg.py

【幂等性】
按 (scope, doc_id) 粒度比对:PG 里已存在的 chunk_id 跳过,只补缺口
——中断重跑安全,且不会用 Milvus 旧数据覆盖 PG 里已重导入的新数据。

【前置条件】
  - 目标 PG 装有 pgvector 扩展(脚本建表时自动 CREATE EXTENSION,
    无权限时报错并给 DBA 指引);
  - Milvus 不可达/无 collection 时直接跳过(exit 0)——纯 pg 新部署
    没有存量可迁,不是错误。

【范围】
只迁 collection 名匹配 {prefix}_{UUID}_v1 的 collection(默认前缀
kg/bi,即 knowledge_graph 与 chatbi 两个 pack;--prefixes 可增补)。
不匹配的 collection 打印跳过说明,不动。

【验收】
每个 collection 迁移后打印 Milvus 行数 vs PG 行数;PG ≥ Milvus 即成功
(PG 里可能有 Milvus 之后新写入的数据)。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path

# 让 backend/src 可被 import(脚本在宿主跑,无 PYTHONPATH)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdk.pg_vector_store import PgVectorStore  # noqa: E402

# collection 名 → (prefix, scope):{prefix}_{scope 的 - 转 _}_v1
_COLL_RE = re.compile(r"^([a-z0-9_]+?)_([0-9a-f_]+)_v1$")

_BATCH = 500  # Milvus 迭代器批量 → PG upsert 批量


def _milvus_client(uri: str, user: str, password: str):
    from pymilvus import MilvusClient
    return MilvusClient(uri=uri, user=user or None,
                        password=password or None, timeout=5)


def _iter_rows(client, collection: str):
    """全量迭代一个 collection 的行(含向量;query_iterator 游标翻页)。"""
    it = client.query_iterator(
        collection_name=collection, batch_size=_BATCH, filter="",
        output_fields=["chunk_id", "doc_id", "seq", "text", "vector"])
    try:
        while True:
            batch = it.next()
            if not batch:
                return
            for row in batch:
                yield row
    finally:
        try:
            it.close()
        except Exception:
            pass


def _collection_dim(client, collection: str) -> int:
    """从 collection schema 取向量维度(FLOAT_VECTOR 字段)。"""
    from pymilvus import DataType
    desc = client.describe_collection(collection)
    for f in (desc.get("fields") or []):
        if f.get("type") == DataType.FLOAT_VECTOR:
            return int(f.get("params", {}).get("dim"))
    raise RuntimeError(f"collection {collection} 缺 FLOAT_VECTOR 字段")


def migrate(milvus_uri: str, user: str, password: str, dsn: str,
            prefixes: list) -> int:
    try:
        client = _milvus_client(milvus_uri, user, password)
        collections = client.list_collections()
    except Exception as e:
        print(f"• Milvus 不可达({e})——视为无存量数据,跳过迁移。"
              "如确有数据请检查 MILVUS_URI。")
        return 0
    if not collections:
        print("• Milvus 无 collection——无存量数据,跳过。")
        return 0

    matched = 0
    for collection in sorted(collections):
        m = _COLL_RE.match(collection)
        if not m or m.group(1) not in prefixes:
            print(f"• 跳过 {collection}(不匹配前缀 {prefixes} 或命名规则)")
            continue
        prefix, scope = m.group(1), m.group(2).replace("_", "-")
        try:
            uuid.UUID(scope)
        except ValueError:
            print(f"• 跳过 {collection}(scope {scope!r} 非 UUID,非本迁移契约)")
            continue
        matched += 1
        dim = _collection_dim(client, collection)
        pg = PgVectorStore(dsn=dsn, collection_prefix=prefix)
        try:
            pg.ensure_collection(scope, dim)
            src_count = int(client.get_collection_stats(collection)
                            .get("row_count") or 0)
            # 按 doc 分组补缺口(幂等:已存在的 chunk_id 不覆盖)
            by_doc: dict = {}
            for row in _iter_rows(client, collection):
                by_doc.setdefault(row.get("doc_id") or "", []).append(row)
            copied = 0
            for doc_id, rows in by_doc.items():
                existing = pg.existing_chunk_ids(scope, doc_id)
                missing = [r for r in rows
                           if r.get("chunk_id") not in existing]
                if missing:
                    copied += pg.upsert_chunks(scope, [{
                        "chunk_id": r["chunk_id"],
                        "doc_id": r.get("doc_id") or "",
                        "seq": r.get("seq") or 0,
                        "text": r.get("text") or "",
                        "vector": r.get("vector") or [],
                    } for r in missing])
            pg_count = pg.count(scope)
            status = "OK" if pg_count >= src_count else "行数不足,请重跑"
            print(f"• {collection} → PG 表(前缀 {prefix},scope {scope}): "
                  f"milvus={src_count} pg={pg_count} 本次补写={copied} [{status}]")
        finally:
            pg.close()
    print(f"\n完成:{matched} 个 collection 处理,"
          f"{len(collections) - matched} 个跳过。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Milvus → PostgreSQL(pgvector) 向量数据迁移(幂等)")
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI", ""),
                        help="Milvus 地址(默认 env MILVUS_URI)")
    parser.add_argument("--milvus-user", default=os.getenv("MILVUS_USER", ""))
    parser.add_argument("--milvus-password",
                        default=os.getenv("MILVUS_PASSWORD", ""))
    parser.add_argument("--url", default=os.getenv("DATABASE_URL", ""),
                        help="目标 PG 连接串(默认 env DATABASE_URL)")
    parser.add_argument("--prefixes", default="kg,bi",
                        help="要迁移的 collection 前缀,逗号分隔(默认 kg,bi)")
    args = parser.parse_args()

    if not args.milvus_uri:
        print("✗ 缺少 Milvus 地址:请用 --milvus-uri 或配置 env MILVUS_URI")
        return 2
    if not args.url:
        print("✗ 缺少目标库:请用 --url 或配置 env DATABASE_URL")
        return 2
    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    return migrate(args.milvus_uri, args.milvus_user, args.milvus_password,
                   args.url, prefixes)


if __name__ == "__main__":
    sys.exit(main())
