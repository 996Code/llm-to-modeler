"""preflight 前置校验链路测试（SDK 钩子 → 业务实现 → 引擎拦截）。

三层各自的行为约定：
  - SDK：Tool.preflight 默认通过（None），覆写由业务自己写；
  - 业务（njmind_form）：依赖的上游服务地址不可解析时返回拦截 ToolResult；
  - 引擎：execute_tool_node 在 execute 前调用钩子，非 None 即拦截（不执行工具）。
"""
from unittest.mock import MagicMock

import pytest

from sdk.tool import Tool, ToolContext, ToolResult
from domains.njmind_form.tools._preflight import require_modeler_service


class _NoUpstreamTool(Tool):
    """测试桩：不依赖上游的工具（如 chat），用于验证默认 preflight 通过。"""

    name = "stub_tool"
    description = "测试桩"
    when = "测试"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(reply="ok")


def _ctx(asset_client) -> ToolContext:
    """最小 ToolContext：只需 asset_client，其余依赖置空。"""
    return ToolContext(
        llm_client=None,
        asset_client=asset_client,
        conversation=None,
        emit=lambda *a, **k: None,
    )


class TestSdkHook:
    def test_default_preflight_passes(self):
        """SDK 默认实现返回 None（通过）——业务不覆写就没有校验。"""
        assert _NoUpstreamTool().preflight({}, _ctx(None)) is None


class TestNjmindPreflight:
    def test_blocks_when_service_unresolvable(self):
        """上游地址不可解析 → 返回拦截 ToolResult（含指引性错误信息）。"""
        asset = MagicMock()
        asset.has_service.return_value = False
        result = require_modeler_service(_ctx(asset))
        assert result is not None
        assert result.error_for_llm and "njmind-modeler" in result.error_for_llm
        assert "services" in result.error_for_llm

    def test_passes_when_service_resolvable(self):
        """上游地址可解析 → None（通过）。"""
        asset = MagicMock()
        asset.has_service.return_value = True
        assert require_modeler_service(_ctx(asset)) is None

    def test_passes_when_client_lacks_has_service(self):
        """asset_client 无 has_service 方法（测试桩/非 HTTP 实现）→ 视为通过。

        校验器不拦没有契约的方法；真实运行期由 resolve_base fail-closed 兜底。
        """
        asset = object()  # 任意无该属性的对象
        assert require_modeler_service(_ctx(asset)) is None


class TestEngineWiring:
    """execute_tool_node 把 preflight 作为链路一环调用并拦截。"""

    @pytest.fixture(autouse=True)
    def _setup_nodes(self):
        """注入 fake 依赖并在测试后还原（nodes 模块全局单例，防串测试）。"""
        from engine import nodes

        saved = (nodes._registry, nodes._llm_client, nodes._asset_client,
                 nodes._conversation, nodes._prompt_loader,
                 nodes._pack_routers, nodes._pack_configs)

        blocked = _NoUpstreamTool()
        blocked.name = "blocked_tool"
        blocked.preflight = lambda state, ctx: ToolResult(
            error_for_llm="上游服务地址缺失", summary="缺少上游服务地址",
        )
        executed = _NoUpstreamTool()
        executed.name = "pass_tool"

        registry = MagicMock()
        registry.get = lambda n: {"blocked_tool": blocked, "pass_tool": executed}.get(n)

        nodes.configure(
            registry=registry,
            llm_client=None,
            asset_client=MagicMock(),
            conversation=None,
            prompt_loader=None,
            pack_routers={},
            pack_configs={},
        )
        yield blocked, executed
        nodes.configure(
            registry=saved[0], llm_client=saved[1], asset_client=saved[2],
            conversation=saved[3], prompt_loader=saved[4],
            pack_routers=saved[5], pack_configs=saved[6],
        )

    def test_blocked_tool_not_executed(self, _setup_nodes):
        """preflight 返回非 None → 工具不执行，错误结论进 tool_result。"""
        from engine.nodes import execute_tool_node

        blocked, _ = _setup_nodes
        original_execute = blocked.execute
        called = {"n": 0}
        blocked.execute = lambda state, ctx: (called.__setitem__("n", called["n"] + 1),
                                              original_execute(state, ctx))[1]

        result = execute_tool_node({
            "tool_name": "blocked_tool",
            "tool_state": {},
        })
        assert called["n"] == 0, "被拦截的工具不应执行 execute"
        assert result["tool_result"]["summary"] == "缺少上游服务地址"
        assert result["tool_result"]["error_for_llm"] == "上游服务地址缺失"

    def test_passing_tool_executes(self, _setup_nodes):
        """preflight 通过（None）→ 正常执行。"""
        from engine.nodes import execute_tool_node

        result = execute_tool_node({
            "tool_name": "pass_tool",
            "tool_state": {},
        })
        assert result["tool_result"]["reply"] == "ok"

    def test_preflight_exception_does_not_block(self, _setup_nodes):
        """钩子自身抛异常不拦截（校验器故障 ≠ 业务不可用）。"""
        from engine.nodes import execute_tool_node

        _, executed = _setup_nodes
        executed.preflight = lambda state, ctx: 1 / 0
        result = execute_tool_node({
            "tool_name": "pass_tool",
            "tool_state": {},
        })
        assert result["tool_result"]["reply"] == "ok"
