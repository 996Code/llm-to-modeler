"""Tool/CompositeTool ABC 测试 — Fail-Closed 默认值 + hooks。"""
import pytest
from sdk.tool import Tool, CompositeTool, ToolResult, ToolContext


class DummyTool(Tool):
    """测试用最小工具实现。"""
    name = "dummy"
    description = "测试工具"
    when = "测试时用"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(reply="ok", summary="dummy 执行完成")


class TestToolFailClosedDefaults:
    """Fail-Closed:安全相关属性默认保守。"""

    def test_is_destructive_defaults_true(self):
        t = DummyTool()
        assert t.is_destructive is True  # 默认破坏性

    def test_is_read_only_defaults_false(self):
        t = DummyTool()
        assert t.is_read_only is False

    def test_is_concurrency_safe_defaults_false(self):
        """C.2-B:默认不可并发(保守)。"""
        t = DummyTool()
        assert t.is_concurrency_safe is False


class TestToolHooks:
    """可选 hooks 有默认实现,pack 按需覆写。"""

    def test_validate_input_defaults_none(self):
        t = DummyTool()
        assert t.validate_input({}) is None  # 默认通过

    def test_requires_follow_up_defaults_false(self):
        t = DummyTool()
        assert t.requires_follow_up(ToolResult()) is False

    def test_summarize_artifact_defaults_empty(self):
        t = DummyTool()
        assert t.summarize_artifact({}) == ""

    def test_title_for_defaults_empty(self):
        t = DummyTool()
        assert t.title_for({}) == ""


class TestCompositeTool:
    """CompositeTool 提供 step 编排。"""

    def test_steps_defaults_empty(self):
        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx): return ToolResult()

        c = MyComposite()
        assert c.steps == []

    def test_run_pipeline_executes_steps_in_order(self):
        """steps 按序调用 _step_<name>。"""
        executed = []

        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            steps = ["alpha", "beta"]

            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                self.run_pipeline(state, ctx)
                return ToolResult(summary="done")

            def _step_alpha(self, state, ctx):
                executed.append("alpha")

            def _step_beta(self, state, ctx):
                executed.append("beta")

        c = MyComposite()
        ctx = _make_ctx()
        c.run_pipeline({}, ctx)
        assert executed == ["alpha", "beta"]

    def test_run_pipeline_emits_stage_per_step(self):
        """每个 step 自动 emit 一个 stage 事件(供 SSE 流式进度)。"""
        emitted = []

        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            steps = ["x", "y"]

            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx): return ToolResult()
            def _step_x(self, state, ctx): pass
            def _step_y(self, state, ctx): pass

        ctx = ToolContext(
            llm_client=None, asset_client=None, conversation=None,
            emit=lambda event_type, message, **kw: emitted.append((event_type, message)),
        )
        MyComposite().run_pipeline({}, ctx)
        assert emitted == [("stage", "x"), ("stage", "y")]

    def test_run_pipeline_short_circuits_on_clarification(self):
        """step 内抛 ClarificationRaised → run_pipeline 立即上抛,不执行后续 step。
        v4 §4.1 约定:step 可抛 ClarificationRaised 短路。"""
        from sdk.tool import ClarificationRaised

        executed = []

        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            steps = ["alpha", "beta"]

            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx): return ToolResult()
            def _step_alpha(self, state, ctx):
                executed.append("alpha")
                raise ClarificationRaised(questions=["需要哪些字段?"])
            def _step_beta(self, state, ctx):
                executed.append("beta")  # 不应执行

        with pytest.raises(ClarificationRaised) as exc_info:
            MyComposite().run_pipeline({}, _make_ctx())
        assert exc_info.value.questions == ["需要哪些字段?"]
        assert executed == ["alpha"]  # beta 没执行


def _make_ctx() -> ToolContext:
    """构造测试用 ToolContext。"""
    return ToolContext(
        llm_client=None,
        asset_client=None,
        conversation=None,
        emit=lambda *a, **k: None,
    )
