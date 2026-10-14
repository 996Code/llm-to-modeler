"""P0#4 平台 rerank 接口测试 —— Cohere/Jina 协议、env 配置、观测、降级语义。"""
import json
from types import SimpleNamespace

import pytest

from llm.client import LLMClient


class _FakeStore:
    """收集 save_call_log 调用(观测断言用)。"""

    def __init__(self):
        self.calls = []

    def save_call_log(self, **kw):
        self.calls.append(kw)
        return "log-id"


def _make_client():
    return LLMClient(config=None, conversation_store=_FakeStore())


class TestRerank:

    def test_unconfigured_raises(self, monkeypatch):
        """未配置 RERANK_BASE_URL/MODEL → RuntimeError(调用方降级的触发条件);
        降级也落 call_logs(终审:开关开但平台未配置时,管理端须能看到
        '为何每次都降级'而不是只有服务端 warning)。"""
        for k in ("RERANK_BASE_URL", "RERANK_MODEL"):
            monkeypatch.delenv(k, raising=False)
        client = _make_client()
        with pytest.raises(RuntimeError, match="RERANK"):
            client.rerank("q", ["a", "b"])
        log = client._conversation_store.calls[-1]
        assert "未配置" in log["error_message"]
        assert log["status_code"] == 500

    def test_returns_desc_indices(self, monkeypatch):
        """Cohere/Jina 协议:POST {model,query,documents,top_n}
        → results[{index,relevance_score}] 按分数降序——返回原文索引降序列。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://rerank.test/v1")
        monkeypatch.setenv("RERANK_MODEL", "bge-reranker")
        monkeypatch.setenv("RERANK_API_KEY", "sk-test")
        client = _make_client()

        class FakeResp:
            status_code = 200
            text = json.dumps({"results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.4},
                {"index": 2, "relevance_score": 0.1},
            ]})

        monkeypatch.setattr("httpx.Client.post",
                            lambda self, url, **kw: FakeResp())
        assert client.rerank("q", ["a", "b", "c"]) == [1, 0, 2]

    def test_request_shape_and_auth(self, monkeypatch):
        """请求体 {model,query,documents,top_n=len} + Bearer 头 + POST {base}/rerank。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://rerank.test/v1")
        monkeypatch.setenv("RERANK_MODEL", "bge-reranker")
        monkeypatch.setenv("RERANK_API_KEY", "sk-test")
        client = _make_client()
        captured = {}

        class FakeResp:
            status_code = 200
            text = json.dumps({"results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.2}]})

        def fake_post(self, url, json=None, headers=None, **kw):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

        monkeypatch.setattr("httpx.Client.post", fake_post)
        client.rerank("问题", ["t1", "t2"], stage="kg.rerank")
        assert captured["url"] == "http://rerank.test/v1/rerank"
        assert captured["json"] == {"model": "bge-reranker", "query": "问题",
                                    "documents": ["t1", "t2"], "top_n": 2}
        assert captured["headers"]["Authorization"] == "Bearer sk-test"

    def test_timeout_default_3s(self, monkeypatch):
        """默认 3s 超时(蓝图:排序增强不值得长等);RERANK_TIMEOUT 可调。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://x/v1")
        monkeypatch.setenv("RERANK_MODEL", "m")
        captured = {}

        class FakeResp:
            status_code = 200
            text = json.dumps({"results": [{"index": 0, "relevance_score": 0.5}]})

        import httpx as _hx
        real_init = _hx.Client.__init__

        def spy_init(self, *a, **kw):
            captured.update(kw)
            real_init(self, *a, **kw)

        monkeypatch.setattr(_hx.Client, "__init__", spy_init)
        monkeypatch.setattr(_hx.Client, "post",
                            lambda self, url, **kw: FakeResp())
        _make_client().rerank("q", ["a"])
        assert captured.get("timeout") == 3.0

    def test_observation_logged(self, monkeypatch):
        """成功/失败都入 call_logs(endpoint=rerank:{model},stage 并入
        request_data,失败带 error)——与 embeddings 同款纪律。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://x/v1")
        monkeypatch.setenv("RERANK_MODEL", "m")
        client = _make_client()

        class FakeResp:
            status_code = 200
            text = json.dumps({"results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.2}]})

        monkeypatch.setattr("httpx.Client.post",
                            lambda self, url, **kw: FakeResp())
        client.rerank("q", ["a", "b"], conv_id="c1", stage="kg.rerank")
        log = client._conversation_store.calls[-1]
        assert log["endpoint"] == "rerank:m"
        assert log["request_data"]["stage"] == "kg.rerank"
        assert log["conv_id"] == "c1"
        assert log["response_data"]["topScore"] == 0.5
        # 失败路径
        def boom(self, url, **kw):
            raise RuntimeError("conn refused")
        monkeypatch.setattr("httpx.Client.post", boom)
        with pytest.raises(RuntimeError):
            client.rerank("q", ["a"], stage="kg.rerank")
        log2 = client._conversation_store.calls[-1]
        assert log2["error_message"] == "conn refused"
        assert log2["status_code"] == 500

    def test_malformed_results_raises(self, monkeypatch):
        """results 缺 index/数量不符 → 抛异常(调用方降级),不静默给错序。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://x/v1")
        monkeypatch.setenv("RERANK_MODEL", "m")
        client = _make_client()

        class BadResp:
            status_code = 200
            text = json.dumps({"results": [{"index": 5, "relevance_score": 0.9}]})

        monkeypatch.setattr("httpx.Client.post",
                            lambda self, url, **kw: BadResp())
        with pytest.raises(ValueError):
            client.rerank("q", ["a", "b"])

    def test_non_200_raises_with_error_logged(self, monkeypatch):
        """非 200 → RuntimeError(带状态码与响应片段)且 error 入 call_logs
        ——蓝图验收"断掉端点后日志有 degraded 记录"的观测前提。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://x/v1")
        monkeypatch.setenv("RERANK_MODEL", "m")
        client = _make_client()

        class ErrResp:
            status_code = 503
            text = "service unavailable"

        monkeypatch.setattr("httpx.Client.post",
                            lambda self, url, **kw: ErrResp())
        with pytest.raises(RuntimeError, match="503"):
            client.rerank("q", ["a"], stage="kg.rerank")
        log = client._conversation_store.calls[-1]
        assert log["status_code"] == 500
        assert "503" in log["error_message"]

    def test_close_closes_rerank_client(self, monkeypatch):
        """close() 收口 rerank httpx client——与 OpenAI client 同款生命周期
        (审查 major:不关则 socket 等 GC)。"""
        monkeypatch.setenv("RERANK_BASE_URL", "http://x/v1")
        monkeypatch.setenv("RERANK_MODEL", "m")
        client = _make_client()

        class OkResp:
            status_code = 200
            text = json.dumps({"results": [{"index": 0, "relevance_score": 0.5}]})

        closed = []
        monkeypatch.setattr("httpx.Client.post", lambda self, url, **kw: OkResp())
        monkeypatch.setattr("httpx.Client.close",
                            lambda self: closed.append(1))
        client.rerank("q", ["a"])
        client.close()
        assert closed, "close() 必须关闭 _rerank_client"
