"""Request-scoped routing must not escape the selected plugin."""
import pytest

from engine import nodes
from sdk.pack_router import DefaultPackRouter
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult


class EchoTool(Tool):
    description = "测试处理器"
    when = "测试"

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def input_schema(self):
        return {}

    def execute(self, state, ctx):
        self.calls += 1
        return ToolResult(reply="executed")


class FixedRouter(DefaultPackRouter):
    def __init__(self, registry, result):
        super().__init__(registry)
        self.result = result

    def route(self, *args, **kwargs):
        return self.result


@pytest.fixture
def configured(monkeypatch):
    for key in ("_registry", "_pack_routers", "_pack_configs", "_llm_client",
                "_asset_client", "_conversation", "_prompt_loader"):
        monkeypatch.setattr(nodes, key, getattr(nodes, key))
    a, b = EchoTool("alpha_tool"), EchoTool("beta_tool")
    ra, rb, merged = ToolRegistry(), ToolRegistry(), ToolRegistry()
    ra.register(a)
    rb.register(b)
    merged.register(b)
    merged.register(a)
    routers = {"alpha": FixedRouter(ra, (None, 0.0)),
               "beta": FixedRouter(rb, (b.name, 1.0))}
    nodes.configure(merged, None, None, pack_routers=routers,
                    pack_configs={"alpha": {"domain": {}}, "beta": {"domain": {}}})
    return a, b, routers


def test_no_match_does_not_execute_global_first(configured):
    out = nodes.classify_intent_node({"user_input": "summary", "packs": ["alpha"]})
    assert out["tool_name"] == ""
    assert any(e["type"] == "result" and e["data"].get("error") for e in out["sse_events"])


def test_router_cannot_return_tool_from_another_pack(configured):
    _, b, routers = configured
    routers["alpha"].result = (b.name, 1.0)
    out = nodes.classify_intent_node({"user_input": "x", "packs": ["alpha"]})
    assert out["tool_name"] == ""


def test_unavailable_explicit_pack_never_becomes_global(configured):
    assert nodes._route_pack("x", packs=["missing"]) == ""


def test_execute_blocks_old_cross_pack_checkpoint(configured):
    _, b, _ = configured
    out = nodes.execute_tool_node({"packs": ["alpha"], "tool_name": b.name,
                                   "tool_state": {"user_input": "x"}})
    assert b.calls == 0
    assert out["tool_result"]["error_for_llm"]


def test_each_new_route_resets_previous_confirmation(configured):
    a, _, routers = configured
    routers["alpha"].result = (a.name, 0.3)
    out = nodes.classify_intent_node({"user_input": "x", "packs": ["alpha"],
                                      "intent_confirmed": True})
    assert out["intent_confirmed"] is False


def test_no_arbitrary_global_fallback(configured):
    assert nodes._get_fallback_tool_name() == ""


def test_declared_fallback_keeps_own_confidence(configured):
    a, _, _ = configured
    nodes._pack_configs["alpha"] = {"domain": {"fallback_tool": a.name}}
    out = nodes.classify_intent_node({"user_input": "x", "packs": ["alpha"]})
    assert out["tool_name"] == a.name
    assert out["intent_confidence"] is None


def test_global_pack_no_match_without_declared_fallback_stops(configured):
    class NoMatch:
        def chat_json(self, *args, **kwargs):
            return {"pack": None}
    nodes._llm_client = NoMatch()
    assert nodes._route_pack("x") == ""


def test_execution_keeps_selected_pack_even_with_multiple_allowed(configured):
    _, b, _ = configured
    out = nodes.execute_tool_node({"packs": ["alpha", "beta"], "selected_pack": "alpha",
                                   "tool_name": b.name, "tool_state": {"user_input": "x"}})
    assert b.calls == 0
    assert out["tool_result"]["error_for_llm"]


def test_legal_low_confidence_requires_explicit_confirmation(configured, monkeypatch):
    a, _, _ = configured
    state = {"tool_name": a.name, "intent_confidence": 0.2, "user_input": "x"}
    shown = []
    monkeypatch.setattr(nodes, "interrupt", lambda payload: shown.append(payload) or {})
    out = nodes.check_confidence_node(state)
    assert shown
    assert a.description in shown[0]["questions"][0]["question"]
    assert out["tool_name"] == ""


def test_explicit_confirmation_preserves_only_legal_candidate(configured, monkeypatch):
    a, _, _ = configured
    monkeypatch.setattr(nodes, "interrupt", lambda payload: {"意图确认": "就按这个意思"})
    out = nodes.check_confidence_node({"tool_name": a.name, "intent_confidence": 0.2})
    assert out["intent_confirmed"] is True
    assert "tool_name" not in out


def test_valid_scoped_tool_still_executes(configured):
    a, _, _ = configured
    out = nodes.execute_tool_node({"packs": ["alpha"], "tool_name": a.name,
                                   "tool_state": {"user_input": "x"}})
    assert a.calls == 1
    assert out["tool_result"]["reply"] == "executed"


def test_knowledge_router_is_deterministic():
    from domains import load_pack
    reg, _, router = load_pack("knowledge_graph")

    class UnexpectedLLM:
        def chat_json(self, *args, **kwargs):
            pytest.fail("knowledge entry must not classify the question again")

    assert router.route("JJF_1444-2014.pdf 帮我概述", None,
                        llm_client=UnexpectedLLM()) == ("kb_search", 1.0)
