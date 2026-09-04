"""传输层「假 200 业务信封」+ 领域客户端凭证策略测试（生产事故回归）。

njmind 网关对 MCP 资产匿名放行、拿着无效凭证/无端点权限的凭证直接拒
（HTTP 200 + {code:403/500, msg}）。分层后的职责：
  - 传输层：信封识别（GET→None 不缓存；POST→(None, msg)）；
  - 领域客户端（pack 代码控制，不走配置）：静态资产匿名（auth=False）、
    业务端点透传（auth=True）；validate 收信封转「校验未执行」Fail-Closed。
"""
from unittest.mock import MagicMock

import pytest

from services.upstream_client import (
    UpstreamClient, UpstreamConfig,
    set_request_services, set_forward_headers,
)
from domains.njmind_form.upstream import ModelerAPI, SERVICE_NAME
from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.tools.modify_form import ModifyFormTool
from sdk.tool import ClarificationRaised


class _Resp:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _bind_service():
    """传输按宿主表解析地址，测试线程先绑定再清。"""
    set_request_services({SERVICE_NAME: "http://test:1"})
    set_forward_headers(None)
    yield
    set_request_services(None)
    set_forward_headers(None)


def _transport():
    c = UpstreamClient(config=UpstreamConfig())
    c._client = MagicMock()
    return c


class TestEnvelopeHelper:
    def test_helper(self):
        assert UpstreamClient._envelope_error_msg(
            {"code": 403, "data": None, "msg": "没有该操作权限"}) == "没有该操作权限"
        assert UpstreamClient._envelope_error_msg(
            {"code": 500, "data": None, "msg": "功能异常"}) == "功能异常"
        # 成功信封与普通内容都不是错误
        assert UpstreamClient._envelope_error_msg({"code": 200, "msg": ""}) is None
        assert UpstreamClient._envelope_error_msg({"fieldTypes": []}) is None


class TestTransportEnvelope:
    def test_get_envelope_returns_none_and_not_cached(self):
        """GET 收信封 → None；不进缓存（换有效响应后下一次能拿到真数据）。"""
        c = _transport()
        c._client.get.side_effect = [
            _Resp({"code": 403, "data": None, "msg": "没有该操作权限"}),
            _Resp({"fieldTypes": [{"code": 4}]}),
        ]
        assert c.get(SERVICE_NAME, "/g", auth=False) is None
        guide = c.get(SERVICE_NAME, "/g", auth=False, cache=True)
        assert guide and guide["fieldTypes"][0]["code"] == 4

    def test_post_envelope_returns_error(self):
        """POST 收信封 → (None, msg)，调用方决定业务语义。"""
        c = _transport()
        c._client.post.return_value = _Resp({"code": 403, "data": None, "msg": "没有该操作权限"})
        data, err = c.post(SERVICE_NAME, "/v", json_body={}, auth=True)
        assert data is None
        assert "没有该操作权限" in err


class TestModelerCredentialsPolicy:
    """凭证策略由 pack 代码控制：静态资产匿名、业务端点透传。"""

    def test_static_assets_anonymous(self):
        c = _transport()
        c._client.get.return_value = _Resp({"fieldTypes": []})
        ModelerAPI(c).get_guide()
        assert c._client.get.call_args.kwargs.get("headers") is None

    def test_all_endpoints_anonymous(self):
        """全端点匿名（部署事实：mcp 端点族带用户身份反而 403）。"""
        c = _transport()
        set_forward_headers({"Authorization": "Bearer x"})
        c._client.post.return_value = _Resp({"pass": True, "errors": []})
        ModelerAPI(c).validate_artifact({"formName": "t"}, mode="CREATE")
        assert c._client.post.call_args.kwargs.get("headers") is None

    def test_validate_envelope_fail_closed(self):
        """validate 收信封：校验未执行，Fail-Closed 且指引用户刷新。"""
        c = _transport()
        c._client.post.return_value = _Resp({"code": 403, "data": None, "msg": "没有该操作权限"})
        result = ModelerAPI(c).validate_artifact({"formName": "t"}, mode="CREATE")
        assert result["valid"] is False
        assert "上游校验未执行" in result["errors"][0]["message"]
        assert "刷新设计器" in result["errors"][0]["message"]

    def test_paths_from_manifest(self):
        """端点路径来自 config.yaml paths 表（不再硬编码在传输层）。"""
        c = _transport()
        c._client.get.return_value = _Resp([])
        ModelerAPI(c).list_templates()
        url = c._client.get.call_args.args[0]
        assert url.endswith("/api/mcp/templates/list-templates")


def _ctx(asset):
    from sdk.tool import ToolContext
    return ToolContext(llm_client=None, asset_client=asset,
                       conversation=None, emit=lambda *a, **k: None)


class _AssetNone:
    def get_guide(self):
        return None


class TestToolsClarifyOnGuideFailure:
    @pytest.mark.parametrize("tool_cls", [CreateFormTool, ModifyFormTool])
    def test_none_guide_raises_clarification(self, tool_cls):
        """guide 拉取失败（信封/服务不可用）→ 追问用户刷新，不盲跑管线。"""
        tool = tool_cls()
        with pytest.raises(ClarificationRaised) as ei:
            tool._step_fetch_guide({}, _ctx(_AssetNone()))
        assert "刷新设计器" in ei.value.questions[0]
