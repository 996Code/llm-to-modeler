"""ImageFormTool 测试。"""
import pytest
from unittest.mock import MagicMock

from domains.njmind_form.tools.image_form import ImageFormTool
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


class TestImageFormToolDeclaration:
    def test_is_not_destructive(self):
        assert ImageFormTool().is_destructive is False

    def test_is_read_only(self):
        assert ImageFormTool().is_read_only is True

    def test_is_concurrency_safe(self):
        assert ImageFormTool().is_concurrency_safe is True

    def test_name(self):
        assert ImageFormTool().name == "image_form"


class TestAnalyzeImage:
    def test_analyze_with_image_url(self):
        """提供 image_url -> 构建多模态消息并调用 LLM。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "formName": "请假表",
            "fields": [{"fieldTitleText": "姓名", "fieldType": 0, "isRequired": True}],
        }
        ctx = _make_ctx(llm=llm)

        result = tool._analyze_image("根据图片生成表单", "https://example.com/form.png", None, ctx)
        assert result is not None
        assert result["formName"] == "请假表"
        # 验证 LLM 被调用时消息中包含 image_url
        call_args = llm.chat_json.call_args
        messages = call_args[0][0]
        has_image = any(
            isinstance(m.get("content"), list) and
            any(item.get("type") == "image_url" for item in m["content"])
            for m in messages
        )
        assert has_image

    def test_analyze_with_image_base64(self):
        """提供 image_base64 -> 构建带 data URL 的多模态消息。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "formName": "报销表",
            "fields": [{"fieldTitleText": "金额", "fieldType": 2, "isRequired": True}],
        }
        ctx = _make_ctx(llm=llm)

        result = tool._analyze_image("生成表单", None, "iVBORw0KGgoAAAANSUhEUg==", ctx)
        assert result is not None
        assert result["formName"] == "报销表"
        # 验证 LLM 消息中包含 base64 data URL
        call_args = llm.chat_json.call_args
        messages = call_args[0][0]
        has_base64 = any(
            isinstance(m.get("content"), list) and
            any(
                item.get("type") == "image_url" and
                "data:image/png;base64," in item.get("image_url", {}).get("url", "")
                for item in m["content"]
            )
            for m in messages
        )
        assert has_base64

    def test_analyze_exception(self):
        """LLM 抛异常 -> 返回 None。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = Exception("LLM error")
        ctx = _make_ctx(llm=llm)

        result = tool._analyze_image("生成表单", "https://example.com/img.png", None, ctx)
        assert result is None


class TestExecute:
    def test_execute_no_image(self):
        """未提供图片 -> 返回错误。"""
        tool = ImageFormTool()
        ctx = _make_ctx()

        result = tool.execute({"user_input": "根据图片生成表单"}, ctx)
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "未提供图片" in result.error_for_llm

    def test_execute_with_image_url_success(self):
        """image_url 提供 -> 分析 -> 生成 -> 校验通过 -> 成功。"""
        tool = ImageFormTool()
        llm = MagicMock()
        # _analyze_image 调用
        llm.chat_json.side_effect = [
            # 第一次: _analyze_image
            {
                "formName": "请假表",
                "fields": [{"fieldTitleText": "姓名", "fieldType": 0, "isRequired": True}],
            },
            # 第二次: _generate_config
            {
                "formCode": "qingjia_sqb",
                "formName": "请假表",
                "formFieldConfigVos": [
                    {"fieldTitleText": "姓名", "formFieldType": 0, "isRequired": 1},
                ],
            },
        ]
        asset = MagicMock()
        asset.get_guide.return_value = {}
        asset.list_templates.return_value = []
        asset.validate_artifact.return_value = {"valid": True, "errors": []}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute(
            {"user_input": "根据图片生成表单", "image_url": "https://example.com/form.png"},
            ctx,
        )
        assert result.artifact is not None
        assert result.artifact["formName"] == "请假表"
        assert "请假表" in result.summary

    def test_execute_with_image_base64_success(self):
        """image_base64 提供 -> 成功。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = [
            {
                "formName": "报销表",
                "fields": [{"fieldTitleText": "金额", "fieldType": 2, "isRequired": True}],
            },
            {
                "formCode": "baoxiao_sqb",
                "formName": "报销表",
                "formFieldConfigVos": [
                    {"fieldTitleText": "金额", "formFieldType": 2, "isRequired": 1},
                ],
            },
        ]
        asset = MagicMock()
        asset.get_guide.return_value = {}
        asset.list_templates.return_value = []
        asset.validate_artifact.return_value = {"valid": True, "errors": []}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute(
            {"user_input": "生成表单", "image_base64": "iVBORw0KGgoAAAANSUhEUg=="},
            ctx,
        )
        assert result.artifact is not None
        assert result.artifact["formName"] == "报销表"

    def test_execute_analyze_failure(self):
        """图片分析失败 -> 返回错误。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = Exception("analysis failed")
        ctx = _make_ctx(llm=llm)

        result = tool.execute(
            {"user_input": "生成表单", "image_url": "https://example.com/form.png"},
            ctx,
        )
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "图片分析失败" in result.error_for_llm

    def test_execute_generate_failure(self):
        """分析成功但配置生成失败 -> 返回错误。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = [
            # _analyze_image 成功
            {
                "formName": "请假表",
                "fields": [{"fieldTitleText": "姓名", "fieldType": 0, "isRequired": True}],
            },
            # _generate_config 失败
            Exception("generation failed"),
        ]
        asset = MagicMock()
        asset.get_guide.return_value = {}
        asset.list_templates.return_value = []
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute(
            {"user_input": "生成表单", "image_url": "https://example.com/form.png"},
            ctx,
        )
        assert result.artifact is None
        assert result.error_for_llm is not None
        assert "配置生成失败" in result.error_for_llm

    def test_execute_validate_failure(self):
        """校验失败 -> 仍返回配置,但包含 validation_errors。"""
        tool = ImageFormTool()
        llm = MagicMock()
        llm.chat_json.side_effect = [
            # _analyze_image
            {
                "formName": "请假表",
                "fields": [{"fieldTitleText": "姓名", "fieldType": 0, "isRequired": True}],
            },
            # _generate_config
            {
                "formCode": "qingjia_sqb",
                "formName": "请假表",
                "formFieldConfigVos": [
                    {"fieldTitleText": "姓名", "formFieldType": 0, "isRequired": 1},
                ],
            },
        ]
        asset = MagicMock()
        asset.get_guide.return_value = {}
        asset.list_templates.return_value = []
        asset.validate_artifact.return_value = {
            "valid": False,
            "errors": [{"message": "formCode 重复"}],
        }
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute(
            {"user_input": "生成表单", "image_url": "https://example.com/form.png"},
            ctx,
        )
        # 校验失败但仍返回配置
        assert result.artifact is not None
        assert result.artifact["formName"] == "请假表"
        assert "校验失败" in result.summary
        assert result.extra.get("validation_errors") is not None
        assert len(result.extra["validation_errors"]) == 1


class TestSummarizeArtifact:
    def test_summarize_with_fields(self):
        tool = ImageFormTool()
        artifact = {
            "formName": "请假表",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "请假表" in result
        assert "姓名" in result

    def test_summarize_many_fields_truncated(self):
        tool = ImageFormTool()
        artifact = {
            "formName": "大表单",
            "formFieldConfigVos": [
                {"fieldTitleText": f"字段{i}"} for i in range(15)
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "共 15 个字段" in result


class TestFormatResult:
    def test_format_result(self):
        tool = ImageFormTool()
        artifact = {
            "formName": "请假表",
            "formCode": "qingjia_sqb",
            "formFieldConfigVos": [
                {"fieldTitleText": "姓名"},
                {"fieldTitleText": "日期"},
            ],
        }
        result = tool.format_result(artifact)
        assert result["fieldCount"] == 2
        assert result["formName"] == "请假表"
        assert result["formCode"] == "qingjia_sqb"
        assert result["title"] == "请假表"
