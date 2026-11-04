"""web_search 管理端点契约测试——admin 门控/正常路径/参数校验。

不依赖真实 SearXNG:monkeypatch websearch.search 返回固定结果。
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import api as kg_api, runtime, websearch
from sdk import pack_api


@pytest.fixture
def env(monkeypatch):
    """挂 KG api router + 注册放行 admin 鉴权 + websearch 替身。"""
    # admin 门控:注册"永远放行"的鉴权实现(门控行为单独用例覆盖)
    async def allow(request):
        return None
    pack_api.register_admin_auth(allow)

    store = SimpleNamespace()  # log DAO 走 fake conn(不真连 PG)
    executed = []
    inserted = []

    class _Conn:
        def execute(self, sql, params=()):
            executed.append((sql, params))
            sql_u = sql.upper()
            if "INSERT INTO KG_WEB_SEARCH_LOGS" in sql_u:
                inserted.append(params)
                return None
            if sql_u.strip().startswith("SELECT"):
                rows = [dict(zip(
                    ("id", "query", "results_json", "result_count",
                     "cached", "operator", "created_at"), p))
                    for p in inserted]
                return SimpleNamespace(fetchall=lambda: rows)
            if sql_u.strip().startswith("DELETE"):
                inserted.clear()
                return SimpleNamespace(fetchall=lambda: [], rowcount=0)
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    store._get_conn = lambda: _Conn()
    monkeypatch.setattr(runtime, "get_kg_store", lambda s: store)
    websearch.reset_cache()

    # search 替身:返回固定 2 条结果
    def fake_search(app_state, query, top_k=5):
        return {"query": query, "cached": False, "results": [
            {"title": "检定规程 A", "url": "https://a.test/x",
             "content": "摘要 A"},
            {"title": "检定规程 B", "url": "https://b.test/y",
             "content": "摘要 B"}][:top_k]}
    monkeypatch.setattr(websearch, "search", fake_search)

    app = FastAPI()
    app.include_router(kg_api.router, prefix="/api/packs/knowledge_graph")
    app.state = SimpleNamespace(settings_store=None, conversation_store=None)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, executed=executed,
                              monkeypatch=monkeypatch)


def test_web_search_admin_gate_blocks_without_auth(env):
    """admin_required 转发宿主鉴权:宿主拒绝时端点 401/403。"""
    from fastapi import HTTPException

    async def deny(request):
        raise HTTPException(401, "未授权")
    pack_api.register_admin_auth(deny)
    resp = env.client.post("/api/packs/knowledge_graph/web-search",
                           json={"query": "q"})
    assert resp.status_code in (401, 403)


def test_web_search_returns_results_and_logs(env):
    resp = env.client.post("/api/packs/knowledge_graph/web-search",
                           json={"query": "JJG 544", "top_k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["url"] == "https://a.test/x"
    assert body["logId"]
    # 调研记录已 INSERT(fake conn 捕获)
    sqls = " ".join(s for s, _ in env.executed)
    assert "INSERT INTO kg_web_search_logs" in sqls


def test_web_search_empty_query_422(env):
    resp = env.client.post("/api/packs/knowledge_graph/web-search",
                           json={"query": "   "})
    assert resp.status_code == 422


def test_web_search_upstream_error_502_without_internal_detail(env):
    from sdk.tool import PackInvokeError

    def boom(app_state, query, top_k=5):
        raise PackInvokeError("连接 http://searxng.internal:8080 超时",
                              PackInvokeError.RETRYABLE)
    env.monkeypatch.setattr(websearch, "search", boom)
    resp = env.client.post("/api/packs/knowledge_graph/web-search",
                           json={"query": "q"})
    assert resp.status_code == 502
    # 内部地址不回显给客户端
    assert "searxng.internal" not in resp.json().get("detail", "")


def test_web_search_logs_list_and_clear(env):
    env.client.post("/api/packs/knowledge_graph/web-search",
                    json={"query": "调研 1"})
    resp = env.client.get("/api/packs/knowledge_graph/web-search/logs")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)

    resp = env.client.delete("/api/packs/knowledge_graph/web-search/logs")
    assert resp.status_code == 200
    # 清空后列表为空(fake conn 的 SELECT 返回 None → 空列表)
    resp = env.client.get("/api/packs/knowledge_graph/web-search/logs")
    assert resp.json() == []
