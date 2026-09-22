"""脚本生成工具测试：结构校验 / row 引用拦截 / 字段目录值形态标注。"""
from unittest.mock import MagicMock

from domains.njmind_form.tools._script_common import (
    build_field_catalog, JS_MARK, SQL_MARK, strip_script_mark, script_mark_of,
)
from domains.njmind_form.tools.generate_js_script import GenerateJsScriptTool
from sdk.tool import ToolContext


def _make_ctx():
    ctx = ToolContext(
        llm_client=MagicMock(),
        asset_client=None,
        conversation=None,
        emit=lambda *a, **k: None,
    )
    return ctx


class TestScriptMark:
    def test_mark_roundtrip(self):
        assert script_mark_of("[script:js] 让金额只读") == JS_MARK
        assert script_mark_of("[script:sql] 只看本部门") == SQL_MARK
        assert script_mark_of("普通需求") is None
        assert strip_script_mark("[script:js] 需求 A") == "需求 A"


class TestFieldCatalog:
    def test_id_value_types_annotated(self):
        """目录对 6/7/10/15/17 标注 值=ID串（与 lesscode _label 类型集对齐）。"""
        fields = [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "formFieldType": 1},
            {"fieldTitleKey": "dep", "fieldTitleText": "部门",
             "formFieldType": 6},
            {"fieldTitleKey": "cascade", "fieldTitleText": "级联",
             "formFieldType": 15},
        ]
        catalog = build_field_catalog(fields)
        lines = catalog["text"].splitlines()
        assert "值=ID串" in lines[1]  # 部门
        assert "值=ID串" in lines[2]  # 级联
        assert "值=ID串" not in lines[0]  # 金额
        assert catalog["keys"] == {"jine", "dep", "cascade"}


class TestJsCheck:
    def _tool(self):
        return GenerateJsScriptTool()

    def _state(self, script, slot):
        catalog = build_field_catalog([
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "formFieldType": 1},
            {"fieldTitleKey": "shifouzhifu", "fieldTitleText": "是否支付",
             "formFieldType": 4,
             "optionSettings": {"optionFields": [
                 {"optionLabel": "已支付", "optionValue": 1},
                 {"optionLabel": "未支付", "optionValue": 0}]}}])
        return {
            "script": script,
            "script_slot": slot,
            "known_keys": catalog["keys"],
            "top_keys": catalog["top_keys"],
            # 预置达上限：失败即丢弃，不再触发 generate 重试（LLM mock 无意义）
            "retry_count": 99,
            "check_errors": [],
        }

    def test_row_ref_in_non_row_slot_rejected(self):
        """三码脚本位用 row. 取值 → 拦截（运行时 row=字段配置，语义陷阱）。"""
        state = self._state(
            "({ raw, formData, userInfo }) => { return row.jine > 100; }",
            "editFieldCode")
        self._tool()._step_check(state, _make_ctx())
        assert any("row" in e for e in state["check_errors"])
        assert "script" not in state  # 达上限丢弃

    def test_row_ref_ok_in_row_slot(self):
        """row 两码脚本位用 row. 引用目录内 key → 通过。"""
        state = self._state(
            "({ raw, formData, userInfo, row }) => { return row.jine > 1000; }",
            "viewDetailCode")
        self._tool()._step_check(state, _make_ctx())
        assert not state.get("check_errors")

    def test_row_bad_key_in_row_slot_rejected(self):
        state = self._state(
            "({ raw, formData, userInfo, row }) => { return row.bianzao > 1; }",
            "editDetailCode")
        self._tool()._step_check(state, _make_ctx())
        assert any("row 引用了不存在" in e for e in state["check_errors"])

    def test_string_compare_script_passes(self):
        """字符串形态比较的合法脚本 → 通过（值形态引导的回归样例）。"""
        state = self._state(
            "({ raw, formData, userInfo }) => "
            "{ return formData.shifouzhifu === '1'; }",
            "editFieldCode")
        self._tool()._step_check(state, _make_ctx())
        assert not state.get("check_errors")
