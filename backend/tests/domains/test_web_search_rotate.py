"""mihomo 节点自动切换单测——限流检测/跨区域轮换/controller 交互。"""
from types import SimpleNamespace

import pytest

from domains.web_search import websearch
from sdk.tool import PackInvokeError


class _Resp:
    def __init__(self, payload=None, status=200):
        self._p = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


class _Client:
    """httpx 替身:按 URL 前缀分发(controller / searxng),记录调用。"""

    def __init__(self, manual_info, searxng_responses):
        self.manual_info = dict(manual_info)
        self.searxng_responses = list(searxng_responses)
        self.calls = []  # (method, url)
        self.puts = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if "/proxies/MANUAL" in url:
            return _Resp(self.manual_info)
        # searxng 搜索
        return _Resp(self.searxng_responses.pop(0)
                     if self.searxng_responses else {"results": []})

    def put(self, url, **kw):
        self.calls.append(("PUT", url))
        self.puts.append((url, kw["json"]))
        if url.endswith("/MANUAL"):
            self.manual_info["now"] = kw["json"]["name"]
        return _Resp({}, status=204)


_MANUAL = {"now": "新加坡-SG-1-流量倍率:1", "all": [
    "有效期2028-11-21, 剩余 91.73 GB",   # 假节点(应被过滤)
    "荷兰-NL-4-流量倍率:0.1",
    "新加坡-SG-1-流量倍率:1",
    "新加坡-SG-3-1-流量倍率:1",
    "中国香港-HK-1-流量倍率:0.7",
    "日本-TY-1-流量倍率:1.2",
]}

_SUSPENDED = {"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}
_OK = {"results": [{"title": "检定规程", "url": "https://a.test/x",
                    "content": "摘要"}]}


def _setup(monkeypatch, manual=_MANUAL, responses=None, api="http://mihomo.test:9090"):
    responses = responses if responses is not None else [_SUSPENDED, _OK]
    client = _Client(manual, responses)
    monkeypatch.setattr(websearch, "_get_client", lambda: client)
    monkeypatch.setattr(websearch, "_mihomo_api", lambda s: api)
    monkeypatch.setattr(websearch, "settings_url", lambda s: "http://searxng.test")
    monkeypatch.setattr(websearch.time, "sleep", lambda seconds: None)
    websearch.reset_cache()
    return client


def test_rotate_prefers_cross_region(monkeypatch):
    """限流后轮换优先跨区域:新加坡坏 → 切香港/日本,不再落新加坡。"""
    client = _setup(monkeypatch)
    websearch.rotate_mihomo_node(None)
    puts = [u for m, u in client.calls if m == "PUT"]
    assert any("/proxies/MANUAL" in u for u in puts)
    assert any("/proxies/PROXY" in u for u in puts)
    assert client.puts[0][1] == {"name": "中国香港-HK-1-流量倍率:0.7"}


def test_search_auto_rotates_and_recovers(monkeypatch):
    """限流 → 切节点 → 重试成功(用户视角一次成功,无感知)。"""
    client = _setup(monkeypatch, responses=[_SUSPENDED, _SUSPENDED, _OK])
    out = websearch.search(None, "JJG 52")
    assert len(out["results"]) == 1
    assert any(m == "PUT" for m, _ in client.calls)  # 发生过切换


def test_search_raises_when_all_rotations_fail(monkeypatch):
    """切满仍限流:PERMANENT(内部已带切换重试 4 次,引擎层再重跑无意义)。"""
    _setup(monkeypatch, responses=[_SUSPENDED] * 5)
    with pytest.raises(PackInvokeError) as ei:
        websearch.search(None, "JJG 52")
    assert ei.value.error_type == PackInvokeError.PERMANENT
    assert "限流" in ei.value.message
    assert "所有" not in ei.value.message and "全部" not in ei.value.message
    assert "均被" not in ei.value.message


def test_search_no_rotation_configured_still_retryable(monkeypatch):
    """未配置 controller:单次限流抛 RETRYABLE(引擎重试有意义)。"""
    _setup(monkeypatch, responses=[_SUSPENDED], api="")
    with pytest.raises(PackInvokeError) as ei:
        websearch.search(None, "JJG 52")
    assert ei.value.error_type == PackInvokeError.RETRYABLE


def test_no_api_configured_no_rotation(monkeypatch):
    """未配置 mihomo_api_url:不切节点,限流直接如实抛。"""
    client = _setup(monkeypatch, responses=[_SUSPENDED], api="")
    with pytest.raises(PackInvokeError):
        websearch.search(None, "q")
    assert not any(m == "PUT" for m, _ in client.calls)


def test_rotate_skips_fake_nodes(monkeypatch):
    """假节点(有效期/剩余流量)永不入选。"""
    client = _setup(monkeypatch)
    # 从头轮换:当前 SG-1,候选不应包含假节点——由 test_rotate_prefers_cross_region
    # 的 expect 断言覆盖;这里补边界:now 不在 real 里 → 不切
    manual = {"now": "不存在的节点", "all": _MANUAL["all"]}
    monkeypatch.setattr(websearch, "_get_client",
                        lambda: _Client(manual, []))
    assert websearch.rotate_mihomo_node(None) is None


def test_rotation_never_revisits_attempted_nodes(monkeypatch):
    client = _setup(monkeypatch, manual={"now": "A-1", "all": ["A-1", "B-1", "A-2"]},
                    responses=[_SUSPENDED] * 5)
    with pytest.raises(PackInvokeError):
        websearch.search(None, "不循环")
    nodes = [body["name"] for url, body in client.puts if url.endswith("/MANUAL")]
    assert nodes == ["B-1", "A-2"]
    assert len([url for method, url in client.calls if url.endswith("/search")]) == 3


@pytest.mark.parametrize("reason", ["timeout", "TimeoutException", "parsing error", "SearxEngineResponseException"])
def test_non_rate_limit_engine_failure_does_not_rotate(monkeypatch, reason):
    client = _setup(monkeypatch, responses=[
        {"results": [], "unresponsive_engines": [["google", reason]]}])
    with pytest.raises(PackInvokeError) as exc:
        websearch.search(None, "故障分类")
    assert exc.value.error_type == PackInvokeError.RETRYABLE
    assert "限流" not in exc.value.message
    assert client.puts == []


@pytest.mark.parametrize("reason", ["CAPTCHA", "HTTP 429", "access denied", "SearxEngineTooManyRequests"])
def test_rate_limit_reasons_rotate(monkeypatch, reason):
    client = _setup(monkeypatch, responses=[
        {"results": [], "unresponsive_engines": [["google", reason]]}, _OK])
    assert websearch.search(None, "限流分类")["results"]
    assert len(client.puts) == 2


def test_rotation_checks_authorization_before_every_network_call(monkeypatch):
    client = _setup(monkeypatch)
    checks = []
    def authorize():
        checks.append(len(client.calls))
    websearch.search(None, "授权逐跳检查", before_request=authorize)
    assert checks == list(range(len(client.calls)))


def test_rotation_does_not_swallow_authorization_rejection(monkeypatch):
    client = _setup(monkeypatch)
    def authorize():
        raise PackInvokeError("disabled", PackInvokeError.PERMANENT)
    with pytest.raises(PackInvokeError, match="disabled"):
        websearch.rotate_mihomo_node(None, before_request=authorize)
    assert client.calls == []


def test_controller_switches_are_serialized(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    client = _setup(monkeypatch)
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original_get = client.get
    def blocking_get(*args, **kwargs):
        response = original_get(*args, **kwargs)
        entered.set()
        assert release.wait(3)
        return response
    client.get = blocking_get
    def second():
        started.set()
        return websearch.rotate_mihomo_node(None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(websearch.rotate_mihomo_node, None)
        assert entered.wait(2)
        two = pool.submit(second)
        assert started.wait(2)
        threading.Event().wait(0.05)
        count = len(client.calls)
        release.set()
        assert one.result(2)
        assert two.result(2)
    assert count == 1
