"""njmind_bpm upstream 封装测试：mock UpstreamClient 断言端点路径与 payload。"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def transport():
    t = MagicMock()
    t.get.return_value = {"data": {"pass": True}}
    t.post.return_value = ({"draft": {}}, None)
    return t


def make_api(transport):
    from domains.njmind_bpm.upstream import BpmModelerAPI
    return BpmModelerAPI(transport)


def test_service_name_from_manifest():
    from domains.njmind_bpm.upstream import SERVICE_NAME
    assert SERVICE_NAME == "njmind-modeler"


def test_get_guide_uses_manifest_path(transport):
    api = make_api(transport)
    api.get_guide()
    transport.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/guide", auth=False, cache=True)


def test_list_templates_no_cache(transport):
    api = make_api(transport)
    api.list_templates()
    transport.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/templates/list", auth=False)


def test_get_template_appends_json_suffix(transport):
    api = make_api(transport)
    api.get_template("bpm_process_leave")
    transport.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/templates/bpm_process_leave.json",
        auth=False, cache=True)


def test_get_schema_appends_schema_json_suffix(transport):
    api = make_api(transport)
    api.get_schema("bpm_process_draft")
    transport.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/schemas/bpm_process_draft.schema.json",
        auth=False, cache=True)


def test_parse_posts_bpmn_xml_payload(transport):
    api = make_api(transport)
    result = api.parse("<xml/>")
    transport.post.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/parse",
        json_body={"bpmnXml": "<xml/>"}, auth=False)
    assert result["draft"] == {}
    assert result["warnings"] == []


def test_render_posts_bare_draft_payload(transport):
    api = make_api(transport)
    draft = {"processKey": "leave", "topology": {"nodes": [], "edges": []}}
    api.render(draft)
    transport.post.assert_called_once_with(
        "njmind-modeler", "/api/mcp/bpm/render",
        json_body=draft, auth=False)


def test_parse_fail_closed_on_upstream_error(transport):
    transport.post.return_value = (None, "timeout")
    api = make_api(transport)
    result = api.parse("<xml/>")
    assert result["draft"] is None
    assert result["warnings"][0]["code"] == "PARSE_FAILED"


def test_render_fail_closed_on_upstream_error(transport):
    transport.post.return_value = (None, "503")
    api = make_api(transport)
    result = api.render({"processKey": "x"})
    assert result["bpmnXml"] is None
    assert result["validation"]["pass"] is False


def test_get_form_uses_form_endpoint(transport):
    api = make_api(transport)
    api.get_form("leave_form")
    transport.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/forms/leave_form", auth=False)


def test_missing_path_key_raises(transport):
    api = make_api(transport)
    api._paths = {}
    with pytest.raises(KeyError):
        api._path("render")
