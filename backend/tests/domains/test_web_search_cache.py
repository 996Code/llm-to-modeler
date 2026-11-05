"""缓存健壮性补充:空结果不缓存 / 引擎挂起抛 retryable。"""
import pytest

from domains.web_search import websearch
from sdk.tool import PackInvokeError

from tests.domains.test_web_search_pack import _mock_get, env  # noqa: F401


@pytest.mark.parametrize("outcome", ["empty", "error", "results"])
def test_singleflight_shares_result_or_error(env, monkeypatch, outcome):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release, registered = threading.Event(), threading.Event(), threading.Event()
    calls = []
    class Flights(dict):
        reads = 0
        def get(self, key, default=None):
            self.reads += 1
            if self.reads == 4:
                registered.set()
            return super().get(key, default)
    monkeypatch.setattr(websearch, "_INFLIGHT", Flights())
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"results": [] if outcome == "empty" else [
                {"title": str(i), "url": f"https://x.test/{i}", "content": "x"}
                for i in range(5)]}
    def get(*a, **kw):
        calls.append(kw)
        entered.set()
        assert release.wait(3)
        if outcome == "error":
            raise websearch.httpx.TimeoutException("private url")
        return Resp()
    monkeypatch.setattr(websearch.httpx.Client, "get", get)
    def run(k):
        try:
            return websearch.search(env.state, "共享结果", top_k=k)
        except PackInvokeError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(run, 1)
        assert entered.wait(2)
        followers = [pool.submit(run, 5) for _ in range(3)]
        assert registered.wait(2)
        release.set()
        outputs = [f.result(3) for f in [first, *followers]]
    assert len(calls) == 1
    if outcome == "error":
        assert all(isinstance(out, PackInvokeError) for out in outputs)
        assert all(out is outputs[0] for out in outputs)
    else:
        assert len(outputs[0]["results"]) == (0 if outcome == "empty" else 1)
        assert all(len(out["results"]) == (0 if outcome == "empty" else 5)
                   for out in outputs[1:])


def test_singleflight_wait_timeout_never_starts_second_request(env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"results": []}
    def get(*a, **kw):
        calls.append(kw)
        entered.set()
        assert release.wait(3)
        return Resp()
    monkeypatch.setattr(websearch.httpx.Client, "get", get)
    monkeypatch.setattr(websearch, "_TIMEOUT_S", 0.01)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(websearch.search, env.state, "慢请求")
        assert entered.wait(2)
        follower = pool.submit(websearch.search, env.state, "慢请求")
        try:
            with pytest.raises(PackInvokeError) as exc:
                follower.result(0.5)
            assert exc.value.error_type == PackInvokeError.RETRYABLE
            assert len(calls) == 1
        finally:
            release.set()
        leader.result(2)


def test_cache_and_flight_keys_include_service_engine_language(env, monkeypatch):
    calls = _mock_get(env)
    websearch.search(env.state, "同词")
    monkeypatch.setattr(websearch, "settings_url", lambda s: "http://second.test")
    websearch.search(env.state, "同词")
    monkeypatch.setattr(websearch, "_ENGINE", "bing")
    websearch.search(env.state, "同词")
    monkeypatch.setattr(websearch, "_LANGUAGE", "en-US", raising=False)
    websearch.search(env.state, "同词")
    assert len(calls) == 4
    assert calls[-1]["params"]["language"] == "en-US"


def test_cached_results_do_not_bypass_configuration_validation(env, monkeypatch):
    _mock_get(env)
    websearch.search(env.state, "同词")
    monkeypatch.setattr(websearch, "settings_url", lambda s: "")
    with pytest.raises(PackInvokeError) as exc:
        websearch.search(env.state, "同词")
    assert exc.value.error_type == PackInvokeError.CREDENTIAL


def test_inflight_queries_for_different_services_are_independent(env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    both = threading.Barrier(2)
    calls = []
    monkeypatch.setattr(websearch, "settings_url", lambda state: state.url)
    class Resp:
        def __init__(self, url): self.url = url
        def raise_for_status(self): pass
        def json(self): return {"results": [{"title": "t", "url": self.url, "content": "c"}]}
    def get(client, url, **kw):
        calls.append(url)
        both.wait(timeout=2)
        return Resp(url)
    monkeypatch.setattr(websearch.httpx.Client, "get", get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda url: websearch.search(SimpleNamespace(url=url), "同词"),
                                ["http://first.test", "http://second.test"]))
    assert set(calls) == {"http://first.test/search", "http://second.test/search"}
    assert [out["results"][0]["url"] for out in outputs] == [
        "http://first.test/search", "http://second.test/search"]


def test_search_calls_authorization_before_network(env):
    calls = _mock_get(env)
    checks = []
    def authorize():
        checks.append(len(calls))
        raise PackInvokeError("disabled", PackInvokeError.PERMANENT)
    with pytest.raises(PackInvokeError):
        websearch.search(env.state, "被停用", before_request=authorize)
    assert checks == [0]
    assert calls == []


def test_empty_results_not_cached(env):  # noqa: F811
    """空结果不入缓存:瞬时抖动的空结果不放大成 10 分钟持续故障。"""
    calls = _mock_get(env, payload={"results": [], "unresponsive_engines": []})
    out1 = websearch.search(env.state, "零结果问题", top_k=5)
    assert out1["results"] == [] and out1["cached"] is False
    websearch.search(env.state, "零结果问题", top_k=5)
    # 第二次仍真出站(未缓存)
    assert len(calls) == 2


def test_engine_unresponsive_raises_retryable(env):  # noqa: F811
    """google 被 SearXNG 挂起(限流/验证):retryable,不当"没搜到"。"""
    _mock_get(env, payload={"results": [],
                            "unresponsive_engines": [["google", "CAPTCHA"]]})
    with pytest.raises(PackInvokeError) as ei:
        websearch.search(env.state, "限流问题")
    assert ei.value.error_type == PackInvokeError.RETRYABLE
