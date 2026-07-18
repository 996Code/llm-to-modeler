"""ModifyFormTool 测试。"""
import pytest
from unittest.mock import MagicMock

from domains.njmind_form.tools.modify_form import ModifyFormTool
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


class TestModifyFormToolDeclaration:
    def test_is_destructive(self):
        assert ModifyFormTool().is_destructive is True

    def test_is_not_concurrency_safe(self):
        assert ModifyFormTool().is_concurrency_safe is False

    def test_steps_count(self):
        assert len(ModifyFormTool().steps) == 3

    def test_steps_order(self):
        assert ModifyFormTool().steps == ["fetch_guide", "modify", "validate"]


class TestValidateInput:
    """语义校验:modify 必须有 source_artifact。"""

    def test_validate_input_fails_without_source(self):
        """无 source_artifact -> 返回错误文本(回流给 LLM)。"""
        tool = ModifyFormTool()
        err = tool.validate_input({})
        assert err is not None
        assert "source_artifact" in err

    def test_validate_input_passes_with_source(self):
        tool = ModifyFormTool()
        err = tool.validate_input({"source_artifact": {"formCode": "x"}})
        assert err is None


class TestStepModify:
    def test_modify_uses_source_artifact_first_time(self):
        """首次 modify:从 source_artifact 出发。"""
        tool = ModifyFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "modified", "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm)

        state = {
            "user_input": "加一个手机号字段",
            "source_artifact": {"formCode": "original", "formFieldConfigVos": []},
            "guide": {},
        }
        tool._step_modify(state, ctx)

        assert state["artifact"]["formCode"] == "modified"
        assert state["validation_errors"] == []

    def test_modify_uses_artifact_on_retry(self):
        """retry:从 artifact(上次失败结果)出发。"""
        tool = ModifyFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "fixed", "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm)

        state = {
            "user_input": "加字段",
            "source_artifact": {"formCode": "original"},
            "artifact": {"formCode": "broken"},
            "validation_errors": [{"message": "err"}],
            "guide": {},
        }
        tool._step_modify(state, ctx)

        # LLM 应该收到 broken 配置(不是 original)
        call_args = llm.chat_json.call_args
        messages = call_args[0][0]
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "broken" in user_msg["content"]


class TestStepValidate:
    def test_validate_pass(self):
        tool = ModifyFormTool()
        asset = MagicMock()
        asset.validate_artifact.return_value = {"valid": True, "errors": [], "warnings": []}
        ctx = _make_ctx(asset_client=asset)
        state = {"artifact": {"formCode": "test"}}

        tool._step_validate(state, ctx)
        assert state["validation_errors"] == []

    def test_validate_fail_retries(self):
        """校验失败 -> 重跑 modify + 递归再校验。"""
        tool = ModifyFormTool()
        asset = MagicMock()
        asset.validate_artifact.side_effect = [
            {"valid": False, "errors": [{"message": "err"}], "warnings": []},
            {"valid": True, "errors": [], "warnings": []},
        ]
        llm = MagicMock()
        llm.chat_json.return_value = {"formCode": "fixed", "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm, asset_client=asset)

        state = {"artifact": {"formCode": "test"}, "retry_count": 0}
        tool._step_validate(state, ctx)

        assert state["validation_errors"] == []
        assert state["retry_count"] == 1


class TestSummarizeArtifact:
    def test_summarize(self):
        tool = ModifyFormTool()
        artifact = {
            "formName": "请假表",
            "formCode": "qingjia",
            "formFieldConfigVos": [{"fieldTitleText": "姓名"}],
        }
        result = tool.summarize_artifact(artifact)
        assert "请假表" in result
        assert "姓名" in result
