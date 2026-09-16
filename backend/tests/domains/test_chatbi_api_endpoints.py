"""chatbi 管理端 API 端点级测试(五审 5.1 要求: 纯函数测试发现不了
函数体里的未注入 request——必须走真实的 FastAPI 路由调用)。

跑法(专属库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_api \\
      ./venv/bin/python -m pytest tests/domains/test_chatbi_api_endpoints.py -q
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.chatbi.models import (
    Column, Model, Relationship, SemanticModelContent,
)


@pytest.fixture()
def client(monkeypatch):
    """chatbi router + 测试 pack 库 + 放行管理鉴权。"""
    monkeypatch.setenv("ADMIN_TOKEN", "t")
    import sdk.pack_api as pp
    from domains.chatbi import api as chatbi_api
    from domains.chatbi import runtime

    async def _allow(request):
        return None
    pp._admin_auth_fn = _allow

    # 测试库替换单例(api._db 走 runtime.get_pack_db)
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store, configure_encryption
    from cryptography.fernet import Fernet
    configure_encryption(Fernet.generate_key().decode())
    test_db = PackRelationalDB("chatbi")
    init_store(test_db)
    monkeypatch.setattr(runtime, "get_pack_db", lambda: test_db)
    monkeypatch.setattr(chatbi_api, "get_pack_db", lambda: test_db)

    app = FastAPI()
    app.include_router(chatbi_api.router)
    app.state.llm_client = object()
    with TestClient(app) as c:
        yield c, test_db


def _content() -> SemanticModelContent:
    orders = Model(name="orders", display_name="订单", columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
        Column(name="user_id", display_name="用户", data_type="INTEGER"),
    ], relationships=[Relationship(
        name="r1", target_model="users", join_type="LEFT",
        on="orders.user_id = users.id", type="N:1", source="fk",
        confidence=0.9)])
    users = Model(name="users", display_name="用户", columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
    ])
    return SemanticModelContent(models=[orders, users])


@pytest.fixture()
def ds_id(client):
    c, db = client
    from domains.chatbi import datasources, semantic
    info = datasources.create_datasource(
        db, "端点测库", "postgresql", "localhost", 5432, "x", "u", "p")
    semantic.save_content(db, info.id, _content(), source="scan")
    return info.id


class TestGraphEndpoints:
    """五审 5.1 回归锚: 曾因函数体引用未注入的 request 稳定 500。"""

    def test_get_graph_200(self, client, ds_id):
        c, _ = client
        r = c.get(f"/datasources/{ds_id}/graph")
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["nodes"]) == 2 and len(data["edges"]) == 1

    def test_join_path_preview_200(self, client, ds_id):
        c, _ = client
        r = c.post(f"/join-path-preview?ds_id={ds_id}",
                   json={"tables": ["orders", "users"]})
        assert r.status_code == 200, r.text
        assert "orders" in r.json()["expanded_tables"]

    def test_subgraph_communities_hubs_200(self, client, ds_id):
        c, _ = client
        for url in (f"/datasources/{ds_id}/graph/subgraph?center=orders&depth=1",
                    f"/datasources/{ds_id}/graph/communities",
                    f"/datasources/{ds_id}/graph/hubs?top_k=5",
                    f"/datasources/{ds_id}/graph/impact?table=orders",
                    f"/datasources/{ds_id}/graph/reverse-relationships?table=users"):
            r = c.get(url)
            assert r.status_code == 200, f"{url} → {r.status_code}: {r.text[:100]}"

    def test_graph_404_unscanned(self, client):
        c, _ = client
        assert c.get("/datasources/no-such-ds/graph").status_code == 404


class TestMetricsEndpoint:
    def test_metrics_and_window(self, client, ds_id):
        c, db = client
        from domains.chatbi.query_stats import record_query
        record_query(db, data_source_id=ds_id, status="ok", duration_ms=12000)
        for days in ("", "?days=1", "?days=7", "?days=30"):
            r = c.get(f"/datasources/{ds_id}/metrics{days}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["slow_query_ms"] == 10000
            assert body["metrics"]["query_count"] == 1  # 窗口参数不再让汇总恒空
            assert len(body["slow_queries"]) == 1       # 12s > 10s 慢查询


class TestMemoryEndpoints:
    def test_create_requires_ds(self, client):
        c, _ = client
        r = c.put("/memories", json={"name": "x", "content": "y"})
        assert r.status_code == 422

    def test_create_rejects_unknown_ds(self, client):
        c, _ = client
        r = c.put("/memories", json={"name": "x", "content": "y",
                                     "data_source_id": "ghost"})
        assert r.status_code == 404

    def test_create_and_backfill_flow(self, client, ds_id):
        c, db = client
        from domains.chatbi.memory import get_memory_store
        store = get_memory_store(db)
        # 直接造一条无归属(绕过 API 校验——模拟历史存量)
        store.save_memory(name="孤儿", description="d", content="c")
        r = c.post("/memories/backfill-scope",
                   json={"data_source_id": ds_id})
        assert r.status_code == 200
        assert r.json()["backfilled"] == 1
