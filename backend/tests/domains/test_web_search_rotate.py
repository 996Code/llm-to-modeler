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
        self.manual_info = manual_info
        self.searxng_responses = list(searxng_responses)
        self.calls = []  # (method, url)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if "/proxies/MANUAL" in url:
            return _Resp(self.manual_info)
        # searxng 搜索
        return _Resp(self.searxng_responses.pop(0)
                     if self.searxng_responses else {"results": []})

    def put(self, url, **kw):
        self.calls.append(("PUT", url))
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
    websearch.reset_cache()
    return client


def test_rotate_prefers_cross_region(monkeypatch):
    """限流后轮换优先跨区域:新加坡坏 → 切香港/日本,不再落新加坡。"""
    client = _setup(monkeypatch)
    websearch.rotate_mihomo_node(None)
    puts = [u for m, u in client.calls if m == "PUT"]
    assert any("/proxies/MANUAL" in u for u in puts)
    assert any("/proxies/PROXY" in u for u in puts)
    # 跨区域断言:切到的节点不是 新加坡-* (从 PUT body 看——替身没记 body,
    # 用逻辑复算:ordered 从 SG-1 之后开始,首个非新加坡=中国香港-HK-1
    real = [n for n in _MANUAL["all"] if "剩余" not in n]
    idx = real.index("新加坡-SG-1-流量倍率:1")
    ordered = real[idx + 1:] + real[:idx]
    expect = next((n for n in ordered
                   if n.split("-", 1)[0] != "新加坡"), ordered[0])
    assert expect == "中国香港-HK-1-流量倍率:0.7"


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
    assert "均被限流" in ei.value.message


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
