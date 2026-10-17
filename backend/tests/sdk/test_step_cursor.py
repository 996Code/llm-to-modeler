"""D9 步骤游标 —— run_pipeline 的 resume 跳步(保守增强)。

覆盖:游标推进(每步完成后+1)、resume 跳过声明幂等的已完成步、
未声明步照跑(守卫短路语义不变)、_resumed_steps 观测标记、
首跑(cursor 不存在)行为完全不变(回归锚)、中断(resume 挂起)
后游标保留、ask_data 的幂等声明生效。
"""
import pytest

from sdk.tool import CompositeTool, ToolContext, ToolResult

try:
    import langgraph  # noqa: F401
    _HAS = True
except ImportError:
    _HAS = False

pytestmark = pytest.mark.skipif(not _HAS, reason="langgraph 未安装")


def _ctx():
    return ToolContext(llm_client=None, asset_client=None, conversation=None,
                       emit=lambda *a, **k: None, conv_id="")


class _Pipelined(CompositeTool):
    """三步管线:记录调用序;step2 可主动挂起(_need_clarify)。"""

    name, description, when = "t", "t", "t"
    steps = ["one", "two", "three"]
    idempotent_steps = set()

    def __init__(self, hang_at=None):
        self.calls = []
        self.hang_at = hang_at

    def input_schema(self):
        return {}

    def execute(self, state, ctx):
        self.run_pipeline(state, ctx)
        return ToolResult(summary="done")

    def _step_one(self, state, ctx):
        self.calls.append("one")
        state["one_done"] = True

    def _step_two(self, state, ctx):
        if self.hang_at == "two":
            self.calls.append("two-hang")
            state["_need_clarify"] = True
            state["_result"] = ToolResult(summary="ask")
            return
        self.calls.append("two")

    def _step_three(self, state, ctx):
        if state.get("_need_clarify"):
            return
        self.calls.append("three")


class TestStepCursor:

    def test_first_run_cursor_advances(self):
        """首跑:游标从无到有逐步推进;全步执行。"""
        t = _Pipelined()
        t.execute({}, _ctx())
        assert t.calls == ["one", "two", "three"]

    def test_resume_skips_idempotent_declared(self):
        """resume:声明幂等的已完成步跳过,未声明的照跑。"""
        class _T(_Pipelined):
            idempotent_steps = {"one", "two"}

        t = _T(hang_at="two")
        state = {}
        t.execute(state, _ctx())  # 首跑到 two 挂起
        assert t.calls == ["one", "two-hang"]
        assert state["_step_cursor"] == 2          # 挂起前 two 已完成(推进)

        # resume:引擎注入答案后重跑(execute 同款清理逻辑)
        state.pop("_need_clarify"), state.pop("_result")
        t2 = _T()
        t2.calls = list(t.calls)
        t2.execute(state, _ctx())
        # one/two 游标跳过(幂等声明),three 照跑
        assert t2.calls == ["one", "two-hang", "three"]
        assert state["_resumed_steps"] == ["one", "two"]

    def test_resume_undeclared_steps_still_run(self):
        """未声明幂等的步:resume 照跑(守卫短路语义不变)。"""
        t = _Pipelined(hang_at="two")   # idempotent_steps 空
        state = {}
        t.execute(state, _ctx())
        assert state["_step_cursor"] == 2

        state.pop("_need_clarify"), state.pop("_result")
        t2 = _Pipelined()
        t2.calls = list(t.calls)
        t2.execute(state, _ctx())
        # 无声明:one/two/three 全部重跑(现状行为=守卫键决定)
        assert t2.calls == ["one", "two-hang", "one", "two", "three"]
        assert state["_resumed_steps"] == []

    def test_cursor_zero_means_no_skip(self):
        """游标 0(首跑无历史):resumed_steps 空,全步执行。"""
        t = _Pipelined()
        state = {}
        t.execute(state, _ctx())
        assert state["_resumed_steps"] == []
        assert state["_step_cursor"] == 3

    def test_ask_data_declares_llm_steps_idempotent(self):
        """ask_data 的幂等声明:think/generate_sql(纯 LLM 推理步)。"""
        from domains.chatbi.tools.ask_data import AskDataTool
        assert AskDataTool.idempotent_steps == {"think", "generate_sql"}
        assert "resolve_datasource" not in AskDataTool.idempotent_steps
        assert "execute_sql" not in AskDataTool.idempotent_steps
