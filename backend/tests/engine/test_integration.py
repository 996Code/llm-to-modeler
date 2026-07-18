"""集成测试:format_result 钩子化 + 连接复用 + 端到端 ToolResult 流。"""
import pytest
from unittest.mock import MagicMock

from engine.dispatcher import ToolDispatcher
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext
from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.tools.modify_form import ModifyFormTool
from domains.njmind_form.tools.chat import ChatTool


class TestFormatResultHook:
    """format_result 钩子:Engine 不直接读制品内部字段。"""

    def test_create_form_format_result(self):
        """CreateFormTool.format_result 提取前端需要的字段。"""
        tool = CreateFormTool()
        artifact = {
            "formName": "请假表",
            "formCode": "qingjia",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.format_result(artifact)
        assert result["fieldCount"] == 2
        assert result["formName"] == "请假表"
        assert result["formCode"] == "qingjia"

    def test_modify_form_format_result(self):
        """ModifyFormTool.format_result 同样提取前端字段。"""
        tool = ModifyFormTool()
        artifact = {
            "formName": "客户表",
            "formCode": "customer",
            "formFieldConfigVos": [{"fieldTitleText": "名称"}],
        }
        result = tool.format_result(artifact)
        assert result["fieldCount"] == 1
        assert result["formName"] == "客户表"

    def test_chat_tool_format_result_defaults_empty(self):
        """ChatTool 不产出制品,format_result 返回空 dict(默认实现)。"""
        tool = ChatTool()
        assert tool.format_result({}) == {}

    def test_execute_includes_formatted_in_extra(self):
        """CreateFormTool.execute 把 format_result 结果放进 extra.formatted。"""
        tool = CreateFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "needsClarification": False,
            "formName": "测试表",
            "formCode": "test",
            "fields": [{"fieldTitleText": "字段1", "fieldType": 0}],
        }
        asset = MagicMock()
        asset.get_guide.return_value = {}
        asset.list_templates.return_value = []
        asset.get_template.return_value = {"formName": "模板"}
        asset.validate_artifact.return_value = {"valid": True, "errors": [], "warnings": []}

        ctx = ToolContext(
            llm_client=llm,
            asset_client=asset,
            conversation=None,
            emit=lambda *a, **k: None,
        )
        object.__setattr__(ctx, "prompt_loader", None)

        result = tool.execute({"user_input": "创建测试表"}, ctx)
        assert "formatted" in result.extra
        assert result.extra["formatted"]["formName"] == "测试表"


class TestAssetClientReuse:
    """CRITICAL 修复:asset_client 复用,不每次 new UpstreamClient。"""

    def test_asset_client_created_once(self):
        """_build_ctx 首次创建 asset_client,后续复用同一实例。"""
        registry = ToolRegistry()
        registry.register(ChatTool())
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"]}
        llm.chat.return_value = "回复"

        dispatcher = ToolDispatcher(registry, llm, asset_client=MagicMock())

        # 注入了 asset_client,不应再创建
        ctx1 = dispatcher._build_ctx({}, lambda *a, **k: None)
        ctx2 = dispatcher._build_ctx({}, lambda *a, **k: None)
        assert ctx1.asset_client is ctx2.asset_client  # 同一实例

    def test_asset_client_lazy_creation(self):
        """未注入 asset_client 时,首次 _build_ctx 延迟创建并缓存。"""
        registry = ToolRegistry()
        registry.register(ChatTool())
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"]}
        llm.chat.return_value = "回复"

        dispatcher = ToolDispatcher(registry, llm)  # 不传 asset_client

        # 首次创建
        ctx1 = dispatcher._build_ctx({}, lambda *a, **k: None)
        assert dispatcher._asset_client is not None
        first_client = dispatcher._asset_client

        # 第二次复用
        ctx2 = dispatcher._build_ctx({}, lambda *a, **k: None)
        assert dispatcher._asset_client is first_client  # 同一实例


class TestEndToEndToolResultFlow:
    """端到端:Dispatcher.run -> ToolResult 三态分流。"""

    def test_e2e_chat_flow(self):
        """闲聊端到端:LLM 选 chat -> ChatTool 执行 -> reply。"""
        registry = ToolRegistry()
        registry.register(ChatTool())
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"]}
        llm.chat.return_value = "你好!我是助手。"

        dispatcher = ToolDispatcher(
            registry, llm,
            asset_client=MagicMock(),
            prompt_loader=None,
        )
        result = dispatcher.run("你好", "conv1")

        assert result.reply == "你好!我是助手。"
        assert result.summary
        assert result.artifact is None
        assert result.ask is None
        assert result.error_for_llm is None

    def test_e2e_error_flow(self):
        """错误端到端:工具抛异常 -> error_for_llm。"""
        class CrashTool(Tool):
            name = "chat"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                raise RuntimeError("连接失败")

        registry = ToolRegistry()
        registry.register(CrashTool())
        llm = MagicMock()
        llm.chat_json.return_value = {"tools": ["chat"]}

        dispatcher = ToolDispatcher(registry, llm, asset_client=MagicMock())
        result = dispatcher.run("你好", "conv1")

        assert result.error_for_llm is not None
        assert "连接失败" in result.error_for_llm
