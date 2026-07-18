"""ToolResult 三层结构 + AskSpec 追问规格测试。"""
import pytest
from pydantic import ValidationError

from sdk.tool import ToolResult, AskSpec, AskQuestion, AskOption, ClarificationRaised


class TestToolResult:
    def test_empty_result_defaults(self):
        """空 ToolResult 所有字段有默认值。"""
        r = ToolResult()
        assert r.artifact is None
        assert r.reply is None
        assert r.ask is None
        assert r.summary == ""
        assert r.extra == {}
        assert r.error_for_llm is None

    def test_artifact_is_dict_opaque(self):
        """artifact 是 dict,Engine 不读内部结构。"""
        r = ToolResult(artifact={"formCode": "leave", "formFieldConfigVos": []})
        assert isinstance(r.artifact, dict)

    def test_extra_accepts_arbitrary(self):
        """extra 接受领域自由扩展。"""
        r = ToolResult(extra={"validation_errors": ["字段缺失"]})
        assert r.extra["validation_errors"] == ["字段缺失"]


class TestAskSpec:
    """C.2-A:Clarification 建模为内置 AskTool 的追问规格。"""

    def test_ask_with_questions(self):
        q = AskQuestion(
            question="请假单需要哪些字段?",
            header="字段",
            options=[AskOption(label="基础字段", description="申请人、日期"),
                     AskOption(label="完整字段", description="含请假原因、审批人")],
        )
        spec = AskSpec(questions=[q])
        assert len(spec.questions) == 1
        assert spec.questions[0].header == "字段"

    def test_ask_option_requires_label_and_description(self):
        with pytest.raises(ValidationError):
            AskOption()  # 缺必填


class TestClarificationRaisedLegacy:
    """向后兼容:旧式异常仍可抛(阶段 3 改造完的工具会切到 ToolResult.ask)。"""

    def test_exception_carries_questions(self):
        exc = ClarificationRaised(questions=["需要哪些字段?"])
        assert exc.questions == ["需要哪些字段?"]
