"""Milvus 连接自愈(10-15 真实事故根治)——ConnectionNotExist 时重建重试一次。

事故复现:uvicorn --reload 的 unload 钩子 close 了缓存的 store 连接,
在途导入报 should create connection first 而降级——不自愈只能重启进程。
"""
from unittest.mock import MagicMock

import pytest

from sdk.vector_store import MilvusVectorStore


class _FakeConnErr(Exception):
    """模拟 pymilvus ConnectionNotExistException(名字/消息双口径都命中)。"""


def _make_store():
    calls = {"n": 0}

    def fake_method(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeConnErr("(code=1, message=should create connection first.)")
        return "ok"

    store = object.__new__(MilvusVectorStore)
    store._uri = "http://x"
    store._user = ""
    store._password = ""
    store._prefix = "kg"
    store._client = MagicMock()
    store._client.some_op = fake_method
    store._connect = lambda: setattr(store, "_client", MagicMock())
    return store, calls


class TestConnectionSelfHeal:

    def test_reconnects_on_connection_not_exist(self):
        """连接失效 → 重建客户端重试一次成功。"""
        store, calls = _make_store()
        out = store._with_reconnect(store._client.some_op)
        assert out == "ok" and calls["n"] == 2

    def test_other_errors_not_retried(self):
        """非连接类异常原样上抛(只重试连接失效,不吞业务错误)。"""
        store, _ = _make_store()
        def boom():
            raise ValueError("业务错误")
        store._client.some_op = boom
        with pytest.raises(ValueError, match="业务错误"):
            store._with_reconnect(store._client.some_op)

    def test_second_connection_failure_raises(self):
        """重建后仍连接失败 → 上抛(一次自愈,不无限重试)。"""
        store, calls = _make_store()
        def always_conn_err(*a, **k):
            calls["n"] += 1
            raise _FakeConnErr("should create connection first")
        store._client.some_op = always_conn_err
        with pytest.raises(_FakeConnErr):
            store._with_reconnect(store._client.some_op)
        assert calls["n"] == 2   # 原次 + 重试一次,无第三次
