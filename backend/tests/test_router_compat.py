"""二级路由 conv_id 契约协商的回归测试。

背景事故:引擎给 router.route() 硬传 conv_id 关键字参数,而 pack 自定义
路由(njmind_form 规则路由)签名没有该参数 → TypeError 被 except 吞掉 →
兜底到 chat 闲聊工具,闲聊工具谎称"已创建表单"(制品为空)。

修复:引擎按签名协商(_route_accepts_conv_id),旧签名路由不传、新签名
路由传。本测试用真实 pack 路由锁死该行为。
"""
from sdk.pack_router import DefaultPackRouter
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult


class _DummyTool(Tool):
    """最小工具:给 DefaultPackRouter 提供候选。"""

    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "测试工具"

    @property
    def input_schema(self) -> dict:
        return {}

    def execute(self, state, ctx) -> ToolResult:
        return ToolResult(reply="ok")


def test_real_njmind_router_old_signature_compatible():
    """njmind_form 自定义路由(无 conv_id 参数)在新调用方式下正常路由。"""
    from domains import load_pack
    from engine import nodes

    _, _, router = load_pack("njmind_form")  # 真实 NjmindFormRouter

    # 协商正确识别:旧签名 → 不接受 conv_id
    assert nodes._route_accepts_conv_id(router) is False

    # 真实规则路由跑创建话术(创建意图 + 无画布),不因 kwargs 抛 TypeError
    name = router.route("帮我创建一个设备报修表单", None, history="", llm_client=None)
    assert name == "create_form", f"应路由到 create_form,实际 {name}"


def test_default_router_new_signature_accepted():
    """DefaultPackRouter(带 conv_id 参数)协商为接受。"""
    from engine import nodes

    reg = ToolRegistry()
    reg.register(_DummyTool())
    router = DefaultPackRouter(reg)
    assert nodes._route_accepts_conv_id(router) is True


def test_kwargs_router_treated_as_accepting():
    """**kwargs 风格的路由视为接受 conv_id。"""

    class _KwRouter:
        def route(self, user_input, artifact, history="", llm_client=None, **kw):
            return "x"

    from engine import nodes
    assert nodes._route_accepts_conv_id(_KwRouter()) is True


def test_classify_passes_image_base64_through_tool_state():
    """回归:stream.py 注入初始 tool_state 的 image_base64 必须穿过
    classify_intent_node 的 tool_state 重建——修复前该键被丢弃,
    image_form 工具收到"未提供图片"(LangGraph 重构遗留断链)。
    """
    from domains import load_pack
    from engine import nodes

    reg, _, router = load_pack("leave_application")  # 单 pack:一级路由直通,零 LLM
    nodes.configure(
        registry=reg, llm_client=None, asset_client=None, conversation=None,
        prompt_loader=None, pack_routers={"leave_application": router}, pack_configs={},
    )
    out = nodes.classify_intent_node({
        "user_input": "看图建表单",
        "compressed_history": "",
        "conversation_id": "c-img",
        "tool_state": {"image_base64": "QUJD"},
    })
    assert out["tool_state"].get("image_base64") == "QUJD"
    # 不带图片的常规请求:键存在但为 None(不影响其余工具)
    out2 = nodes.classify_intent_node({
        "user_input": "你好", "compressed_history": "", "conversation_id": "c2",
        "tool_state": {},
    })
    assert out2["tool_state"].get("image_base64") is None
