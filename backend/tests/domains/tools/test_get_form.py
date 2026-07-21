"""GetFormTool 测试。"""
import pytest
from unittest.mock import MagicMock

from domains.njmind_form.tools.get_form import GetFormTool
from sdk.tool import ToolContext, ToolResult


def _make_ctx(llm=None, asset_client=None, prompt_loader=None):
    ctx = ToolContext(
        llm_client=llm or MagicMock(),
        asset_client=asset_client or MagicMock(),
        conversation=None,
        emit=lambda *a, **k: None,
    )
    object.__setattr__(ctx, "prompt_loader", prompt_loader)
    return ctx


class TestGetFormToolDeclaration:
    def test_is_not_destructive(self):
        assert GetFormTool().is_destructive is False

    def test_is_read_only(self):
        assert GetFormTool().is_read_only is True

    def test_is_concurrency_safe(self):
        assert GetFormTool().is_concurrency_safe is True

    def test_name(self):
        assert GetFormTool().name == "get_form"


class TestExtractFormCode:
    def test_extract_form_code_success(self):
        """LLM 返回 formCode -> 成功提取。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "qingjia_sqb"}
        ctx = _make_ctx(llm=llm)

        result = tool._extract_form_code("查看请假申请表单", ctx)
        assert result == "qingjia_sqb"

    def test_extract_form_code_empty(self):
        """LLM 返回空 -> 返回空字符串。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": ""}
        ctx = _make_ctx(llm=llm)

        result = tool._extract_form_code("随便说说", ctx)
        assert result == ""

    def test_extract_form_code_exception(self):
        """LLM 抛异常 -> 返回 None。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = Exception("LLM error")
        ctx = _make_ctx(llm=llm)

        result = tool._extract_form_code("查看表单", ctx)
        assert result is None


class TestExecute:
    def test_execute_success(self):
        """正常查询:提取 formCode -> API 返回配置 -> 成功。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "qingjia_sqb"}
        asset = MagicMock()
        asset.get_form.return_value = {
            "formName": "请假申请表",
            "formCode": "qingjia_sqb",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "查看请假申请表单"}, ctx)
        assert result.artifact is not None
        assert result.artifact["formName"] == "请假申请表"
        assert "请假申请表" in result.summary
        assert "2 个字段" in result.summary

    def test_execute_form_not_found(self):
        """API 返回 None -> 查询失败。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "not_exist"}
        asset = MagicMock()
        asset.get_form.return_value = None
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "查看不存在的表单"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "not_exist" in result.error_for_llm

    def test_execute_no_form_code_extracted(self):
        """LLM 无法提取 formCode -> 返回错误。"""
        tool = GetFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": ""}
        ctx = _make_ctx(llm=llm)

        result = tool.execute({"user_input": "随便说说"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "formCode" in result.error_for_llm


class TestSummarizeArtifact:
    def test_summarize_with_fields(self):
        tool = GetFormTool()
        artifact = {
            "formName": "请假表",
            "formCode": "qingjia",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "请假表" in result
        assert "qingjia" in result
        assert "姓名" in result

    def test_summarize_many_fields_truncated(self):
        tool = GetFormTool()
        artifact = {
            "formName": "大表单",
            "formCode": "big",
            "formFieldConfigVos": [
                {"fieldTitleText": f"字段{i}"} for i in range(15)
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "共 15 个字段" in result


class TestFormatResult:
    def test_format_result(self):
        tool = GetFormTool()
        artifact = {
            "formName": "请假表",
            "formCode": "qingjia_sqb",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
                {"fieldTitleText": "原因"},
            ],
        }
        result = tool.format_result(artifact)
        assert result["fieldCount"] == 3
        assert result["formName"] == "请假表"
        assert result["formCode"] == "qingjia_sqb"
        assert result["title"] == "请假表"
