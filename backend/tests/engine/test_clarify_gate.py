"""I2 缺陷回归:追问挂起迁移独立节点(clarify_gate)——resume 只重放门节点。

背景(call_logs 实证):旧拓扑 interrupt 挂在 execute_tool 节点内部,
挂起时节点未 return,tool_state 的挂起态键(如 _permission_pending_artifact)
未提交进图状态。用户答复后 LangGraph 从节点头重放 execute_tool:
工具全管线重跑(真实多烧 parse+generate 两次 LLM),产物可能与用户
确认时不一致;重放到 interrupt 拿到答案后,节点内二次执行工具走守卫路径。

修复后拓扑:
  execute_tool 遇追问**正常 return**(提交 tool_state 挂起键 + pending_questions
  + tool_result=None)→ route_after_execute → clarify_gate(轻节点,只做
  interrupt)→ resume 只重放 clarify_gate → 注入 answers → 重跑 execute_tool
  走守卫路径(零 LLM)。

对外 SSE 事件序列不变(result/needsClarification/questions 结构、
pipeline_definition、stage 顺序——stream.py 零改动,前端零改动)。
"""
from unittest.mock import MagicMock

import pytest

try:
    import langgraph  # noqa: F401 —— build_graph 模块级依赖,缺失时全模块跳过
    from langgraph.types import Command
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False

from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext, ClarificationRaised

pytestmark = pytest.mark.skipif(
    not _HAS_LANGGRAPH, reason="langgraph package not installed"
)

CONV_ID = "conv-clarify-gate-test"
ROLE_ANSWER = {"__role:amount:canEditFieldRoles:0": "财务角色（FIN）"}
PENDING_ARTIFACT = {"formName": "确认时产物", "fields": 3}


class _ProbeTool(Tool):
    """追问挂起探针:复刻真实权限追问工具的两段语义。

    首轮 execute:烧 2 次 LLM(parse+generate)、emit 管线定义与进度、
    把产物存进挂起键 `_pending_artifact` 后抛 ClarificationRaised 请求追问。
    resume 再入(挂起键已提交):守卫路径——emit 一条应用进度、消费挂起
    产物、零 LLM 返回 artifact。答"再问一个"时模拟连环追问(挂起键保留,
    抛第二个问题)。
    """

    name = "probe_tool"
    description = "追问挂起探针"
    when = "测试"

    def __init__(self):
        self.execute_calls = 0

    def input_schema(self) -> dict:
        return {}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        self.execute_calls += 1
        pending = state.get("_pending_artifact")
        if pending is None:
            ctx.emit("pipeline_definition", {
                "tool": self.name,
                "steps": [{"key": "parse", "label": "解析"},
                          {"key": "generate", "label": "生成"}],
            })
            ctx.emit("stage", "parse", "解析中...")
            ctx.llm_client.chat_json([{"role": "user", "content": "parse"}])
            ctx.emit("stage", "generate", "生成中...")
            ctx.llm_client.chat_json([{"role": "user", "content": "generate"}])
            state["_pending_artifact"] = dict(PENDING_ARTIFACT)
            raise ClarificationRaised(["金额字段的可编辑角色是？"])
        answers = state.get("clarify_answers") or {}
        if answers.get("text") == "再问一个":
            # 连环追问:挂起键保留,请求第二个问题
            raise ClarificationRaised(["第二个问题：还需要配置什么？"])
        ctx.emit("stage", "apply_answer", "正在应用您的选择...")
        artifact = dict(pending)
        artifact["answeredWith"] = answers
        state.pop("_pending_artifact", None)
        return ToolResult(artifact=artifact, summary="已应用选择")


def _initial_input(user_input="做一个报销单"):
    """镜像 stream.py 的首问初始 state(键名/结构与生产一致)。"""
    return {
        "user_input": user_input,
        "conversation_history": [],
        "compressed_history": "",
        "conversation_id": CONV_ID,
        "forward_headers": {},
        "context_artifact": None,
        "pack_params": {},
        "packs": [],
        "tool_name": "",
        "intent_reason": "",
        "tool_state": {},
        "tool_result": None,
        "pending_questions": {},
        "clarify_answers": {},
        "sse_events": [],
    }


def _events(chunks):
    """按顺序抽出所有节点 chunk 里的 sse_events(stream.py 同款消费顺序)。"""
    out = []
    for chunk in chunks:
        for _node, update in chunk.items():
            if isinstance(update, dict):
                out.extend(update.get("sse_events") or [])
    return out


def _interrupt_payload(graph, config):
    """取挂起现场的 interrupt value(stream.py 据此推 needsClarification)。"""
    snap = graph.get_state(config)
    for task in snap.tasks:
        if task.interrupts:
            return task, task.interrupts[0].value
    raise AssertionError("挂起现场应有 interrupt 任务")


@pytest.fixture
def _restore_nodes():
    """snapshot/恢复 nodes 模块级依赖(build_graph 会 configure 注入)。"""
    from engine import nodes
    saved = {k: getattr(nodes, k) for k in (
        "_registry", "_pack_routers", "_pack_configs", "_llm_client",
        "_asset_client", "_conversation", "_prompt_loader",
    )}
    yield
    for k, v in saved.items():
        setattr(nodes, k, v)


@pytest.fixture
def graph_bundle(tmp_path, monkeypatch, _restore_nodes):
    """真实 build_graph(真 SqliteSaver checkpoint)+ 探针工具 + mock LLM。"""
    from engine.graph import build_graph

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ckpt-test.db"))
    tool = _ProbeTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = MagicMock()
    graph = build_graph(
        registry=registry, llm_client=llm, asset_client=MagicMock(),
        conversation=None, prompt_loader=None,
        pack_routers={}, pack_configs={},
    )
    yield graph, tool, llm
    graph._conn.close()


class TestResumeZeroLLM:
    """I2 核心:resume 只重放 clarify_gate,execute 恰一次、LLM 零调用。"""

    def test_suspend_commits_pending_keys_and_resume_zero_llm(self, graph_bundle):
        graph, tool, llm = graph_bundle
        config = {"configurable": {"thread_id": CONV_ID}}

        # ── 第一轮:跑到追问挂起 ──
        list(graph.stream(_initial_input(), config))
        assert tool.execute_calls == 1
        assert llm.chat_json.call_count == 2  # parse + generate(真实烧了)

        task, intr_value = _interrupt_payload(graph, config)

        # I2 核心①:挂起时 tool_state 的挂起键已提交进图状态
        # (旧拓扑:execute_tool 在 interrupt 处未 return,checkpoint 无此键)
        snap = graph.get_state(config)
        assert snap.values["tool_state"].get("_pending_artifact") == PENDING_ARTIFACT

        # I2 核心②:挂起任务必须是轻节点 clarify_gate,而非 execute_tool
        # (挂谁 resume 就重放谁——挂 execute_tool 意味着工具全管线重跑)
        assert task.name == "clarify_gate"

        # interrupt payload 结构:stream.py 据此推 needsClarification(前端契约)
        assert intr_value["questions"][0]["question"] == "金额字段的可编辑角色是？"
        assert "summary" in intr_value

        # ── 第二轮:用户答复,resume ──
        llm.chat_json.reset_mock()
        execute_before = tool.execute_calls
        leg2 = list(graph.stream(Command(resume=ROLE_ANSWER), config))

        # I2 核心③:工具 execute 恰好一次(旧拓扑:重放全跑 + 节点内重跑 = 2 次)
        assert tool.execute_calls - execute_before == 1
        # I2 核心④:重跑走守卫路径,零 LLM(旧拓扑:重放重烧 parse+generate = 2 次)
        assert llm.chat_json.call_count == 0

        # 守卫生效:产物是用户确认时的同一份挂起产物(无漂移)
        result_events = [e for e in _events(leg2) if e["type"] == "result"]
        assert result_events, "resume 后应有最终 result 事件"
        payload = result_events[0]["data"]
        assert payload["config"]["formName"] == "确认时产物"
        assert payload["config"]["answeredWith"] == ROLE_ANSWER

    def test_multi_ask_rounds_each_suspend_commits_state(self, graph_bundle):
        """连续两轮追问:每轮挂起都提交 tool_state,答案逐轮累积不丢。"""
        graph, tool, llm = graph_bundle
        config = {"configurable": {"thread_id": CONV_ID}}

        list(graph.stream(_initial_input(), config))
        task, _ = _interrupt_payload(graph, config)
        assert task.name == "clarify_gate"

        # 第一轮答"再问一个" → 工具再次请求追问(挂起键仍在,换新问题)
        leg2 = list(graph.stream(Command(resume={"text": "再问一个"}), config))
        task2, intr2 = _interrupt_payload(graph, config)
        assert task2.name == "clarify_gate"
        assert intr2["questions"], "第二轮追问应仍有问题"
        assert graph.get_state(config).values["tool_state"].get(
            "_pending_artifact") == PENDING_ARTIFACT

        # 第二轮正式答复 → 完成
        leg3 = list(graph.stream(Command(resume=ROLE_ANSWER), config))
        result_events = [e for e in _events(leg3) if e["type"] == "result"]
        assert result_events
        assert result_events[0]["data"]["config"]["answeredWith"] == ROLE_ANSWER


class TestSSESequenceSnapshot:
    """对外 SSE 事件序列快照:result/needsClarification/questions 结构、
    pipeline_definition、stage 顺序——stream.py 与前端零改动兼容。"""

    def test_event_sequence_unchanged(self, graph_bundle):
        graph, tool, llm = graph_bundle
        config = {"configurable": {"thread_id": CONV_ID}}

        leg1 = list(graph.stream(_initial_input(), config))
        _, intr_value = _interrupt_payload(graph, config)
        leg2 = list(graph.stream(Command(resume=ROLE_ANSWER), config))

        # 首轮事件序列:意图 stage → 管线定义 → 工具 stage ×2 → needsClarification
        seq1 = [(e["type"], e.get("stage") or e.get("data", {}).get("tool"))
                for e in _events(leg1)]
        assert seq1 == [
            ("stage", "classify_intent"),
            ("pipeline_definition", "probe_tool"),
            ("stage", "parse"),
            ("stage", "generate"),
        ]

        # needsClarification 事件由 stream.py 从 interrupt payload 构造:
        # {needsClarification, questions, summary, conversationId}——
        # 此处锁定 payload 结构(questions 元素含 question/header/options)。
        assert intr_value == {
            "questions": [{
                "question": "金额字段的可编辑角色是？",
                "header": "补充信息",
                "options": [{"label": "自行输入", "description": ""}],
                "multi_select": False,
            }],
            "summary": "我需要确认一些信息：\n1. 金额字段的可编辑角色是？",
        }

        # resume 轮事件序列:守卫应用 stage → result(结构:config/valid/...)
        seq2 = [(e["type"], e.get("stage") or e.get("data", {}).get("tool"))
                for e in _events(leg2)]
        assert seq2 == [("stage", "apply_answer"), ("result", None)]
        result_data = _events(leg2)[-1]["data"]
        assert result_data["config"]["formName"] == "确认时产物"
        assert result_data["valid"] is True


class TestClarifyGateUnits:
    """节点函数单元:execute_tool 遇追问正常 return;clarify_gate 注入答案。"""

    @pytest.fixture(autouse=True)
    def _setup(self, _restore_nodes):
        from engine import nodes
        self.nodes = nodes
        nodes.configure(
            registry=None, llm_client=MagicMock(), asset_client=MagicMock(),
            conversation=None, prompt_loader=None,
            pack_routers={}, pack_configs={},
        )

    def _registry_with(self, tool):
        registry = ToolRegistry()
        registry.register(tool)
        self.nodes.configure(
            registry=registry, llm_client=MagicMock(), asset_client=MagicMock(),
            conversation=None, prompt_loader=None,
            pack_routers={}, pack_configs={},
        )

    def test_execute_tool_ask_returns_without_interrupt(self):
        """execute_tool 遇追问正常 return:不再在节点内挂起(直接调用不抛
        GraphInterrupt),挂起键 + pending_questions 随返回提交。"""
        self._registry_with(_ProbeTool())
        state = {
            "tool_name": "probe_tool",
            "tool_state": {"user_input": "x", "conversation_id": "",
                           "forward_headers": {}},
            "conversation_id": "",
        }
        update = self.nodes.execute_tool_node(state)

        assert update["tool_result"] is None  # None 语义触发 rerun 路由
        assert update["tool_state"]["_pending_artifact"] == PENDING_ARTIFACT
        pending = update["pending_questions"]
        assert pending["questions"][0]["question"] == "金额字段的可编辑角色是？"
        assert "summary" in pending

    def test_clarify_gate_injects_answers_and_clears_pending(self, monkeypatch):
        """clarify_gate:interrupt 拿到答案 → 注入 tool_state、清 pending、
        tool_result=None 触发重跑(语义同旧 execute_tool 恢复段)。"""
        monkeypatch.setattr(self.nodes, "interrupt", lambda value: ROLE_ANSWER)
        state = {
            "tool_name": "probe_tool",
            "tool_state": {"user_input": "x", "_pending_artifact": dict(PENDING_ARTIFACT)},
            "pending_questions": {"questions": [{"question": "q"}], "summary": "s"},
            "clarify_answers": {},
            "tool_result": None,
        }
        update = self.nodes.clarify_gate_node(state)

        assert update["tool_state"]["clarify_answers"] == ROLE_ANSWER
        assert update["pending_questions"] == {}
        assert update["tool_result"] is None
        assert update["clarify_answers"] == {}  # 已注入 tool_state,避免重复

    def test_clarify_gate_normalizes_string_answer(self, monkeypatch):
        """非 dict 答复归一为 {"text": ...}(语义同旧恢复段)。"""
        monkeypatch.setattr(self.nodes, "interrupt", lambda value: "确认")
        state = {
            "tool_name": "t",
            "tool_state": {},
            "pending_questions": {"questions": [{"question": "q"}], "summary": "s"},
        }
        update = self.nodes.clarify_gate_node(state)
        assert update["tool_state"]["clarify_answers"] == {"text": "确认"}

    def test_route_after_execute(self):
        """条件边:pending_questions 非空 → clarify;否则 → handle_result。"""
        assert self.nodes.route_after_execute(
            {"pending_questions": {"questions": [{"question": "q"}]}}) == "clarify"
        assert self.nodes.route_after_execute(
            {"pending_questions": {}, "tool_result": {"summary": "ok"}}) == "result"
        assert self.nodes.route_after_execute({}) == "result"
