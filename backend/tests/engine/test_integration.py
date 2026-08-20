"""集成测试:format_result 钩子化（Engine 不直接读制品内部字段）。

注：旧 ToolDispatcher 的连接复用/端到端测试已随 dispatcher.py 退役删除
（graph 架构前的旧调度路径）；新链路的端到端由真实请求 E2E 与
test_modify_form/test_incremental_ops 覆盖。
"""
import pytest
from unittest.mock import MagicMock

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
        asset.get_guide.return_value = {"fieldTypes": [{"code": 0, "name": "TEXT"}]}
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
