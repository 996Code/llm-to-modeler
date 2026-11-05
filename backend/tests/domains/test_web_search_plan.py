"""搜索词规划(plan_queries/search_multi)测试——改写/合并/降级。"""
from types import SimpleNamespace

import pytest

from domains.web_search import websearch
from sdk.tool import PackInvokeError


def _searxng_ok(n=3):
    return {"results": [
        {"title": f"结果 {i}", "url": f"https://r.test/{i}",
         "content": f"摘要 {i}"} for i in range(1, n + 1)]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(websearch, "settings_url", lambda s: "http://searxng.test")
    websearch.reset_cache()
    return SimpleNamespace(monkeypatch=monkeypatch)


class _LLM:
    def __init__(self, payload):
        self._p = payload

    def chat_json(self, messages, **kw):
        return self._p


def _mock_search(env, per_query):
    """per_query: {搜索词: payload};未列出的词返回 _searxng_ok()。"""
    calls = []

    def fake_search(app_state, query, top_k=5):
        calls.append(query)
        return {"query": query, "cached": False,
                "results": (per_query.get(query) or _searxng_ok())["results"][:top_k]}

    env.monkeypatch.setattr(websearch, "search", fake_search)
    return calls


def test_plan_queries_keeps_entity_and_caps(env):
    state = SimpleNamespace(llm_client=_LLM(
        {"queries": ['"陈蔓" 重名', "全国 同名 陈蔓 查询", "陈蔓", "多余的一条", "再一条"]}))
    qs = websearch.plan_queries(state, "全国有多少人叫陈蔓")
    assert qs == ['"陈蔓" 重名', "全国 同名 陈蔓 查询", "陈蔓"]  # 去重+截 3


def test_plan_queries_failopen_to_clean_question(env):
    state = SimpleNamespace(llm_client=_Boom())
    qs = websearch.plan_queries(state, "上网查一下JJG 544检定周期")
    assert qs == ["JJG 544检定周期"]


class _Boom:
    def chat_json(self, *a, **k):
        raise RuntimeError("llm down")


def test_search_multi_merges_and_dedupes(env):
    state = SimpleNamespace(llm_client=_LLM({"queries": ["Q1", "Q2"]}))
    calls = _mock_search(env, {
        "Q1": {"results": [
            {"title": "a", "url": "https://x.test/1", "content": "c"},
            {"title": "b", "url": "https://x.test/2", "content": "c"}]},
        "Q2": {"results": [
            {"title": "b重复", "url": "https://x.test/2", "content": "c"},  # 去重
            {"title": "c", "url": "https://x.test/3", "content": "c"}]},
    })
    out = websearch.search_multi(state, "某问题", top_k=5)
    assert calls == ["Q1", "Q2"]
    assert [r["url"] for r in out["results"]] == \
        ["https://x.test/1", "https://x.test/2", "https://x.test/3"]
    assert out["queries"] == ["Q1", "Q2"]


def test_search_multi_empty_question_raises(env):
    state = SimpleNamespace(llm_client=_LLM({"queries": ["x"]}))
    env.monkeypatch.setattr(websearch, "plan_queries", lambda *a, **k: [])
    with pytest.raises(PackInvokeError):
        websearch.search_multi(state, "??")


def test_plan_queries_non_list_degrades(env):
    """LLM 返回字符串形态:退化为单词直搜,不逐字符拆词(审查 I1)。"""
    state = SimpleNamespace(llm_client=_LLM({"queries": "陈蔓 重名"}))
    qs = websearch.plan_queries(state, "全国有多少人叫陈蔓")
    assert qs == ["全国有多少人叫陈蔓"]


def test_search_multi_result_cap_and_cached_or(env):
    """合并上限 top_k*2;cached 为各路 OR。"""
    state = SimpleNamespace(llm_client=_LLM({"queries": ["Q1", "Q2"]}))
    env.monkeypatch.setattr(websearch, "search", lambda s, q, top_k=5: {
        "query": q, "cached": q == "Q2",
        "results": [{"title": f"{q}-{i}", "url": f"https://x.test/{q}-{i}",
                     "content": "c"} for i in range(6)]})
    out = websearch.search_multi(state, "某问题", top_k=3)
    assert len(out["results"]) == 6  # top_k*2=6 上限生效
    assert out["cached"] is True     # Q2 命中即整体 True
