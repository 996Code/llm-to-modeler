"""客服直连 web_search 底层测试——other 分支的联网意图分流(方案 A)。

覆盖:开关开+话术命中→联网总结卡;开关关/话术不中/底座未配置→回落 KB 兜底;
联网失败 fail-open 回落。不依赖真实 SearXNG/LLM(websearch 全替身)。
"""
from types import SimpleNamespace

import pytest

from domains.metrology_assistant.tools import assistant as mta
from domains.metrology_assistant.legacy_demo import demo_scenarios
from domains.metrology_assistant import capability_import as ci


class _Ctx:
    """ctx 替身:emit/trace 全吞。"""

    def emit(self, *args, **kwargs):
        pass

    def trace(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(mta, '_current_scenarios', demo_scenarios)
    monkeypatch.setattr(ci, 'resolve_scenarios', lambda table: table)
    monkeypatch.setattr(ci, 'resolve_datasource_for_tool', lambda server, tool: None)
    yield monkeypatch


def _state(ws_enabled):
    return {"pack_params": {"web_search": {"enabled": ws_enabled}}}


class _Registry:
    """装配替身:web_search 工具在册(停用语义测试单独换空 registry)。"""

    def get(self, name):
        return object() if name == 'web_search' else None


class _App:
    registry = _registry = None


def _app():
    a = _App()
    a.registry = _Registry()
    return a


def _patch_websearch(monkeypatch, configured=True, boom=False):
    """websearch 底层替身(记调用;boom=搜索抛错)。mock search_multi——
    客服直连与工具入口已升级为规划+多角度搜索。"""
    from domains.web_search import websearch
    calls = []

    def fake_search(app_state, question, top_k=5, conv_id=None):
        calls.append(("search", question))
        if boom:
            from sdk.tool import PackInvokeError
            raise PackInvokeError("超时", PackInvokeError.RETRYABLE)
        return {"question": question, "queries": [question], "cached": False,
                "results": [
                    {"title": "AI 资讯", "url": "https://a.test/x", "content": "摘要"}]}

    def fake_synth(app_state, query, results, conv_id=None):
        calls.append(("synthesize", query))
        return "**总结**要点 [来源:1]"

    monkeypatch.setattr(websearch, "search_multi", fake_search)
    monkeypatch.setattr(websearch, "synthesize_answer", fake_synth)
    monkeypatch.setattr(websearch, "settings_url",
                        lambda s: "http://searxng.test" if configured else "")
    return calls


def test_toggle_on_with_web_hint_routes_to_websearch(env):
    _patch_websearch(env)
    tool = mta.MetrologyAssistantTool(app_state=_app())
    out = tool._try_websearch("帮我在网上搜一下ai最新的消息", _Ctx(),
                              _state(True), lambda *a: None)
    assert out is not None
    assert out.artifact["type"] == "web_search_result"
    assert out.artifact["queries"] == ["帮我在网上搜一下ai最新的消息"]  # search_multi 透传规划词(fake 以原话为词)
    assert out.artifact["pack"] == "metrology_assistant"
    assert "[来源:1]" in out.summary


def test_toggle_off_falls_back(env):
    calls = _patch_websearch(env)
    tool = mta.MetrologyAssistantTool(app_state=_app())
    out = tool._try_websearch("帮我在网上搜一下ai最新的消息", _Ctx(),
                              _state(False), lambda *a: None)
    assert out is None
    assert calls == []


def test_plain_phrasing_without_web_words_falls_back(env):
    calls = _patch_websearch(env)
    tool = mta.MetrologyAssistantTool(app_state=_app())
    out = tool._try_websearch("查一下压力表检定规程", _Ctx(),
                              _state(True), lambda *a: None)
    assert out is None
    assert calls == []


def test_unconfigured_searxng_falls_back_silently(env):
    calls = _patch_websearch(env, configured=False)
    tool = mta.MetrologyAssistantTool(app_state=_app())
    out = tool._try_websearch("上网查一下最新消息", _Ctx(),
                              _state(True), lambda *a: None)
    assert out is None
    assert calls == []


def test_search_failure_degrades_to_kb_fallback(env):
    calls = _patch_websearch(env, boom=True)
    tool = mta.MetrologyAssistantTool(app_state=_app())
    out = tool._try_websearch("上网查一下最新消息", _Ctx(),
                              _state(True), lambda *a: None)
    assert out is None  # fail-open:回落 KB 兜底链
    assert calls and calls[0][0] == "search"


def test_web_hint_words_coverage():
    from domains.metrology_assistant.tools.assistant import MetrologyAssistantTool as T
    for hint in ("上网", "联网", "网上查", "网上搜", "搜索引擎", "互联网上"):
        assert hint in T._WEB_HINTS


def test_pack_disabled_falls_back(env):
    """web_search pack 停用(registry 无此工具):直连层自查回落 KB 兜底。"""
    _patch_websearch(env)
    tool = mta.MetrologyAssistantTool(app_state=_app())

    class _EmptyRegistry:
        def get(self, name):
            return None

    tool._app_state.registry = _EmptyRegistry()
    out = tool._try_websearch("上网查一下最新消息", _Ctx(), _state(True),
                              lambda *a: None)
    assert out is None
