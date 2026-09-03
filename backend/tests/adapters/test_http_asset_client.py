"""HttpAssetClient 测试 — 领域委托 + 通用传输数据操作 + sanitize。"""
import pytest
from unittest.mock import MagicMock

from adapters.http_asset_client import HttpAssetClient


def _make_modeler():
    """构造 mock 领域客户端（pack 注入的 ModelerAPI 形状）。"""
    m = MagicMock()
    m.list_templates.return_value = ["simple_form.json", "leave.json"]
    m.get_template.return_value = {"formName": "表\u200B单", "fields": []}
    m.get_schema.return_value = {"type": "object", "properties": {}}
    m.get_guide.return_value = {"title": "指\u202e南"}
    m.validate_form.return_value = {"valid": True, "errors": [], "warnings": []}
    m.create_form.return_value = {"success": True, "message": "ok"}
    m.update_form.return_value = {"success": True, "formCode": "leave"}
    return m


def _make_client():
    """HttpAssetClient + 注入 mock 领域客户端（upstream 用 mock 传输占位）。"""
    client = HttpAssetClient(upstream=MagicMock())
    client.set_modeler_api(_make_modeler())
    return client


class TestHttpAssetClient:
    def test_list_templates_sanitized(self):
        assert "simple_form.json" in _make_client().list_templates()

    def test_get_template_sanitizes_zero_width(self):
        result = _make_client().get_template("simple_form")
        assert "\u200B" not in result["formName"]
        assert result["formName"] == "表单"

    def test_get_guide_sanitizes_bidi(self):
        result = _make_client().get_guide()
        assert "\u202e" not in result["title"]

    def test_get_schema(self):
        assert _make_client().get_schema("form-config") == {
            "type": "object", "properties": {}}

    def test_get_template_missing_returns_empty(self):
        client = _make_client()
        client._modeler.get_template.return_value = None
        assert client.get_template("nope") == {}

    def test_validate_artifact_normalizes_response(self):
        assert _make_client().validate_artifact({"a": 1}, mode="create")["valid"] is True

    def test_validate_artifact_mode_uppercased(self):
        """mode 统一大写（上游 CREATE/UPDATE 枚举）。"""
        client = _make_client()
        client.validate_artifact({"a": 1}, mode="update")
        client._modeler.validate_form.assert_called_once_with({"a": 1}, mode="UPDATE")

    def test_persist_artifact_create(self):
        assert _make_client().persist_artifact({"formCode": "leave"}, mode="create")["success"] is True

    def test_persist_artifact_update(self):
        client = _make_client()
        result = client.persist_artifact({"formCode": "leave"}, mode="update")
        assert result["success"] is True
        client._modeler.update_form.assert_called_once_with("leave", {"formCode": "leave"})

    def test_persist_artifact_update_missing_code(self):
        with pytest.raises(ValueError):
            _make_client().persist_artifact({"formName": "无编码"}, mode="update")

    def test_guide_none_passthrough(self):
        """guide 拉取失败 None 透传（工具层据此追问用户刷新）。"""
        client = _make_client()
        client._modeler.get_guide.return_value = None
        assert client.get_guide() is None

    def test_config_ops_require_modeler(self):
        """未注入领域客户端时配置类操作显式报错（装配遗漏快速暴露）。"""
        client = HttpAssetClient(upstream=MagicMock())
        with pytest.raises(RuntimeError, match="set_modeler_api"):
            client.list_templates()


class TestDataOpsViaTransport:
    """数据操作按 service_name 经通用传输（地址解析/信封/日志归传输治理）。"""

    def test_submit_data_via_transport(self):
        client = HttpAssetClient(upstream=MagicMock())
        client._upstream.post.return_value = ({"pass": True}, None)
        result = client.submit_data(path="/api/leave/submit",
                                    data={"a": 1}, service_name="leave-system")
        client._upstream.post.assert_called_once_with(
            "leave-system", "/api/leave/submit", json_body={"a": 1}, auth=True)
        assert result["success"] is True  # pass 归一化为 success

    def test_submit_data_fail_closed(self):
        """传输失败（地址不可解析/网络/假200信封）→ success=False 不抛。"""
        from services.upstream_client import ServiceUnresolvableError
        client = HttpAssetClient(upstream=MagicMock())
        client._upstream.post.side_effect = ServiceUnresolvableError("无地址")
        result = client.submit_data(path="/x", data={}, service_name="leave-system")
        assert result["success"] is False

    def test_query_data_via_transport(self):
        client = HttpAssetClient(upstream=MagicMock())
        client._upstream.get.return_value = {"status": "approved"}
        result = client.query_data(path="/api/leave/status",
                                   service_name="leave-system", params={"q": 1})
        client._upstream.get.assert_called_once_with(
            "leave-system", "/api/leave/status", auth=True)
        assert result["status"] == "approved"
