"""ChatBI 受控执行服务(execute_controlled)单测。

核心回归点(设计 4.4,离线探针实证的缺口):
- 业务信封 forbidden/not_found 不再变 ok=True 成功行;
- PackInvokeError 错误类型(credential/retryable)不抹平;
- 前置校验失败时不发 MCP 请求;
- 纯记录含普通 status 字段不误判为业务信封。
"""
import pytest

from domains.chatbi import controlled
from domains.chatbi.models import DataSourceInfo
from sdk.tool import PackInvokeError


def _info(**kw):
    base = dict(id="ds-1", name="计量业务平台", db_type="mcp",
                host="metrology", port=0, database="query_progress",
                is_active=True)
    base.update(kw)
    return DataSourceInfo(**base)


@pytest.fixture
def wiring(monkeypatch):
    """替身装配: 数据源解析 + SDK 调用点(不发真实网络)。"""
    calls = {"mcp": []}

    def fake_get_datasource(db, ds_id, decrypt=False):
        return wiring.info

    def fake_call_tool(code, tool, args):
        calls["mcp"].append((code, tool, dict(args)))
        return wiring.payload

    monkeypatch.setattr(
        "domains.chatbi.datasources.get_datasource", fake_get_datasource)
    monkeypatch.setattr("domains.chatbi.runtime.get_pack_db",
                        lambda: object())
    monkeypatch.setattr("sdk.mcp_client.call_tool", fake_call_tool)
    wiring.info = _info()
    wiring.payload = {}
    wiring.calls = calls
    return wiring


def test_forbidden_envelope_is_not_success(wiring):
    """上游 forbidden 信封 → ok=False + status=forbidden(不变成功表格)。"""
    wiring.payload = {"status": "forbidden", "message": "该委托单不属于当前主体"}
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ8888"})
    assert r.ok is False
    assert r.status == "forbidden"
    assert r.error.error_type == "permanent"
    assert r.error.retryable is False
    assert r.data is None


def test_not_found_envelope(wiring):
    wiring.payload = {"status": "not_found", "message": "未找到委托单 BJ9000"}
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ9000"})
    assert r.ok is False and r.status == "not_found"


def test_ok_envelope_record_normalized(wiring):
    wiring.payload = {"status": "ok", "data": {"order_no": "BJ2026",
                                               "status": "检测中"}}
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ2026"})
    assert r.ok is True
    assert r.data == {"record": {"order_no": "BJ2026", "status": "检测中"}}


def test_ok_envelope_tabular_normalized(wiring):
    wiring.payload = {"status": "ok", "data": {"columns": ["a"],
                                               "rows": [[1], [2]]}}
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is True
    assert r.data == {"columns": ["a"], "rows": [(1,), (2,)]}


def test_plain_record_with_status_field_not_envelope(wiring):
    """纯记录恰好含 status 字段(非信封载荷键) → 按记录归一,不误判。"""
    wiring.payload = {"order_no": "BJ2026", "status": "检测中"}
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ2026"})
    assert r.ok is True
    assert r.data == {"record": wiring.payload}


def test_non_envelope_tabular(wiring):
    wiring.payload = {"columns": ["city", "n"], "rows": [["北京", 3]]}
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is True and r.data["rows"] == [("北京", 3)]


def test_credential_error_type_preserved(wiring, monkeypatch):
    """PackInvokeError(credential) → code=credential,类型不抹平。"""
    def raise_cred(code, tool, args):
        raise PackInvokeError("server key 无效", error_type="credential")
    monkeypatch.setattr("sdk.mcp_client.call_tool", raise_cred)
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is False
    assert r.status == "credential"
    assert r.error.error_type == "credential"


def test_retryable_timeout_classification(wiring, monkeypatch):
    def raise_timeout(code, tool, args):
        raise PackInvokeError("timeout", error_type="retryable")
    monkeypatch.setattr("sdk.mcp_client.call_tool", raise_timeout)
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is False
    assert r.status == "unavailable"
    assert r.error.retryable is True


def test_preflight_failures_send_no_mcp(wiring):
    """前置校验失败 → 不发 MCP 请求(调用点计数为零)。"""
    wiring.info = _info(db_type="mysql")
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is False and r.status == "invalid_input"
    assert wiring.calls["mcp"] == []

    wiring.info = _info(is_active=False)
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is False and r.status == "unavailable"
    assert wiring.calls["mcp"] == []

    wiring.info = _info(database="other_tool")
    r = controlled.execute_controlled(None, "ds-1", "query_progress", {})
    assert r.ok is False and r.status == "invalid_input"
    assert wiring.calls["mcp"] == []


def test_datasource_missing(wiring, monkeypatch):
    monkeypatch.setattr("domains.chatbi.datasources.get_datasource",
                        lambda db, ds_id, decrypt=False: None)
    r = controlled.execute_controlled(None, "nope", "query_progress", {})
    assert r.ok is False and r.status == "invalid_input"


def test_unknown_envelope_status_is_contract_error(wiring):
    """信封形状但未知状态枚举 → invalid_input 契约错误,不当成功记录
    (设计 4.4 步3/4;13.2 P1 必测项)。"""
    wiring.payload = {"status": "processing", "message": "处理中"}
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ2026"})
    assert r.ok is False
    assert r.status == "invalid_input"
    assert "processing" in r.error.message


# ── inputSchema 验参(P1/设计 4.3:完整 Schema 校验,不过不发) ──────

from domains.chatbi.controlled import validate_against_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "order_no": {"type": "string", "description": "单号"},
        "count": {"type": "integer"},
        "state": {"type": "string", "enum": ["open", "done"]},
        "filter": {"type": "object", "properties": {"kb": {"type": "string"}},
                   "required": ["kb"]},
    },
    "required": ["order_no"],
}


def test_schema_pass_and_required():
    assert validate_against_schema({"order_no": "BJ1"}, SCHEMA) is None
    assert "缺少必填参数" in validate_against_schema({"count": 1}, SCHEMA)
    assert "缺少必填参数" in validate_against_schema({"order_no": " "}, SCHEMA)


def test_schema_type_and_enum():
    assert "integer" in validate_against_schema(
        {"order_no": "a", "count": 1.5}, SCHEMA)
    assert validate_against_schema(
        {"order_no": "a", "count": 3}, SCHEMA) is None
    # bool 不是 integer(Python 子类陷阱)
    assert "integer" in validate_against_schema(
        {"order_no": "a", "count": True}, SCHEMA)
    assert "必须是" in validate_against_schema(
        {"order_no": "a", "state": "paused"}, SCHEMA)
    assert validate_against_schema(
        {"order_no": "a", "state": "open"}, SCHEMA) is None


def test_schema_nested_required():
    err = validate_against_schema(
        {"order_no": "a", "filter": {"other": 1}}, SCHEMA)
    assert "缺少必填参数 kb" in err
    assert validate_against_schema(
        {"order_no": "a", "filter": {"kb": "x"}}, SCHEMA) is None


def test_schema_none_passes_and_unknown_props_ignored():
    assert validate_against_schema({"x": 1}, None) is None
    assert validate_against_schema(
        {"order_no": "a", "undeclared": object()}, SCHEMA) is None


def test_snapshot_missing_tool_reports_refresh(wiring, monkeypatch):
    """快照一致性(设计 0.5.1 降级版):server 在册但快照无此工具 →
    明确报错提示刷新,不模糊失败在传输层。"""
    class _FakeStore:
        def get_by_code(self, code):
            # 快照已刷新:query_progress 已下线,只剩 query_instrument
            return {"code": code, "tools_snapshot": [
                {"name": "query_instrument",
                 "inputSchema": {"type": "object",
                                 "properties": {"name": {"type": "string"}},
                                 "required": ["name"]}}]}

    monkeypatch.setattr("services.mcp_registry.McpServerStore", _FakeStore)
    # 白名单放行(数据源限定=query_progress)但快照已无它 → 报错刷新
    r = controlled.execute_controlled(None, "ds-1", "query_progress",
                                      {"order_no": "BJ1"})
    # 换无白名单数据源验"快照内工具正常走"
    wiring.info = _info(database="")
    assert r.ok is False and r.status == "invalid_input"
    assert "快照" in r.error.message and "query_instrument" in r.error.message
    # 快照内工具正常走(按快照 schema 验参)
    wiring.payload = {"columns": ["a"], "rows": [["b"]]}
    r2 = controlled.execute_controlled(None, "ds-1", "query_instrument",
                                       {"name": "压力表"})
    assert r2.ok is True
