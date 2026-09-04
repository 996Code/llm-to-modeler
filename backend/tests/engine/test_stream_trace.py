"""stream_graph 请求初始化参数打点(request_context trace)测试。

验证每轮 chat 请求的初始化参数(services 表/掩蔽鉴权头/上下文规格)
进入会话事件流——事后在管理端链路视图按轮可追溯,也是
「services 缺失静默回退 env 地址」类问题的排障依据。
"""
import asyncio
from unittest.mock import MagicMock

import pytest

try:
    import langgraph  # noqa: F401 —— stream_graph 模块级依赖,缺失时全模块跳过
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False

from engine.stream import stream_graph

pytestmark = pytest.mark.skipif(
    not _HAS_LANGGRAPH, reason="langgraph package not installed"
)


class _FakeGraph:
    """最小图桩:不产 chunk、无中断状态,足够驱动 stream_graph 全流程。"""

    def stream(self, input_data, config):
        return iter([])

    def get_state(self, config):
        return None


def _drive(**kwargs):
    """跑一轮 stream_graph,返回记录 append_event 调用的 mock store。"""
    store = MagicMock()

    async def _consume():
        async for _ in stream_graph(
            graph=_FakeGraph(),
            user_input="创建一个请假表单",
            conversation_id="conv-1",
            user_id="qa",
            conversation_store=store,
            **kwargs,
        ):
            pass

    asyncio.run(_consume())
    return store


def _trace_payload(store):
    """从 mock store 的调用记录里取出 request_context 打点 payload。"""
    for args in store.append_event.call_args_list:
        if len(args.args) >= 3 and args.args[1] == "trace":
            payload = args.args[2]
            if payload.get("stage") == "request_context":
                return payload
    return None


def test_request_context_recorded_with_services():
    """嵌入形态:services 表原样入链,鉴权头只留键名 + 遮蔽值。"""
    store = _drive(
        services={"njmind-modeler": "http://192.168.99.22/codeBack"},
        forward_headers={"Authorization": "Bearer sk-super-secret-token",
                         "tenant-id": "1"},
        context_artifact={"formName": "请假表", "formFieldConfigVos": [{"a": 1}]},
    )
    payload = _trace_payload(store)
    assert payload is not None, "request_context 打点缺失"
    detail = payload["detail"]
    assert detail["services"] == {"njmind-modeler": "http://192.168.99.22/codeBack"}
    # 请求头原文记录（产品决策：内网审计需要完整凭证形态）
    assert detail["forward_headers"]["Authorization"] == "Bearer sk-super-secret-token"
    assert detail["forward_headers"]["tenant-id"] == "1"
    assert detail["context_artifact_bytes"] > 0
    assert detail["user_id"] == "qa"
    assert detail["resume_answers"] is False


def test_request_context_standalone_empty_services():
    """独立形态:services 为空也照样打点——空表正是「无上游地址可用」的证据。"""
    store = _drive(services=None, forward_headers=None, context_artifact=None)
    payload = _trace_payload(store)
    assert payload is not None
    detail = payload["detail"]
    assert detail["services"] == {}
    assert detail["forward_headers"] == {}
    assert detail["context_artifact_bytes"] == 0

