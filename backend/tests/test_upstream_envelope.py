"""上游「假 200 业务信封」处理测试（生产事故：过期 token 拉到 403 信封）。

njmind 网关对 MCP 资产匿名放行、拿着无效凭证直接拒（HTTP 200 + 
{code:403/500, msg}）。此前只看状态码：错误信封被当内容缓存 300s 毒化
后续请求，validate 缺 errors 字段还被误判「校验通过」。
处理策略（按产品取舍）：明确失败——读接口返回 None → 工具追问用户
刷新重开；validate Fail-Closed 带原因；一律不进缓存。不做匿名重试
（过期就该让用户知道，静默自愈会掩盖会话失效）。
"""
from unittest.mock import MagicMock

import pytest

from services.upstream_client import (
    UpstreamClient, UpstreamConfig, set_request_services,
)
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
    """guide/validate 走 resolve_base（宿主表唯一来源），测试线程先绑定再清。"""
    set_request_services({"njmind-modeler": "http://test:1"})
    yield
    set_request_services(None)


def _client():
    c = UpstreamClient(config=UpstreamConfig())
    c._client = MagicMock()
    return c


class TestEnvelopeDetect:
    def test_helper(self):
        assert UpstreamClient._envelope_error_msg(
            {"code": 403, "data": None, "msg": "没有该操作权限"}) == "没有该操作权限"
        assert UpstreamClient._envelope_error_msg(
            {"code": 500, "data": None, "msg": "功能异常"}) == "功能异常"
        # 成功信封与普通内容都不是错误
        assert UpstreamClient._envelope_error_msg({"code": 200, "msg": ""}) is None
        assert UpstreamClient._envelope_error_msg({"fieldTypes": []}) is None


class TestGuideEnvelope:
    def test_envelope_returns_none_and_not_cached(self):
        """信封 → None；且不进缓存（换有效响应后下一次能拿到真数据）。"""
        c = _client()
        c._client.get.side_effect = [
            _Resp({"code": 403, "data": None, "msg": "没有该操作权限"}),
            _Resp({"fieldTypes": [{"code": 4, "name": "SELECT"}]}),
        ]
        assert c.get_guide() is None
        # 第二次：若信封被缓存，这里会返回 None；实际应重新请求拿到有效 guide
        guide = c.get_guide()
        assert guide and guide["fieldTypes"][0]["code"] == 4


class TestValidateEnvelope:
    def test_envelope_fail_closed(self):
        """validate 收到信封：校验未执行，必须 Fail-Closed 且给用户可读原因。"""
        c = _client()
        c._client.post.return_value = _Resp({"code": 403, "data": None, "msg": "没有该操作权限"})
        result = c.validate_form({"formName": "x"}, mode="CREATE")
        assert result["valid"] is False
        assert "上游校验未执行" in result["errors"][0]["message"]
        assert "刷新设计器" in result["errors"][0]["message"]


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
        """guide 拉取失败（过期 token/服务不可用）→ 追问用户刷新，不盲跑管线。"""
        tool = tool_cls()
        with pytest.raises(ClarificationRaised) as ei:
            tool._step_fetch_guide({}, _ctx(_AssetNone()))
        assert "刷新设计器" in ei.value.questions[0]


class TestStaticAssetsAnonymous:
    def test_guide_fetch_does_not_forward_credentials(self):
        """静态资产读取匿名：即使线程绑定了透传凭证也不携带。

        端点级权限管控下,有效 token 也会吃 403 信封;匿名走白名单放行
        (真实事故:同请求 guide 403 而 schemas/validate 全 200)。
        """
        from services.upstream_client import set_forward_headers
        set_forward_headers({"Authorization": "Bearer valid-but-no-endpoint-perm"})
        try:
            c = _client()
            c._client.get.return_value = _Resp({"fieldTypes": [{"code": 4}]})
            assert c.get_guide() is not None
            kwargs = c._client.get.call_args.kwargs
            assert kwargs.get("headers") is None, "静态资产请求不得携带透传凭证"
        finally:
            set_forward_headers(None)
