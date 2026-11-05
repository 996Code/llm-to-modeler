"""web_search 管理端点契约测试——admin 门控/正常路径/参数校验。

不依赖真实 SearXNG:monkeypatch websearch.search 返回固定结果。
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.web_search import api as ws_api, websearch
from types import SimpleNamespace as _NS
from sdk import pack_api


class _FakeStore:
    def __init__(self):
        self.executed = []
        self.inserted = []

    class _Conn:
        def __init__(self, outer):
            self._outer = outer

        def execute(self, sql, params=()):
            self._outer.executed.append((sql, params))
            sql_u = sql.upper()
            if "INSERT INTO WEB_SEARCH_LOGS" in sql_u:
                self._outer.inserted.append(params)
                return None
            if sql_u.strip().startswith("SELECT"):
                rows = [dict(zip(
                    ("id", "query", "results_json", "result_count",
                     "cached", "operator", "created_at"), p))
                    for p in self._outer.inserted]
                return _NS(fetchall=lambda: rows)
            if sql_u.strip().startswith("DELETE"):
                self._outer.inserted = []
                return _NS(fetchall=lambda: [], rowcount=0)
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def connect(self):
        return self._Conn(self)


class _FakeDB:
    def __init__(self, store):
        self._store = store

    def connect(self):
        return self._store.connect()


@pytest.fixture
def env(monkeypatch):
    """挂 KG api router + 注册放行 admin 鉴权 + websearch 替身。"""
    # admin 门控:注册"永远放行"的鉴权实现(门控行为单独用例覆盖)
    async def allow(request):
        return None
    pack_api.register_admin_auth(allow)

    store = _FakeStore()  # log DAO 走 fake conn(不真连 PG)
    monkeypatch.setattr(websearch, "_db", _FakeDB(store))
    websearch.reset_cache()

    # search 替身:返回固定 2 条结果
    def fake_search(app_state, query, top_k=5):
        return {"query": query, "cached": False, "results": [
            {"title": "检定规程 A", "url": "https://a.test/x",
             "content": "摘要 A"},
            {"title": "检定规程 B", "url": "https://b.test/y",
             "content": "摘要 B"}][:top_k]}
    monkeypatch.setattr(websearch, "search", fake_search)
    # 综合回答替身(真实链路走 LLM+web_answer.j2)
    monkeypatch.setattr(websearch, "synthesize_answer",
                        lambda s, q, r, conv_id=None:
                        f"**总结**:命中 {len(r)} 条 [来源:1]")

    app = FastAPI()
    app.include_router(ws_api.router, prefix="/api/packs/web_search")
    app.state = SimpleNamespace(settings_store=None, conversation_store=None)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, executed=store.executed,
                              monkeypatch=monkeypatch)


def test_web_search_admin_gate_blocks_without_auth(env):
    """admin_required 转发宿主鉴权:宿主拒绝时端点 401/403。"""
    from fastapi import HTTPException

    async def deny(request):
        raise HTTPException(401, "未授权")
    pack_api.register_admin_auth(deny)
    resp = env.client.post("/api/packs/web_search/web-search",
                           json={"query": "q"})
    assert resp.status_code in (401, 403)


def test_web_search_returns_results_and_logs(env):
    resp = env.client.post("/api/packs/web_search/web-search",
                           json={"query": "JJG 544", "top_k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["url"] == "https://a.test/x"
    assert "[来源:1]" in body["answer"]
    assert body["logId"]
    # 调研记录已 INSERT(fake conn 捕获)
    sqls = " ".join(s for s, _ in env.executed)
    assert "INSERT INTO web_search_logs" in sqls


def test_web_search_empty_query_422(env):
    resp = env.client.post("/api/packs/web_search/web-search",
                           json={"query": "   "})
    assert resp.status_code == 422


def test_web_search_structured_error_passthrough(env):
    """PackInvokeError(已脱敏的结构化文案)如实透传——限流不误导成配置错。"""
    from sdk.tool import PackInvokeError

    def boom(app_state, query, top_k=5):
        raise PackInvokeError("搜索引擎暂时不可用(可能被限流),请稍后重试",
                              PackInvokeError.RETRYABLE)
    env.monkeypatch.setattr(websearch, "search", boom)
    resp = env.client.post("/api/packs/web_search/web-search",
                           json={"query": "q"})
    assert resp.status_code == 502
    assert "暂时不可用" in resp.json().get("detail", "")
    # 脱敏契约:透传文案不含内部地址
    assert "http" not in resp.json().get("detail", "")


def test_web_search_generic_error_no_internal_detail(env):
    """非结构化异常:兜底文案,不回显内部串(旧契约保留在通用路径)。"""
    def boom(app_state, query, top_k=5):
        raise RuntimeError("connection to http://searxng.internal:8080 refused")
    env.monkeypatch.setattr(websearch, "search", boom)
    resp = env.client.post("/api/packs/web_search/web-search",
                           json={"query": "q"})
    assert resp.status_code == 502
    assert "searxng.internal" not in resp.json().get("detail", "")


def test_web_search_logs_list_and_clear(env):
    env.client.post("/api/packs/web_search/web-search",
                    json={"query": "调研 1"})
    resp = env.client.get("/api/packs/web_search/web-search/logs")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)

    resp = env.client.delete("/api/packs/web_search/web-search/logs")
    assert resp.status_code == 200
    # 清空后列表为空(fake conn 的 SELECT 返回 None → 空列表)
    resp = env.client.get("/api/packs/web_search/web-search/logs")
    assert resp.json() == []
