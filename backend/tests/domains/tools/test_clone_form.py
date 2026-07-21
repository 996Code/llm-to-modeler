"""CloneFormTool 测试。"""
import pytest
from unittest.mock import MagicMock

from domains.njmind_form.tools.clone_form import CloneFormTool
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


class TestCloneFormToolDeclaration:
    def test_is_destructive(self):
        assert CloneFormTool().is_destructive is True

    def test_is_not_read_only(self):
        assert CloneFormTool().is_read_only is False

    def test_is_not_concurrency_safe(self):
        assert CloneFormTool().is_concurrency_safe is False

    def test_name(self):
        assert CloneFormTool().name == "clone_form"


class TestExtractCloneInfo:
    def test_extract_with_all_fields(self):
        """LLM 返回 source_form_code + new_form_name + new_form_code。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "qingjia_sqb",
            "new_form_name": "年假申请表",
            "new_form_code": "nianjia_sqb",
        }
        ctx = _make_ctx(llm=llm)

        result = tool._extract_clone_info("基于请假表单创建一个年假申请副本", ctx)
        assert result["source_form_code"] == "qingjia_sqb"
        assert result["new_form_name"] == "年假申请表"
        assert result["new_form_code"] == "nianjia_sqb"

    def test_extract_with_source_only(self):
        """LLM 只返回 source_form_code,新名称/标识为空。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "qingjia_sqb",
            "new_form_name": "",
            "new_form_code": "",
        }
        ctx = _make_ctx(llm=llm)

        result = tool._extract_clone_info("复制请假表单", ctx)
        assert result["source_form_code"] == "qingjia_sqb"
        assert result["new_form_name"] == ""
        assert result["new_form_code"] == ""

    def test_extract_exception(self):
        """LLM 抛异常 -> 返回 None。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = Exception("LLM error")
        ctx = _make_ctx(llm=llm)

        result = tool._extract_clone_info("复制表单", ctx)
        assert result is None


class TestModifyIdentity:
    def test_modify_with_new_name_and_code(self):
        """提供了新名称和新标识 -> 使用提供的值。"""
        tool = CloneFormTool()
        source = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [{"fieldTitleText": "姓名"}],
        }

        result = tool._modify_identity(source, "年假申请表", "nianjia_sqb")
        assert result["formCode"] == "nianjia_sqb"
        assert result["formName"] == "年假申请表"

    def test_modify_without_new_name_and_code(self):
        """未提供新名称/标识 -> 自动生成 _copy 后缀和 (副本) 名称。"""
        tool = CloneFormTool()
        source = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [{"fieldTitleText": "姓名"}],
        }

        result = tool._modify_identity(source, "", "")
        assert result["formCode"] == "qingjia_sqb_copy"
        assert result["formName"] == "请假申请表(副本)"

    def test_modify_removes_id_fields(self):
        """应移除 id/createTime/updateTime 等字段。"""
        tool = CloneFormTool()
        source = {
            "id": 123,
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "createTime": "2024-01-01",
            "updateTime": "2024-06-01",
            "createBy": "admin",
            "updateBy": "admin",
            "formFieldConfigVos": [],
        }

        result = tool._modify_identity(source, "新表单", "new_code")
        assert "id" not in result
        assert "createTime" not in result
        assert "updateTime" not in result
        assert "createBy" not in result
        assert "updateBy" not in result
        assert result["formCode"] == "new_code"
        assert result["formName"] == "新表单"

    def test_modify_does_not_mutate_source(self):
        """修改标识不应改变源配置。"""
        tool = CloneFormTool()
        source = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [{"fieldTitleText": "姓名"}],
        }

        result = tool._modify_identity(source, "", "")
        assert source["formCode"] == "qingjia_sqb"
        assert source["formName"] == "请假申请表"
        assert result["formCode"] == "qingjia_sqb_copy"


class TestExecute:
    def test_execute_success(self):
        """完整成功流程:提取 -> 查询源 -> 修改标识 -> 校验通过 -> 创建成功。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "qingjia_sqb",
            "new_form_name": "年假申请表",
            "new_form_code": "nianjia_sqb",
        }
        asset = MagicMock()
        asset.get_form.return_value = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [{"fieldTitleText": "姓名"}],
        }
        asset.validate_artifact.return_value = {"valid": True, "errors": []}
        asset.persist_artifact.return_value = {"success": True}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "基于请假表单创建年假申请副本"}, ctx)
        assert result.artifact is not None
        assert result.artifact["formCode"] == "nianjia_sqb"
        assert result.artifact["formName"] == "年假申请表"
        assert "年假申请表" in result.summary

    def test_execute_source_not_found(self):
        """源表单不存在 -> 返回错误。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "not_exist",
            "new_form_name": "",
            "new_form_code": "",
        }
        asset = MagicMock()
        asset.get_form.return_value = None
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "复制不存在的表单"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "not_exist" in result.error_for_llm

    def test_execute_validate_fail(self):
        """校验不通过 -> 返回错误。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "qingjia_sqb",
            "new_form_name": "",
            "new_form_code": "",
        }
        asset = MagicMock()
        asset.get_form.return_value = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [],
        }
        asset.validate_artifact.return_value = {
            "valid": False,
            "errors": [{"message": "formCode 已存在"}],
        }
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "复制请假表单"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "校验失败" in result.error_for_llm

    def test_execute_no_source_code_extracted(self):
        """LLM 无法提取 source_form_code -> 返回错误。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "",
            "new_form_name": "",
            "new_form_code": "",
        }
        ctx = _make_ctx(llm=llm)

        result = tool.execute({"user_input": "随便说说"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None

    def test_execute_create_failure(self):
        """创建新表单失败 -> 返回错误。"""
        tool = CloneFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "source_form_code": "qingjia_sqb",
            "new_form_name": "副本",
            "new_form_code": "qingjia_copy",
        }
        asset = MagicMock()
        asset.get_form.return_value = {
            "formCode": "qingjia_sqb",
            "formName": "请假申请表",
            "formFieldConfigVos": [],
        }
        asset.validate_artifact.return_value = {"valid": True, "errors": []}
        asset.persist_artifact.return_value = {"success": False}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "复制请假表单"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "创建新表单失败" in result.error_for_llm


class TestSummarizeArtifact:
    def test_summarize(self):
        tool = CloneFormTool()
        artifact = {
            "formName": "请假表(副本)",
            "formCode": "qingjia_copy",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "请假表(副本)" in result
        assert "qingjia_copy" in result
        assert "2 个字段" in result


class TestFormatResult:
    def test_format_result(self):
        tool = CloneFormTool()
        artifact = {
            "formName": "请假表(副本)",
            "formCode": "qingjia_copy",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.format_result(artifact)
        assert result["fieldCount"] == 2
        assert result["formName"] == "请假表(副本)"
        assert result["formCode"] == "qingjia_copy"
        assert result["title"] == "请假表(副本)"
