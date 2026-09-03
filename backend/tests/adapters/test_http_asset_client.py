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
    m.update_form.return_value = {"success": True, "message": "ok"}
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
        """update 走 update_form（按 formCode 更新，不再 create 兜底另建记录）。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.persist_artifact({"formCode": "leave"}, mode="update")
        assert result["success"] is True
        client._upstream.update_form.assert_called_once_with(
            "leave", {"formCode": "leave"}
        )
        client._upstream.create_form.assert_not_called()

    def test_persist_artifact_update_missing_code(self):
        """update 但缺 formCode：编程错误直接抛（Fail-Fast），不静默兜底。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        with pytest.raises(ValueError):
            client.persist_artifact({"formName": "无编码"}, mode="update")


class TestDataOpsServiceAddressing:
    """数据操作按 service_name 寻址（与配置类操作同一套 resolve_base）。"""

    def test_submit_data_resolves_base_via_service_name(self, monkeypatch):
        """URL = resolve_base(service_name) + path；resolve 异常走 Fail-Closed。"""
        from services.upstream_client import ServiceUnresolvableError

        client = HttpAssetClient(upstream=_make_mock_upstream())
        client._upstream.resolve_base.return_value = "http://leave-svc:19999"

        captured = {}

        def _fake_post(url, **kwargs):
            captured["url"] = url

            class _R:
                def json(self):
                    return {"pass": True}

            return _R()

        monkeypatch.setattr("adapters.http_asset_client.httpx.post", _fake_post)
        result = client.submit_data(
            path="/api/leave/submit", data={"a": 1}, service_name="leave-system",
        )
        assert captured["url"] == "http://leave-svc:19999/api/leave/submit"
        client._upstream.resolve_base.assert_called_once_with("leave-system")
        # 上游返回 pass 归一化为 success
        assert result["success"] is True

        # 地址不可解析 → Fail-Closed 返回 success=False,不向上抛
        client._upstream.resolve_base.side_effect = ServiceUnresolvableError("no base")
        failed = client.submit_data(
            path="/api/leave/submit", data={}, service_name="leave-system",
        )
        assert failed["success"] is False

    def test_query_data_resolves_base_via_service_name(self, monkeypatch):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        client._upstream.resolve_base.return_value = "http://leave-svc:19999"

        captured = {}

        def _fake_get(url, **kwargs):
            captured["url"] = url

            class _R:
                def json(self):
                    return {"status": "approved"}

            return _R()

        monkeypatch.setattr("adapters.http_asset_client.httpx.get", _fake_get)
        result = client.query_data(
            path="/api/leave/status", service_name="leave-system",
            params={"query": "张三"},
        )
        assert captured["url"] == "http://leave-svc:19999/api/leave/status"
        assert result["status"] == "approved"
