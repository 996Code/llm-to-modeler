"""B2/B3 审计修复的回归测试：fallback 声明化 + 提交确认门槛 + ToolResult 显式字段。

对应审查发现的修复：
- fallback 三级优先级（声明 fallback_tool → fallback pack 首个工具 → 全局首个）；
- leave 提交确认门槛三态（首次挂起 / 确认放行 / 取消短路）；
- ToolResult.valid/validation_errors 显式字段的引擎消费语义。
"""
from unittest.mock import MagicMock

import pytest

from sdk.tool import Tool, ToolResult, ToolContext, ClarificationRaised
from domains.leave_application.tools.submit_leave import SubmitLeaveTool


def _stub_tool(name):
    t = MagicMock(spec=Tool)
    t.name = name
    return t


def _ctx(asset=None):
    return ToolContext(llm_client=None, asset_client=asset,
                       conversation=None, emit=lambda *a, **k: None)


class TestFallbackPriority:
    """_get_fallback_tool_name 三级优先级（声明驱动）。"""

    @pytest.fixture(autouse=True)
    def _setup_nodes(self):
        from engine import nodes
        saved = (nodes._registry, nodes._pack_routers, nodes._pack_configs)
        yield nodes
        nodes.configure(registry=saved[0], llm_client=None, asset_client=None,
                        conversation=None, prompt_loader=None,
                        pack_routers=saved[1], pack_configs=saved[2])

    def _configure(self, nodes, tools, configs, routers=None):
        registry = MagicMock()
        registry.all.return_value = tools
        registry.get = lambda n: next((t for t in tools if t.name == n), None)
        nodes.configure(registry=registry, llm_client=None, asset_client=None,
                        conversation=None, prompt_loader=None,
                        pack_routers=routers or {}, pack_configs=configs)

    def test_priority1_declared_fallback_tool(self, _setup_nodes):
        """声明了 domain.fallback_tool 且工具存在 → 直接用声明值。"""
        nodes = _setup_nodes
        self._configure(nodes, [_stub_tool("chat"), _stub_tool("other")],
                        {"p1": {"domain": {"fallback": True, "fallback_tool": "chat"}}})
        assert nodes._get_fallback_tool_name() == "chat"

    def test_priority1_skips_missing_tool(self, _setup_nodes):
        """声明的工具不在注册表 → 跳过该声明（不误用）。"""
        nodes = _setup_nodes
        self._configure(nodes, [_stub_tool("real_a")],
                        {"p1": {"domain": {"fallback": True, "fallback_tool": "ghost"}}})
        # 声明的 ghost 不存在 → 落到优先级 3（全局首个）
        assert nodes._get_fallback_tool_name() == "real_a"

    def test_priority2_fallback_pack_first_tool(self, _setup_nodes):
        """未声明 fallback_tool 但声明 fallback=true → 该 pack 首个工具。"""
        nodes = _setup_nodes
        router = MagicMock()
        router._registry.all.return_value = [_stub_tool("pack_b_tool")]
        self._configure(
            nodes,
            [_stub_tool("pack_a_tool"), _stub_tool("pack_b_tool")],
            {"pA": {"domain": {}}, "pB": {"domain": {"fallback": True}}},
            routers={"pB": router},
        )
        assert nodes._get_fallback_tool_name() == "pack_b_tool"

    def test_priority3_global_first(self, _setup_nodes):
        """无任何声明 → 全局第一个工具。"""
        nodes = _setup_nodes
        self._configure(nodes, [_stub_tool("first"), _stub_tool("second")], {})
        assert nodes._get_fallback_tool_name() == "first"


class TestLeaveConfirmGate:
    """提交确认门槛三态：首次挂起 / 确认放行 / 取消短路。"""

    def _tool_state(self, complete_data=True, answers=None):
        tool_state = {
            "user_input": "帮我请假 3 天" if complete_data else "帮我请假",
            "clarify_answers": answers or {},
        }
        return tool_state

    def test_first_run_raises_confirmation(self):
        """字段齐全首次执行 → confirm 步挂起追问（不再直写上游）。"""
        tool = SubmitLeaveTool()
        # parse_info 走 LLM——mock 掉：直接喂完整数据跳过 parse
        state = self._tool_state()
        state["leave_data"] = {"applicant": "张三", "leaveType": "事假",
                               "startDate": "2026-09-03", "endDate": "2026-09-05"}
        with pytest.raises(ClarificationRaised) as ei:
            tool._step_confirm(state, _ctx())
        assert "确认提交" in ei.value.questions[0]

    def test_confirm_answer_proceeds(self):
        """回答"确认"（挂起者=confirm）→ 放行（不抛、不置取消标记）。"""
        tool = SubmitLeaveTool()
        state = self._tool_state(answers={"text": "确认"})
        state["_awaiting"] = "confirm"
        tool._step_confirm(state, _ctx())
        assert not state.get("_cancelled")

    def test_cancel_answer_short_circuits(self):
        """回答"取消"（挂起者=confirm）→ 短路标记置位。"""
        tool = SubmitLeaveTool()
        state = self._tool_state(answers={"text": "取消"})
        state["_awaiting"] = "confirm"
        tool._step_confirm(state, _ctx())
        assert state["_cancelled"] is True
        assert state["_need_clarify"] is True


class TestToolResultValidationFields:
    """ToolResult 显式校验字段（引擎 is_valid 消费语义）。"""

    def test_defaults_are_none(self):
        r = ToolResult(artifact={"a": 1})
        assert r.valid is None
        assert r.validation_errors is None

    def test_explicit_invalid(self):
        r = ToolResult(artifact={"a": 1}, valid=False,
                       validation_errors=[{"message": "x"}])
        assert r.valid is False and len(r.validation_errors) == 1


class TestCompactFocusWiring:
    """compact_focus 接线：assemble_packs 聚合 manifest 声明并刷新 compressor。"""

    def test_assemble_refreshes_focus(self, monkeypatch):
        """装配后 compressor.compact_focus = 启用 pack 声明的聚合（分号拼接）。"""
        from unittest.mock import patch
        import services.pack_manager as pm

        app_state = MagicMock()
        compressor = MagicMock()
        app_state.compressor = compressor
        app_state.pack_configs = {}  # 装配后会被真实 manifest 覆盖

        with patch("domains.load_all_packs") as lap, \
             patch("domains.load_pack_configs") as lpc, \
             patch("engine.nodes.configure"):
            lap.return_value = (MagicMock(), None, {}, {})
            # njmind_form 声明了 compact_focus
            lpc.return_value = {
                "njmind_form": {"domain": {"compact_focus": " 创建了什么表单、修改了哪些字段 "}},
                "leave_application": {"domain": {}},
            }
            pm.assemble_packs(app_state)

        compressor.set_compact_focus.assert_called_once_with(
            "创建了什么表单、修改了哪些字段")

    def test_no_compressor_skips_refresh(self):
        """compressor 未就位（异常装配序）不炸，静默跳过刷新。"""
        import services.pack_manager as pm
        from unittest.mock import patch

        app_state = MagicMock()
        app_state.compressor = None  # 显式无 compressor
        with patch("domains.load_all_packs") as lap, \
             patch("domains.load_pack_configs") as lpc, \
             patch("engine.nodes.configure"):
            lap.return_value = (MagicMock(), None, {}, {})
            lpc.return_value = {}
            pm.assemble_packs(app_state)  # 不抛即通过
