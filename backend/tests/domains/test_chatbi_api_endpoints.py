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
    # monkeypatch 落盘(裸赋值不恢复会泄漏到整个进程,后续测试的管理
    # 鉴权全被换成恒放行——曾让 kg 同步测试的组合顺序假失败)
    monkeypatch.setattr(pp, "_admin_auth_fn", _allow)

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


class TestSemanticPutValidation:
    """十三审 7.6/7.2: 语义PUT校验ds存在 + 缺expected返回428."""

    def test_put_nonexistent_ds_404(self, client):
        c, _ = client
        r = c.put("/datasources/no-such-ds/semantic-models",
                  json={"content": {"models": []}})
        assert r.status_code == 404

    def test_put_missing_expected_428(self, client, ds_id):
        c, _ = client
        r = c.put(f"/datasources/{ds_id}/semantic-models",
                  json={"content": {"models": []}})
        assert r.status_code == 428

    def test_put_with_expected_200(self, client, ds_id):
        c, db = client
        from domains.chatbi import semantic
        _, cur_v = semantic.load_content(db, ds_id)
        r = c.put(f"/datasources/{ds_id}/semantic-models",
                  json={"content": {"models": [
                      {"name": "t", "display_name": "T", "columns": []}]},
                    "expected_version": cur_v})
        assert r.status_code == 200, r.text


class TestGraphApiValidation:
    """十三审 7.2: 图谱增删缺expected返回428."""

    def test_add_missing_expected_428(self, client, ds_id):
        c, _ = client
        r = c.post(f"/datasources/{ds_id}/graph/relationship",
                   json={"from_table": "orders", "target_table": "users",
                         "on": "orders.user_id = users.id"})
        assert r.status_code == 428

    def test_delete_missing_expected_428(self, client, ds_id):
        c, _ = client
        r = c.delete(f"/datasources/{ds_id}/graph/relationship",
                     params={"from_table": "orders", "target_table": "users"})
        assert r.status_code == 428


class TestRollbackValidation:
    """十三审 7.5: 回滚缺expected_current返回428."""

    def test_rollback_missing_expected_428(self, client, ds_id):
        c, _ = client
        r = c.post("/semantic-rollback",
                   params={"ds_id": ds_id, "version": 1})
        assert r.status_code == 428


class TestCardinalityValidation:
    """十四审 7.1: 四种合法基数全部可提交."""

    def test_all_four_cardinalities_accepted(self, client, ds_id):
        c, db = client
        from domains.chatbi import semantic
        _, cur_v = semantic.load_content(db, ds_id)
        for card in ("N:1", "1:N", "1:1", "N:N"):
            r = c.post(f"/datasources/{ds_id}/graph/relationship",
                       json={"from_table": "orders", "target_table": "users",
                             "on": "orders.user_id = users.id",
                             "cardinality": card,
                             "expected_version": cur_v})
            assert r.status_code in (200, 409), \
                f"cardinality {card} → {r.status_code}: {r.text[:80]}"
            if r.status_code == 200:
                # delete it for next iteration
                c.delete(f"/datasources/{ds_id}/graph/relationship",
                         params={"from_table": "orders", "target_table": "users",
                                 "on": "orders.user_id = users.id",
                                 "expected_version": r.json()["version"]})
                _, cur_v = semantic.load_content(db, ds_id)

    def test_invalid_cardinality_422(self, client, ds_id):
        c, _ = client
        r = c.post(f"/datasources/{ds_id}/graph/relationship",
                   json={"from_table": "o", "target_table": "u",
                         "on": "o.x=u.y", "cardinality": "M:N",
                         "expected_version": 1})
        assert r.status_code == 422


class TestFirstPutRequiresExpected:
    """十四审 7.4: 首次 PUT 也必须带 expected(=0)."""

    def test_first_put_without_expected_428(self, client, ds_id):
        """已有版本时缺 expected → 428(不是只在有版本时才拒绝)."""
        c, _ = client
        r = c.put(f"/datasources/{ds_id}/semantic-models",
                  json={"content": {"models": []}})
        assert r.status_code == 428


class TestRebuildIndexEndpoint:
    """遗留①回归: 切向量后端/脏 active 指针后无入口重建索引的兜底端点。
    审查 I-1 后为后台任务模式:端点只做校验+submit(dedu_key 去重),
    执行走 chatbi.rebuild_index 任务 handler(同步执行会阻塞事件循环)。"""

    def _client_with_manager(self, manager):
        """独立小 app + 可控 task_manager 替身(client fixture 未暴露 app)。"""
        from fastapi import FastAPI
        from domains.chatbi import api as chatbi_api
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(chatbi_api.router)
        app.state.llm_client = object()
        app.state.task_manager = manager
        return TestClient(app)

    def test_submit_rebuild_task(self, client, ds_id):
        """已扫描数据源 → 200 + submit 后台任务(payload/dedupe 正确)。"""
        submitted = []

        class _M:
            def submit(self, task_type, payload=None, dedupe_key=None):
                submitted.append({"type": task_type, "payload": payload,
                                  "dedupe_key": dedupe_key})
                return {"id": "task-1"}
        c2 = self._client_with_manager(_M())
        r = c2.post(f"/datasources/{ds_id}/rebuild-index")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "task_id": "task-1"}
        assert submitted[0]["type"] == "chatbi.rebuild_index"
        assert submitted[0]["payload"] == {"datasource_id": ds_id}
        assert submitted[0]["dedupe_key"] == f"chatbi:rebuild:{ds_id}"

    def test_dup_running_409(self, client, ds_id):
        """已有重建任务在途 → 409(去重: 防并发刷事件循环阻塞/成本放大)。"""
        from sdk.pack_api import DuplicateTaskError

        class _DupM:
            def submit(self, *a, **k):
                raise DuplicateTaskError("dup")
        c2 = self._client_with_manager(_DupM())
        r = c2.post(f"/datasources/{ds_id}/rebuild-index")
        assert r.status_code == 409

    def test_unknown_ds_404(self, client):
        c, _ = client
        r = c.post("/datasources/no-such/rebuild-index")
        assert r.status_code == 404

    def test_unscanned_ds_422(self, client):
        c, db = client
        from domains.chatbi import datasources
        info = datasources.create_datasource(
            db, "未扫库", "postgresql", "localhost", 5432, "x", "u", "p")
        r = c.post(f"/datasources/{info.id}/rebuild-index")
        assert r.status_code == 422


class TestDatasourceCreateContract:
    """终审#2: DatasourceCreate 为 mcp 放宽 database/username/password
    默认空串——非 mcp 必填语义必须恢复(空库名/空密码的 postgresql 会
    静默落到连接期才失败, 违反"非 mcp 零变化")。mcp 三字段可空保持。"""

    def test_postgresql_missing_database_422(self, client):
        """postgresql 缺 database → 422(创建期拒绝, 不静默落库)。"""
        c, _ = client
        r = c.post("/datasources", json={
            "name": "pg缺库名", "db_type": "postgresql", "host": "localhost",
            "port": 5432, "username": "u", "password": "p"})
        assert r.status_code == 422, r.text

    def test_postgresql_empty_password_422(self, client):
        """postgresql 显式空串密码 → 同 422(默认空串不得绕过必填)。"""
        c, _ = client
        r = c.post("/datasources", json={
            "name": "pg空密码", "db_type": "postgresql", "host": "localhost",
            "port": 5432, "database": "db", "username": "u", "password": ""})
        assert r.status_code == 422, r.text

    def test_postgresql_full_credentials_200(self, client):
        """postgresql 三字段齐全 → 正常创建(非 mcp 原契约不变)。"""
        c, _ = client
        r = c.post("/datasources", json={
            "name": "pg正常", "db_type": "postgresql", "host": "localhost",
            "port": 5432, "database": "db", "username": "u", "password": "p"})
        assert r.status_code == 200, r.text
        assert r.json()["dbType"] == "postgresql"

    def test_mcp_empty_three_fields_200(self, client):
        """mcp 三字段空 → 通过(mcp 契约: database=tool_name 可空, 凭据空串)。"""
        c, _ = client
        r = c.post("/datasources", json={
            "name": "外部mcp", "db_type": "mcp", "host": "srv_a",
            "port": 0, "database": "", "username": "", "password": ""})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dbType"] == "mcp" and body["host"] == "srv_a"
