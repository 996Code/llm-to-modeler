"""emit 分发契约测试 —— 固化按事件类型标签分发的语义。

背景事故:nodes.py 的 emit 曾按参数个数(len(args)>=3)判定"有无 message",
kwargs 传的 message 被静默丢弃——KG 检索进度文案在前端只剩步骤名。
本测试用全部合法调用形态逐一断言分发结果,防止回归。

通道语义:实时 emitter 在线时只走实时通道(列表不重复入,互斥);
不在线时入列表(节点结束时 flush)。测试分别验证两种通道。
"""
import pytest

from engine import nodes
from engine.nodes import dispatch_emit


@pytest.fixture()
def captured():
    """拦截 emit 的两条通道,返回 (实时通道捕获, 列表通道捕获)。"""
    rt_calls = []
    list_events = []
    nodes._realtime_emitter.fn = lambda kind, payload, message: rt_calls.append(
        (kind, payload, message))
    yield rt_calls, list_events
    nodes._realtime_emitter.fn = None


def test_stage_three_positional(captured):
    """表单系风格:emit("stage", name, message) —— 3 个位置参数。"""
    rt, lst = captured
    dispatch_emit(lst, "stage", "fetch_guide", "正在获取配置指南...")
    assert rt == [("stage", "fetch_guide", "正在获取配置指南...")]
    assert lst == []  # 实时通道在线,列表不重复入(互斥)


def test_stage_message_kwarg(captured):
    """KG 系风格:emit("stage", name, message=...) —— 关键字传 message。

    这就是事故形态:旧实现按 len(args)>=3 分发,此形态 message 被丢弃。
    """
    rt, lst = captured
    dispatch_emit(lst, "stage", "kb_search.graph", message="图谱实体匹配(3 个种子词)…")
    assert rt == [("stage", "kb_search.graph", "图谱实体匹配(3 个种子词)…")]


def test_stage_two_args_empty_message(captured):
    """两参形态:emit("stage", "generate_done") —— message 默认空串。"""
    rt, lst = captured
    dispatch_emit(lst, "stage", "generate_done")
    assert rt == [("stage", "generate_done", "")]


def test_pipeline_definition(captured):
    """管线定义:emit("pipeline_definition", {...})。"""
    rt, lst = captured
    payload = {"tool": "kb_search", "steps": [{"key": "s1", "label": "步骤一"}]}
    dispatch_emit(lst, "pipeline_definition", payload)
    assert rt == [("pipeline_definition", payload, None)]
    assert lst == []


def test_unknown_event_falls_back_to_stage(captured):
    """防御:未知事件类型按 stage 兜底(stage=事件名),不崩。"""
    rt, lst = captured
    dispatch_emit(lst, "weird_event", "some message")
    assert rt[-1][0] == "stage"
    assert rt[-1][1] == "weird_event"


def test_list_channel_without_realtime(captured):
    """列表通道兜底:无实时 emitter 时事件入 sse_events 列表。"""
    rt, lst = captured
    nodes._realtime_emitter.fn = None  # 显式关闭实时通道
    dispatch_emit(lst, "stage", "s", "m")
    dispatch_emit(lst, "pipeline_definition", {"tool": "t", "steps": []})
    assert rt == []  # 实时通道未触发
    assert lst == [
        {"type": "stage", "stage": "s", "message": "m"},
        {"type": "pipeline_definition", "data": {"tool": "t", "steps": []}},
    ]


def test_kb_search_real_call_uses_positional(captured):
    """端到端形态检查:kb_search 工具的 emit 全部走位置参数(契约对齐)。"""
    import os
    import tempfile
    from types import SimpleNamespace

    tmp = tempfile.mkdtemp()
    os.environ.setdefault("PACK_STATE_PATH", os.path.join(tmp, "p.json"))
    os.environ.setdefault("ADMIN_TOKEN", "")
    os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    os.environ.setdefault("LLM_API_KEY", "x")

    from domains.knowledge_graph.tools.kb_search import KbSearchTool

    stage_calls = []

    class Ctx:
        conv_id = "t"
        session_state = None

        def emit(self, *args, **kwargs):
            if args and args[0] == "stage":
                stage_calls.append((args, kwargs))

        def trace(self, *a, **k):
            pass

    KbSearchTool(app_state=SimpleNamespace()).execute({"user_input": "测试"}, Ctx())
    assert stage_calls, "kb_search 应发出 stage 事件"
    for args, kwargs in stage_calls:
        assert len(args) >= 3, f"message 应为位置参数: args={args} kwargs={kwargs}"
    # 工具的 emit 参数经 dispatch_emit 后 message 不丢(关闭实时走列表断言)
    nodes._realtime_emitter.fn = None
    lst2 = []
    dispatch_emit(lst2, *stage_calls[0][0], **stage_calls[0][1])
    assert lst2[0]["message"] == "正在解析问题与选定知识库…"
