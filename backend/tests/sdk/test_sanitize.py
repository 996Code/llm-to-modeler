"""Unicode 隐写清洗测试 — 防上游内容注入隐藏指令。"""
import pytest
from sdk.sanitize import sanitize_text, sanitize_obj


class TestSanitizeText:
    def test_normal_text_unchanged(self):
        assert sanitize_text("正常文本 hello 123") == "正常文本 hello 123"

    def test_zero_width_chars_removed(self):
        # 零宽字符 \u200B-\u200F
        assert sanitize_text("正常\u200B文本") == "正常文本"
        assert sanitize_text("hi\u200cdden") == "hidden"

    def test_bidi_override_removed(self):
        # 方向反转 \u202A-\u202E
        assert sanitize_text("ev\u202eil") == "evil"

    def test_bom_removed(self):
        assert sanitize_text("\ufeffhello") == "hello"

    def test_pua_removed(self):
        # 私用区 \uE000-\uF8FF
        assert sanitize_text("\ue000x") == "x"

    def test_empty_returns_empty(self):
        assert sanitize_text("") == ""

    def test_nfkc_normalization(self):
        # NFKC: 全角 → 半角
        assert sanitize_text("ＡＢＣ") == "ABC"


class TestSanitizeObj:
    def test_dict_values_sanitized(self):
        obj = {"name": "表\u200B单", "fields": [{"title": "字\u202e段"}]}
        result = sanitize_obj(obj)
        assert result["name"] == "表单"
        assert result["fields"][0]["title"] == "字段"

    def test_nested_dict(self):
        obj = {"a": {"b": {"c": "x\u200By"}}}
        assert sanitize_obj(obj)["a"]["b"]["c"] == "xy"

    def test_list_in_dict(self):
        obj = {"items": ["a\u200b", "b", "c\u202e"]}
        result = sanitize_obj(obj)
        assert result["items"] == ["a", "b", "c"]

    def test_non_string_passthrough(self):
        obj = {"count": 42, "flag": True, "rate": 1.5}
        result = sanitize_obj(obj)
        assert result == {"count": 42, "flag": True, "rate": 1.5}

    def test_none_passthrough(self):
        assert sanitize_obj(None) is None
