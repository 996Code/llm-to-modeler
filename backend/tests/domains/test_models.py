"""njmind_form models 测试。"""
import pytest
from domains.njmind_form.models import ParsedField, FormConfig


class TestParsedField:
    def test_parsed_field_defaults(self):
        f = ParsedField()
        assert f.fieldTitleText == ""
        assert f.formFieldType == 0
        assert f.fieldTypeName == "TEXT"

    def test_parsed_field_with_values(self):
        f = ParsedField(
            fieldTitleText="姓名",
            fieldTitleKey="xingming",
            formFieldType=0,
            fieldTypeName="TEXT",
        )
        assert f.fieldTitleText == "姓名"
        assert f.fieldTitleKey == "xingming"

    def test_parsed_field_with_options(self):
        f = ParsedField(
            fieldTitleText="请假类型",
            formFieldType=4,  # SELECT
            options=["年假", "事假", "病假"],
        )
        assert f.options == ["年假", "事假", "病假"]


class TestFormConfig:
    def test_form_config_defaults(self):
        c = FormConfig()
        assert c.formCode == ""
        assert c.formFieldConfigVos == []
        assert c.isShowFieldAdd is True

    def test_form_config_with_values(self):
        c = FormConfig(
            formCode="qingjia",
            formName="请假表",
            formFieldConfigVos=[{"fieldTitleText": "姓名"}],
        )
        assert c.formCode == "qingjia"
        assert len(c.formFieldConfigVos) == 1

    def test_form_config_allows_extra_fields(self):
        """模板可能带更多字段,Config.extra='allow' 允许。"""
        c = FormConfig(
            formCode="test",
            customField="customValue",  # 不在 schema 里
        )
        assert c.formCode == "test"
