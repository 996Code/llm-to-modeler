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
        """首次 modify:从 source_artifact 出发；formCode 硬约束保留基线值。"""
        tool = ModifyFormTool()
        llm = MagicMock()
        # LLM 越权改了 formCode（应为 modified）——代码层保护必须把它纠正回基线
        llm.chat_json.return_value = {"formCode": "modified", "formName": "改名",
                                      "formFieldConfigVos": []}
        ctx = _make_ctx(llm=llm)

        state = {
            "user_input": "加一个手机号字段",
            "source_artifact": {"formCode": "original", "formFieldConfigVos": []},
            "guide": {},
        }
        tool._step_modify(state, ctx)

        assert state["artifact"]["formCode"] == "original"   # 数据库标识不可改
        assert state["artifact"]["formName"] == "改名"        # 其他内容正常生效
        assert state["validation_errors"] == []

    def test_modify_uses_artifact_on_retry(self):
        """retry:增量路径基于 artifact(上次失败结果)构建目录，且带校验错误。"""
        from types import SimpleNamespace
        tool = ModifyFormTool()
        llm = MagicMock()
        # 指令集输出：对 broken 产物里的字段做一次修改
        llm.chat_json.return_value = {"ops": [
            {"op": "update_field", "key": "brokenfield",
             "patch": {"isRequiredField": 0}},
        ]}
        # render 直接回显 catalog 变量（目录文本进 system prompt 的断言用）
        loader = SimpleNamespace(render=lambda d, n, **v: v.get("catalog", ""))
        ctx = _make_ctx(llm=llm, prompt_loader=loader)

        state = {
            "user_input": "加字段",
            "source_artifact": {"formCode": "original",
                                "formFieldConfigVos": [{"fieldTitleKey": "origfield",
                                                        "fieldTitleText": "原",
                                                        "formFieldType": 0}]},
            "artifact": {"formCode": "broken",
                         "formFieldConfigVos": [{"fieldTitleKey": "brokenfield",
                                                 "fieldTitleText": "破",
                                                 "formFieldType": 0,
                                                 "isRequiredField": 1}]},
            "validation_errors": [{"message": "err"}],
            "guide": {},
        }
        tool._step_modify(state, ctx)

        # LLM 收到的目录应基于 broken 产物（含 brokenfield），不是 original
        call_args = llm.chat_json.call_args
        messages = call_args[0][0]
        system_msg = next(m for m in messages if m["role"] == "system")
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "brokenfield" in system_msg["content"]      # 目录 = artifact 的字段
        assert "origfield" not in system_msg["content"]     # 不是 source 的字段
        assert "err" in user_msg["content"]                 # 校验错误带进重试
        # 增量成功：patch 已应用到副本
        assert state["artifact"]["formFieldConfigVos"][0]["isRequiredField"] == 0


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
