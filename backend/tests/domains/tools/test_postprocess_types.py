"""生成后处理的值类型矫正与校验错误归一化测试。

对应两类真实事故：
- 整型属性被 LLM 写成字符串（"12"/"true"）→ 上游 Jackson 反序列化 500；
- 500/超时的异常文本被直接塞进修复 prompt → LLM 无从下手烧光重试。
"""
from domains.njmind_form.tools._postprocess import (
    postprocess_config, normalize_validation_errors,
)


class TestCoerceIntStrings:
    def test_numeric_string_coerced(self):
        """白名单整型键的字符串数字 → int。"""
        cfg = postprocess_config({
            "formColumnsNumber": "4",
            "formFieldConfigVos": [
                {"fieldTitleKey": "a", "fieldTitleText": "姓名",
                 "fieldWidth": "12", "formFieldType": 0,
                 "optionSettings": {"optionFields": [
                     {"optionLabel": "x", "optionValue": "1"},
                 ]}},
            ],
        })
        assert cfg["formColumnsNumber"] == 4
        field = cfg["formFieldConfigVos"][0]
        assert field["fieldWidth"] == 12
        assert field["optionSettings"]["optionFields"][0]["optionValue"] == 1
        # 真字符串字段不受影响
        assert field["fieldTitleText"] == "姓名"

    def test_bool_string_coerced(self):
        """白名单键的 "true"/"false" 字符串 → 1/0。"""
        cfg = postprocess_config({
            "formFieldConfigVos": [
                {"fieldTitleKey": "a", "isRequiredField": "true"},
            ],
        })
        assert cfg["formFieldConfigVos"][0]["isRequiredField"] == 1

    def test_non_numeric_string_untouched(self):
        """非纯数字（"12.5"/空串）保持原样，交给上游报具体错误。"""
        cfg = postprocess_config({
            "formColumnsNumber": "12.5",
            "formFieldConfigVos": [
                {"fieldTitleKey": "a", "fieldWidth": ""},
            ],
        })
        assert cfg["formColumnsNumber"] == "12.5"
        assert cfg["formFieldConfigVos"][0]["fieldWidth"] == ""


class TestNormalizeValidationErrors:
    def test_empty(self):
        assert normalize_validation_errors([]) == []

    def test_transport_error_translated(self):
        """异常文本(500/超时)不进 prompt，替换为可执行的类型修复清单。"""
        errors = [{"message": "Upstream validation request failed: "
                              "Server error '500 Internal Server Error' for url http://x"}]
        out = normalize_validation_errors(errors)
        assert len(out) == 1
        assert "【类型错误】" in out[0]
        assert "500" not in out[0]
        assert "整数" in out[0]

    def test_business_errors_numbered_and_capped(self):
        """业务错误编号保留；超过 6 条聚合为计数提示。"""
        errors = [{"message": f"字段 f{i} 不能为空"} for i in range(8)]
        out = normalize_validation_errors(errors)
        assert out[0].startswith("1. ")
        assert "字段 f0" in out[0]
        assert any("另有 2 条" in x for x in out)

    def test_mixed(self):
        """混合场景：类型修复清单置顶 + 业务错误编号。"""
        errors = [
            {"message": "Upstream validation request failed: Connect Error"},
            {"message": "formTitle 不能为空"},
        ]
        out = normalize_validation_errors(errors)
        assert out[0].startswith("【类型错误】")
        assert out[1].startswith("1. ")
        assert "formTitle" in out[1]
