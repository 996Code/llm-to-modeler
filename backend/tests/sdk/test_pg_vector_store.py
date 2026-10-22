"""PgVectorStore 真实 PG(pgvector)往返测试。

跑法(与其他 PG 测试同款,conftest 已把 DATABASE_URL 指向测试库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/llm_modeler_test_run \
  PYTHONPATH=src python -m pytest tests/sdk/test_pg_vector_store.py -q

覆盖:建表(含 HNSW 索引/扩展幂等)/维度守卫/写入覆盖/余弦检索语义/
doc 过滤/缺口判定/删除/计数/表删除/scope 契约/工厂后端切换。
不依赖 Milvus;Fake 替身测试(tests/domains/)不经本文件。
"""
import os

import psycopg
import pytest

from sdk import vector_store as vs
from sdk.pg_vector_store import PgVectorStore
from sdk.scope_registry import new_scope_id

# conftest 已引导 TEST_DATABASE_URL 并设为 DATABASE_URL(必配,缺则收集期退出)
DSN = os.environ["DATABASE_URL"]

DIM = 3
V_A = [1.0, 0.0, 0.0]
V_A2 = [0.99, 0.02, 0.0]     # 与 V_A 近乎同向
V_B = [0.0, 1.0, 0.0]        # 与 V_A 正交
V_C = [0.0, 0.0, 1.0]


@pytest.fixture()
def store():
    s = PgVectorStore(dsn=DSN, collection_prefix="zz_test")
    yield s
    s.close()


def _table_exists(scope: str) -> bool:
    with psycopg.connect(DSN, autocommit=True) as conn:
        row = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS ok",
            (f"zz_test_{scope.replace('-', '_')}_v1",)).fetchone()
    return bool(row[0])


class TestLifecycle:
    """建表/索引/扩展/维度守卫/删表。"""

    def test_ensure_creates_table_and_hnsw_index(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        assert _table_exists(scope)
        with psycopg.connect(DSN, autocommit=True) as conn:
            idx = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
                (f"zz_test_{scope.replace('-', '_')}_v1",)).fetchall()
        assert any("hnsw" in r[0] and "vector_cosine_ops" in r[0]
                   for r in idx), idx

    def test_ensure_idempotent(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.ensure_collection(scope, DIM)   # 二次不炸不重建
        assert store.count(scope) == 0

    def test_ensure_dim_mismatch_fails_fast(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, 3)
        with pytest.raises(ValueError, match="维度不一致"):
            store.ensure_collection(scope, 4)

    def test_ensure_rejects_bad_dim(self, store):
        with pytest.raises(ValueError, match="dim"):
            store.ensure_collection(new_scope_id(), 0)

    def test_drop_collection(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.drop_collection(scope)
        assert not _table_exists(scope)
        assert store.count(scope) == 0        # 表不在 → 0,不炸
        store.drop_collection(scope)          # 幂等

    def test_ping(self, store):
        store.ping()


class TestDataOps:
    """写入/覆盖/检索/过滤/删除/计数往返。"""

    def test_upsert_count_roundtrip(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        n = store.upsert_chunks(scope, [
            {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
             "text": "甲段", "vector": V_A},
            {"chunk_id": "c2", "doc_id": "d1", "seq": 1,
             "text": "乙段", "vector": V_B},
            {"chunk_id": "c3", "doc_id": "d2", "seq": 0,
             "text": "丙段", "vector": V_C},
        ])
        assert n == 3
        assert store.count(scope) == 3

    def test_upsert_overwrites_same_chunk_id(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.upsert_chunks(scope, [
            {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
             "text": "旧文本", "vector": V_A}])
        store.upsert_chunks(scope, [
            {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
             "text": "新文本", "vector": V_B}])
        assert store.count(scope) == 1
        hits = store.search(scope, V_B, top_k=5)
        assert len(hits) == 1 and hits[0]["text"] == "新文本"

    def test_upsert_requires_table(self, store):
        scope = new_scope_id()
        with pytest.raises(RuntimeError, match="不存在"):
            store.upsert_chunks(scope, [
                {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
                 "text": "x", "vector": V_A}])

    def test_search_semantics_and_ordering(self, store):
        """余弦相似度:同向 score≈1,正交 score≈0;按相似度降序。"""
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.upsert_chunks(scope, [
            {"chunk_id": "same", "doc_id": "d1", "seq": 0,
             "text": "同向", "vector": V_A},
            {"chunk_id": "near", "doc_id": "d1", "seq": 1,
             "text": "近", "vector": V_A2},
            {"chunk_id": "orth", "doc_id": "d1", "seq": 2,
             "text": "正交", "vector": V_B},
        ])
        hits = store.search(scope, V_A, top_k=3)
        assert [h["chunkId"] for h in hits] == ["same", "near", "orth"]
        assert hits[0]["score"] == pytest.approx(1.0, abs=1e-6)  # 完全同向
        assert hits[1]["score"] == pytest.approx(0.9998, abs=1e-3)
        assert hits[2]["score"] == pytest.approx(0.0, abs=1e-6)   # 正交
        assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"]
        # 字段往返完整
        assert hits[0]["docId"] == "d1" and hits[0]["seq"] == 0
        assert hits[0]["text"] == "同向"

    def test_search_doc_filter_and_topk(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.upsert_chunks(scope, [
            {"chunk_id": f"c{i}", "doc_id": f"d{i % 2}", "seq": i,
             "text": f"t{i}", "vector": v}
            for i, v in enumerate([V_A, V_A2, V_B, V_C])])
        hits = store.search(scope, V_A, top_k=1, doc_id="d0")
        assert len(hits) == 1 and hits[0]["docId"] == "d0"

    def test_search_missing_table_returns_empty(self, store):
        assert store.search(new_scope_id(), V_A, top_k=3) == []

    def test_existing_chunk_ids(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.upsert_chunks(scope, [
            {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
             "text": "x", "vector": V_A},
            {"chunk_id": "c2", "doc_id": "d2", "seq": 0,
             "text": "y", "vector": V_B}])
        assert store.existing_chunk_ids(scope, "d1") == {"c1"}
        assert store.existing_chunk_ids(scope, "dX") == set()

    def test_delete_by_doc(self, store):
        scope = new_scope_id()
        store.ensure_collection(scope, DIM)
        store.upsert_chunks(scope, [
            {"chunk_id": "c1", "doc_id": "d1", "seq": 0,
             "text": "x", "vector": V_A},
            {"chunk_id": "c2", "doc_id": "d2", "seq": 0,
             "text": "y", "vector": V_B}])
        store.delete_by_doc(scope, "d1")
        assert store.count(scope) == 1
        assert store.existing_chunk_ids(scope, "d1") == set()
        store.delete_by_doc(scope, "d1")   # 幂等
        store.delete_by_doc(new_scope_id(), "d1")  # 表不在 → 静默


class TestScopeContract:
    """scope_id 契约入口防御(与协议约定同款)。"""

    @pytest.mark.parametrize("bad", ["我的库", "kb-001", "", "UPPER-UUID"])
    def test_rejects_user_input(self, store, bad):
        with pytest.raises(ValueError, match="scope_id"):
            store.ensure_collection(bad, DIM)
        with pytest.raises(ValueError, match="scope_id"):
            store.count(bad)
        with pytest.raises(ValueError, match="scope_id"):
            store.search(bad, V_A)

    def test_missing_prefix_fails(self):
        with pytest.raises(ValueError, match="必传"):
            PgVectorStore(dsn=DSN, collection_prefix="")

    def test_missing_dsn_fails(self):
        with pytest.raises(ValueError, match="dsn"):
            PgVectorStore(dsn=" ", collection_prefix="kg")


class TestFactoryBackend:
    """VECTOR_BACKEND 切换:默认 pg,milvus 保留;缓存键含 backend。"""

    def test_default_backend_is_pg(self, monkeypatch):
        monkeypatch.delenv("VECTOR_BACKEND", raising=False)
        assert vs.resolve_vector_backend() == "pg"

    def test_unknown_backend_fails_fast(self, monkeypatch):
        monkeypatch.setenv("VECTOR_BACKEND", "redis")
        with pytest.raises(ValueError, match="VECTOR_BACKEND"):
            vs.resolve_vector_backend()

    def test_factory_builds_pg_and_caches_by_prefix(self, monkeypatch):
        monkeypatch.delenv("VECTOR_BACKEND", raising=False)
        vs._cached_stores.clear()
        try:
            s1 = vs.get_vector_store({}, collection_prefix="kg")
            s2 = vs.get_vector_store({}, collection_prefix="bi")
            assert isinstance(s1, PgVectorStore) and s1._prefix == "kg"
            assert isinstance(s2, PgVectorStore) and s2._prefix == "bi"
            assert s1 is not s2
            assert vs.get_vector_store({}, collection_prefix="kg") is s1  # 命中缓存
        finally:
            vs.reset_vector_store_cache()

    def test_factory_backend_milvus_failfast(self, monkeypatch):
        """VECTOR_BACKEND=milvus(已下线)→ fail-fast 报错指引改 pg,
        而非静默落回 pg 让"以为在用 Milvus"的部署继续蒙着跑。"""
        monkeypatch.setenv("VECTOR_BACKEND", "milvus")
        vs._cached_stores.clear()
        try:
            with pytest.raises(ValueError, match="pgvector 单后端"):
                vs.get_vector_store({}, collection_prefix="kg")
        finally:
            vs.reset_vector_store_cache()
            monkeypatch.delenv("VECTOR_BACKEND", raising=False)


class TestPgConnectionSelfHeal:
    """连接自愈(功能对齐审查:Milvus 有 _with_reconnect,PG 补齐同款)。"""

    def test_reconnects_on_operational_error(self, monkeypatch):
        from sdk.pg_vector_store import PgVectorStore
        store = object.__new__(PgVectorStore)
        store._dsn = "postgresql://x"
        store._prefix = "kg"
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                import psycopg.errors as pe
                raise pe.OperationalError("connection reset")
            return "ok"

        store._new_pool = lambda: None
        store._pool = type("P", (), {"close": lambda self: None})()
        assert store._with_reconnect(flaky) == "ok"
        assert calls["n"] == 2

    def test_other_errors_not_retried(self, monkeypatch):
        from sdk.pg_vector_store import PgVectorStore
        store = object.__new__(PgVectorStore)
        store._dsn, store._prefix = "x", "kg"
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("业务错误")

        import pytest
        with pytest.raises(ValueError):
            store._with_reconnect(boom)
        assert calls["n"] == 1
