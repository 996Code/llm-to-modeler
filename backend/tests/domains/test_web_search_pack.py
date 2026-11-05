"""web_search 工具契约测试——SearXNG 联网搜索(不依赖真实容器)。

覆盖:正常搜索/缓存命中/未配置 URL(credential)/上游超时(retryable)/
top_k 裁剪/engines 传全名/调研记录落库。
"""
import json
from types import SimpleNamespace

import pytest

from domains.web_search import websearch
from domains.web_search.tools.web_search import WebSearchTool
from sdk.tool import PackInvokeError, ToolContext


def _searxng_payload(n=8):
    """SearXNG JSON API 响应形状(results 数组,每条 title/url/content)。"""
    return {"query": "q", "results": [
        {"title": f"检定规程 {i}", "url": f"https://spc.org.cn/doc{i}",
         "content": f"第 {i} 号技术规范摘要"} for i in range(1, n + 1)]}


class _FakeStore:
    """web_search logs 连接替身:websearch._conn 返回记录 SQL 的假连接。"""

    def __init__(self):
        self.executed = []
        self.inserted = []  # 已 INSERT 的参数元组(供 SELECT 回读)

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
                return SimpleNamespace(fetchall=lambda: rows,
                                       rowcount=len(rows))
            if sql_u.strip().startswith("DELETE"):
                n = len(self._outer.inserted)
                self._outer.inserted = []
                return SimpleNamespace(fetchall=lambda: [], rowcount=n)
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def connect(self):
        return self._Conn(self)


class _FakeDB:
    """替 websearch._db(PackRelationalDB 协议:connect() 返回连接)。"""
    def __init__(self, store):
        self._store = store
    def connect(self):
        return self._store.connect()


@pytest.fixture
def env(monkeypatch):
    """app_state=settings_store 假读出 searxng_url + kg store 替身。"""
    store = _FakeStore()
    state = SimpleNamespace(
        settings_store=None,
        conversation_store=None,
    )
    monkeypatch.setattr(websearch, "_db", _FakeDB(store))
    monkeypatch.setattr(websearch, "settings_url",
                        lambda s: "http://searxng.test:8080")
    websearch.reset_cache()
    ctx = ToolContext(llm_client=None, asset_client=None, conversation=None,
                      emit=lambda *args, **kwargs: None, conv_id="ws-test")
    return SimpleNamespace(state=state, store=store, ctx=ctx,
                           monkeypatch=monkeypatch)


def _mock_get(env, payload=None, calls=None, error=None):
    """monkeypatch httpx.Client.get——记录调用参数,按需返回/抛错。"""
    if calls is None:
        calls = []
    payload = payload if payload is not None else _searxng_payload()

    class FakeResp:
        status_code = 200
        text = json.dumps(payload)

        def raise_for_status(self):
            pass

        def json(self):
            return payload

    def fake_get(self, url, **kw):
        calls.append({"url": url, **kw})
        if error:
            raise error
        return FakeResp()

    env.monkeypatch.setattr(websearch.httpx.Client, "get", fake_get)
    return calls


# ── 领域客户端 websearch.search ────────────────────────────

def test_search_returns_trimmed_results_with_source_urls(env):
    calls = _mock_get(env)
    out = websearch.search(env.state, "JJG 544 压力控制器", top_k=5)
    assert len(out["results"]) == 5
    for r in out["results"]:
        assert set(r) == {"title", "url", "content"}
        assert r["url"].startswith("https://")
    assert out["cached"] is False
    # engines 传全名(shortcut 不生效,实测踩坑);httpx params 编码进 URL
    assert calls[0]["params"]["engines"] == "google"
    assert calls[0]["params"]["format"] == "json"
    assert calls[0]["params"]["language"] == "zh-CN"  # 中文区结果(防日文页混入)
    assert calls[0]["params"]["q"] == "JJG 544 压力控制器"


def test_search_cache_hit_skips_http(env):
    calls = _mock_get(env)
    websearch.search(env.state, "同一问题", top_k=5)
    websearch.search(env.state, "同一问题", top_k=5)
    assert len(calls) == 1
    # 第二次返回缓存标记
    out = websearch.search(env.state, "同一问题", top_k=5)
    assert out["cached"] is True


def test_search_unconfigured_url_raises_credential(env, monkeypatch):
    monkeypatch.setattr(websearch, "settings_url", lambda s: "")
    with pytest.raises(PackInvokeError) as ei:
        websearch.search(env.state, "q")
    assert ei.value.error_type == PackInvokeError.CREDENTIAL


def test_search_upstream_timeout_raises_retryable(env):
    import httpx
    _mock_get(env, error=httpx.TimeoutException("boom"))
    with pytest.raises(PackInvokeError) as ei:
        websearch.search(env.state, "q")
    assert ei.value.error_type == PackInvokeError.RETRYABLE


# ── 工具层 WebSearchTool ───────────────────────────────────

def test_tool_execute_returns_data_artifact_with_sources(env, monkeypatch):
    _mock_get(env)
    # LLM 综合回答替身:返回带 [来源:N] 引用的总结(真实链路走 web_answer.j2)
    monkeypatch.setattr(websearch, "synthesize_answer",
                        lambda s, q, r, conv_id=None:
                        f"**总结**:共 {len(r)} 条结果,首条为 {r[0]['title']} [来源:1]")
    tool = WebSearchTool(env.state)
    result = tool.execute({"query": "JJG 544 压力控制器检定规程"}, env.ctx)
    assert result.artifact_type == "data"
    assert result.artifact["type"] == "web_search_result"
    assert result.artifact["query"] == "JJG 544 压力控制器检定规程"
    assert len(result.artifact["results"]) == 5
    # card-only 展示(前端专用卡:总结+角标+来源卡)
    assert result.artifact["resultDisplay"] == "card"
    assert result.artifact["pack"] == "web_search"
    # summary=LLM 综合回答(带引用编号)
    assert "[来源:1]" in result.summary
    assert result.formatted["resultCount"] == 5


def test_tool_answer_llm_failure_degrades_to_source_list(env, monkeypatch):
    """LLM 综合失败 fail-open:降级带来源 URL 的列表(不丢搜索结果)。"""
    _mock_get(env)
    def boom(s, q, r, conv_id=None):
        raise RuntimeError("llm down")
    # synthesize_answer 内部 try/except 已兜底——这里直接测它的降级路径
    monkeypatch.setattr(websearch, "_prompts", lambda: (_ for _ in ()).throw(RuntimeError("no")))
    answer = websearch.synthesize_answer(env.state, "q", _searxng_payload()["results"][:3])
    assert "https://" in answer
    assert "1." in answer


def test_tool_preflight_blocks_when_toggle_off(env):
    """路由选中但开关没开:给点亮开关的指引(独立 pack 语义)。"""
    tool = WebSearchTool(env.state)
    result = tool.preflight(
        {"query": "q", "pack_params": {"web_search": {"enabled": False}}}, env.ctx)
    assert result is not None
    assert "「联网」开关" in result.error_for_llm


def test_tool_preflight_passes_when_toggle_on(env):
    tool = WebSearchTool(env.state)
    result = tool.preflight(
        {"query": "q", "pack_params": {"web_search": {"enabled": True}}}, env.ctx)
    assert result is None


def test_tool_preflight_blocks_when_unconfigured(env, monkeypatch):
    monkeypatch.setattr(websearch, "settings_url", lambda s: "")
    tool = WebSearchTool(env.state)
    result = tool.preflight({"query": "q"}, env.ctx)
    assert result is not None
    assert result.error_for_llm


def test_tool_validate_input_requires_query(env):
    tool = WebSearchTool(env.state)
    assert tool.validate_input({}) is not None
    assert tool.validate_input({"query": "  "}) is not None
    assert tool.validate_input({"query": "检定规程"}) is None


def test_tool_upstream_error_maps_to_error_for_llm(env):
    import httpx
    _mock_get(env, error=httpx.TimeoutException("boom"))
    tool = WebSearchTool(env.state)
    # 引擎会捕获 PackInvokeError;工具层测试直接验证抛出类型
    with pytest.raises(PackInvokeError):
        tool.execute({"query": "q"}, env.ctx)


# ── 调研记录落库 ───────────────────────────────────────────

def test_log_search_persists_query_and_results(env):
    _mock_get(env)
    out = websearch.search(env.state, "调研问题", top_k=5)
    log = websearch.log_search(env.state, query="调研问题", results=out["results"],
                               cached=out["cached"], operator="admin")
    assert log["id"]
    assert log["resultCount"] == 5
    # 建表+INSERT 都走了 fake conn
    sqls = " ".join(s for s, _ in env.store.executed)
    assert "web_search_logs" in sqls
    assert "INSERT INTO web_search_logs" in sqls


def test_list_and_clear_logs(env):
    _mock_get(env)
    out = websearch.search(env.state, "调研", top_k=5)
    websearch.log_search(env.state, query="调研", results=out["results"],
                         cached=False, operator="admin")
    rows = websearch.list_logs(env.state, limit=10)
    assert len(rows) == 1
    assert rows[0]["query"] == "调研"
    assert rows[0]["operator"] == "admin"
    websearch.clear_logs(env.state)
    assert websearch.list_logs(env.state, limit=10) == []
