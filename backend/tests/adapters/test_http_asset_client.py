"""HttpAssetClient 测试 — 委托 UpstreamClient + 返回前 sanitize。"""
import pytest
from unittest.mock import MagicMock

from adapters.http_asset_client import HttpAssetClient


def _make_mock_upstream():
    """构造 mock UpstreamClient。"""
    m = MagicMock()
    m.list_templates.return_value = ["simple_form.json", "leave.json"]
    m.get_template.return_value = {"formName": "表\u200B单", "fields": []}
    m.get_schema.return_value = {"type": "object", "properties": {}}
    m.get_guide.return_value = {"title": "指\u202e南"}
    m.validate_form.return_value = {"pass": True, "errors": [], "warnings": []}
    m.create_form.return_value = {"success": True, "message": "ok"}
    return m


class TestHttpAssetClient:
    def test_list_templates_sanitized(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.list_templates()
        assert "simple_form.json" in result

    def test_get_template_sanitizes_zero_width(self):
        """关键:返回前清除零宽字符。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_template("simple_form")
        # mock 返回含 \u200B,清洗后应消失
        assert "\u200B" not in result["formName"]
        assert result["formName"] == "表单"

    def test_get_guide_sanitizes_bidi(self):
        """清除方向反转字符。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_guide()
        assert "\u202e" not in result["title"]

    def test_get_schema(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_schema("form-config")
        assert "type" in result

    def test_get_template_missing_returns_empty(self):
        """上游返回 None → 返回空 dict(不崩)。"""
        upstream = _make_mock_upstream()
        upstream.get_template.return_value = None
        client = HttpAssetClient(upstream=upstream)
        assert client.get_template("nope") == {}

    def test_validate_artifact_normalizes_response(self):
        """validate 归一化:上游 {pass, errors, warnings} → {valid, errors, warnings}。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.validate_artifact({"formCode": "x"}, mode="create")
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_validate_artifact_invalid(self):
        upstream = _make_mock_upstream()
        upstream.validate_form.return_value = {
            "pass": False, "errors": ["缺字段\u200B"], "warnings": []
        }
        client = HttpAssetClient(upstream=upstream)
        result = client.validate_artifact({}, mode="create")
        assert result["valid"] is False
        assert result["errors"][0]["message"] == "缺字段"  # 清洗 + 归一化成 {message}

    def test_persist_artifact_create(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.persist_artifact({"formCode": "leave"}, mode="create")
        assert result["success"] is True
        client._upstream.create_form.assert_called_once()

    def test_persist_artifact_update(self):
        """update 走 create_form 兜底(现有 UpstreamClient 无 update_form,阶段 3 完善)。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.persist_artifact({"formCode": "leave"}, mode="update")
        assert result["success"] is True
