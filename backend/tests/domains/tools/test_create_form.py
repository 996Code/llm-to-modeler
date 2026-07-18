"""CreateFormTool 测试。"""
import pytest
from unittest.mock import MagicMock

from domains.njmind_form.tools.create_form import CreateFormTool
from sdk.tool import ToolContext, ToolResult, ClarificationRaised


def _make_ctx(llm=None, asset_client=None, prompt_loader=None):
    ctx = ToolContext(
        llm_client=llm or MagicMock(),
        asset_client=asset_client or MagicMock(),
        conversation=None,
        emit=lambda *a, **k: None,
    )
    object.__setattr__(ctx, "prompt_loader", prompt_loader)
    return ctx


class TestCreateFormToolDeclaration:
    def test_is_destructive(self):
        assert CreateFormTool().is_destructive is True

    def test_is_not_concurrency_safe(self):
        assert CreateFormTool().is_concurrency_safe is False

    def test_steps_count(self):
        assert len(CreateFormTool().steps) == 6

    def test_steps_order(self):
        assert CreateFormTool().steps == [
            "fetch_guide", "list_assets", "parse_fields",
            "fetch_templates", "generate", "validate",
        ]

    def test_input_schema(self):
        schema = CreateFormTool().input_schema()
        assert "user_input" in schema["properties"]


class TestSummarizeArtifact:
    def test_summarize_with_fields(self):
        tool = CreateFormTool()
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
        assert "姓名" in result
        assert "日期" in result

    def test_summarized_many_fields_truncated(self):
        tool = CreateFormTool()
        artifact = {
            "formName": "大表单",
            "formCode": "big",
            "formFieldConfigVos": [
                {"fieldTitleText": f"字段{i}"} for i in range(15)
            ],
        }
        result = tool.summarize_artifact(artifact)
        assert "共 15 个字段" in result


class TestStepFetchGuide:
    def test_fetch_guide_stores_in_state(self):
        tool = CreateFormTool()
        asset = MagicMock()
        asset.get_guide.return_value = {"title": "指南"}
        ctx = _make_ctx(asset_client=asset)

        state = {}
        tool._step_fetch_guide(state, ctx)
        assert state["guide"] == {"title": "指南"}


class TestStepParseFields:
    def test_parse_fields_success(self):
        """LLM 返回清晰需求 -> 解析字段存 state。"""
        tool = CreateFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "needsClarification": False,
            "formName": "请假表",
            "formCode": "qingjia",
            "fields": [
                {"fieldTitleText": "姓名", "fieldTitleKey": "xingming", "fieldType": 0},
            ],
        }
        ctx = _make_ctx(llm=llm)
        state = {"user_input": "创建请假表", "guide": {}}

        tool._step_parse_fields(state, ctx)
        assert state["form_name"] == "请假表"
        assert state["form_code"] == "qingjia"
        assert len(state["parsed_fields"]) == 1
        assert state["parsed_fields"][0].fieldTitleText == "姓名"

    def test_parse_fields_raises_clarification(self):
        """需求模糊 -> 抛 ClarificationRaised。"""
        tool = CreateFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {
            "needsClarification": True,
            "clarificationQuestions": ["需要哪些字段?"],
        }
        ctx = _make_ctx(llm=llm)
        state = {"user_input": "创建一个表单", "guide": {}}

        with pytest.raises(ClarificationRaised) as exc:
            tool._step_parse_fields(state, ctx)
        assert exc.value.questions == ["需要哪些字段?"]


class TestStepFetchTemplates:
    def test_fetch_templates_gets_form_and_field_templates(self):
        tool = CreateFormTool()
        asset = MagicMock()
        asset.get_template.side_effect = lambda name: {"name": name}
        ctx = _make_ctx(asset_client=asset)

        from domains.njmind_form.models import ParsedField
        state = {
            "parsed_fields": [
                ParsedField(fieldTitleText="姓名", formFieldType=0),  # TEXT -> text_field
                ParsedField(fieldTitleText="金额", formFieldType=1),  # NUMBER -> number_field
            ],
        }
        tool._step_fetch_templates(state, ctx)
        assert state["form_template"] == {"name": "simple_form"}
        assert "TEXT" in state["field_templates"]
        assert "NUMBER" in state["field_templates"]


class TestStepValidate:
    def test_validate_pass(self):
        tool = CreateFormTool()
        asset = MagicMock()
        asset.validate_artifact.return_value = {"valid": True, "errors": [], "warnings": []}
        ctx = _make_ctx(asset_client=asset)
        state = {"artifact": {"formCode": "test"}}

        tool._step_validate(state, ctx)
        assert state["validation_errors"] == []

    def test_validate_fail_retries(self):
        """校验失败 -> 重跑 generate + 递归再校验,上限 3 次。"""
        tool = CreateFormTool()
        asset = MagicMock()
        # 前 2 次失败,第 3 次通过
        asset.validate_artifact.side_effect = [
            {"valid": False, "errors": [{"message": "err1"}], "warnings": []},
            {"valid": False, "errors": [{"message": "err2"}], "warnings": []},
            {"valid": True, "errors": [], "warnings": []},
        ]
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "fixed", "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        state = {"artifact": {"formCode": "test"}, "retry_count": 0}
        tool._step_validate(state, ctx)

        assert state["validation_errors"] == []  # 最终通过
        assert state["retry_count"] == 2  # 重试了 2 次

    def test_validate_exceeds_max_retries(self):
        """超过 MAX_RETRIES -> 错误留在 state,不无限重试。"""
        tool = CreateFormTool()
        asset = MagicMock()
        asset.validate_artifact.return_value = {
            "valid": False, "errors": [{"message": "永远失败"}], "warnings": []
        }
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "x", "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        state = {"artifact": {"formCode": "test"}, "retry_count": 0}
        tool._step_validate(state, ctx)

        assert len(state["validation_errors"]) > 0  # 错误保留
        assert state["retry_count"] >= 3  # 达到上限
