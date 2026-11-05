"""缓存健壮性补充:空结果不缓存 / 引擎挂起抛 retryable。"""
import pytest

from domains.web_search import websearch
from sdk.tool import PackInvokeError

from tests.domains.test_web_search_pack import _mock_get, env  # noqa: F401


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
