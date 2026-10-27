"""stream_graph 追问恢复守卫测试(answers 无挂起可恢复 → 明确 error)。

背景(实测):追问被消费后 checkpoint 已是终态,再次带 answers 的请求
Command(resume) 在 LangGraph 里直接空转结束——不重放任何节点、不产
result 事件,前端旧追问卡重复提交只收到 done(静默失败)。

守卫(stream.py):answers 请求 + 无 interrupt + 无结果 → 推一条明确
error 事件("该追问已被处理或已失效"),与插件侧 flow 版本门同语义,
本层兜底并发窗口外的旧卡重放。
"""
import asyncio
import json
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


class _TerminalGraph:
    """终态图桩:stream 不产 chunk(空转),get_state 无挂起 task。

    与真实"追问已消费"的 checkpoint 行为一致——Command(resume) 进来
    图直接结束,无节点重放。
    """

    def stream(self, input_data, config):
        return iter([])

    def get_state(self, config):
        return None


def _collect_events(**kwargs):
    """跑一轮 stream_graph,返回 (SSE 事件列表, mock store)。"""
    store = MagicMock()
    events = []

    async def _consume():
        async for raw in stream_graph(
            graph=_TerminalGraph(),
            user_input="BJ2026",
            conversation_id="conv-stale",
            user_id="qa",
            conversation_store=store,
            **kwargs,
        ):
            for line in raw.split("\n"):
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except (ValueError, TypeError):
                        pass

    asyncio.run(_consume())
    return events, store


def test_stale_answer_gets_explicit_error():
    """旧卡重复提交:无挂起可恢复 → error 事件明确告知,不再静默 done。"""
    events, _ = _collect_events(answers={"委托单号": "BJ2026"})
    errors = [e for e in events
              if isinstance(e, dict) and e.get("error")
              and "已被处理" in str(e.get("error"))]
    assert errors, f"应推'追问已被处理'明确错误,实得事件: {events}"
    # done 仍在(前端关连接语义不变)
    assert any(e == {"status": "done"} for e in events)


def test_normal_message_without_answers_not_guarded():
    """普通消息(无 answers)空转不触发守卫——错误只属于追问恢复路径。"""
    events, _ = _collect_events()
    errors = [e for e in events
              if isinstance(e, dict) and e.get("error")
              and "已被处理" in str(e.get("error"))]
    assert not errors


def test_stale_answer_notice_saved_to_history():
    """守卫文案落会话历史(历史重载可见,不凭空断连)。"""
    _, store = _collect_events(answers={"委托单号": "BJ2026"})
    saved = [c for c in store.add_message.call_args_list
             if c.kwargs.get("role") == "assistant"]
    assert any("已被处理" in str(c.kwargs.get("content"))
               for c in saved), "守卫提示应作为 assistant 消息落库"
