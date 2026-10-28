"""Generic MCP boundaries through the real SDK, registry and ChatBI kernel."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.types import CallToolResult, ListToolsResult, Tool

from domains.chatbi import controlled, datasources
from domains.chatbi.models import DataSourceInfo
from domains.chatbi.tools.ask_data import AskDataTool, reset_mcp_heal_breaker
from sdk import mcp_client
from sdk.mcp_draft import translate_input_schema
from sdk.tool import PackInvokeError
from services.mcp_registry import McpServerStore


def _info():
    return DataSourceInfo(id="boundary", name="Boundary", db_type="mcp",
                          host="boundary", port=0, database="")


def _register(monkeypatch, tools):
    monkeypatch.setattr(mcp_client, "list_remote_tools", lambda cfg: tools)
    store = McpServerStore()
    server = store.create_server("Boundary", "boundary", "http://example.invalid/mcp", {})
    monkeypatch.setattr(mcp_client, "_server_loader", store.get_by_code)
    return store, server


def _tool(name="query", **extra):
    return {"name": name, "description": "query", "inputSchema": {
        "type": "object", "properties": {}}, **extra}


def test_list_pagination_preserves_all_metadata(monkeypatch):
    declarations = [Tool(name="first", inputSchema={"type": "object"},
                         outputSchema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                         annotations={"readOnlyHint": True}, title="First", _meta={"vendor": "x"}),
                    Tool(name="second", inputSchema={"type": "object"})]

    class Session:
        def __init__(self):
            self.cursors = []

        async def list_tools(self, cursor=None):
            self.cursors.append(cursor)
            return ListToolsResult(tools=[declarations[0 if cursor is None else 1]],
                                   nextCursor="next" if cursor is None else None)

    session = Session()

    async def run(cfg, tool, args):
        return await mcp_client._invoke(session, tool, args)

    monkeypatch.setattr(mcp_client, "_run_session", run)
    result = mcp_client.list_remote_tools({"url": "unused"})
    assert session.cursors == [None, "next"]
    assert result == [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                      for t in declarations]


def test_repeated_page_cursor_fails_without_partial_snapshot(monkeypatch):
    class Session:
        async def list_tools(self, cursor=None):
            return ListToolsResult(tools=[], nextCursor="same")

    with pytest.raises(PackInvokeError):
        asyncio.run(mcp_client._invoke(Session(), None, None))


def test_typed_translation_keeps_raw_contract_and_enum_types():
    schema = {"type": "object", "properties": {
        "active": {"type": "boolean", "enum": [False, True]},
        "count": {"type": "number", "enum": [1, 2.5]},
        "filter": {"type": "object", "additionalProperties": False},
        "ids": {"type": "array", "items": {"type": "integer"}},
        "choice": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "mixed": {"type": ["string", "integer"], "enum": ["a", 2]},
    }}
    result = translate_input_schema(schema)
    slots = {s["name"]: s for s in result["slots"]}
    assert slots["active"]["slot_type"] == "boolean"
    assert slots["count"]["slot_type"] == "number"
    assert slots["active"]["enum_options"] == [False, True]
    assert slots["count"]["enum_options"] == [1, 2.5]
    assert all(slots[n]["slot_type"] == "json" for n in ("filter", "ids", "choice", "mixed"))
    assert result["raw_schema"] == schema
    assert slots["filter"]["raw_schema"] == schema["properties"]["filter"]


@pytest.mark.parametrize("schema,args", [
    ({"type": "object", "additionalProperties": False}, {"extra": 1}),
    ({"properties": {"n": {"type": "number", "minimum": 2}}}, {"n": 1}),
    ({"properties": {"n": {"type": "number", "multipleOf": 2}}}, {"n": 3}),
    ({"properties": {"x": {"type": ["string", "null"]}}}, {"x": 3}),
    ({"properties": {"x": {"type": "string"}}}, {"x": None}),
    ({"properties": {"x": {"enum": [1]}}}, {"x": True}),
    ({"properties": {"x": {"pattern": "^OK", "minLength": 3}}}, {"x": "no"}),
    ({"properties": {"x": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}}}, {"x": [1, 1]}),
    ({"properties": {"x": {"type": "array", "items": {"minimum": 2}}}}, {"x": [1]}),
    ({"oneOf": [{"required": ["a"]}, {"required": ["b"]}]}, {"a": 1, "b": 1}),
    ({"if": {"required": ["a"]}, "then": {"required": ["b"]}}, {"a": 1}),
    ({"$defs": {"n": {"type": "integer", "minimum": 2}}, "properties": {"n": {"$ref": "#/$defs/n"}}}, {"n": 1}),
    ({"type": "not-a-json-type"}, {}),
    (False, {}),
    ({"$defs": {"unused": {"$ref": "https://example.invalid/schema"}}}, {}),
    ({"$dynamicRef": "https://example.invalid/schema"}, {}),
])
def test_full_local_schema_validation_rejects(schema, args):
    assert controlled.validate_against_schema(args, schema)


def test_local_refs_nullable_empty_and_typed_values_pass():
    schema = {"$defs": {"n": {"type": "integer", "minimum": 2}},
              "properties": {"n": {"$ref": "#/$defs/n"}, "empty": {"type": "array"},
                             "flag": {"type": "boolean"}, "value": {"type": ["null", "number"]}},
              "required": ["empty", "flag", "value"]}
    assert controlled.validate_against_schema({"n": 2, "empty": [], "flag": False, "value": None}, schema) is None


def test_failed_probe_keeps_snapshot_but_blocks_execution(monkeypatch):
    tools = [_tool(outputSchema={"type": "object"}, annotations={"readOnlyHint": True})]
    store, server = _register(monkeypatch, tools)

    def fail(cfg):
        raise PackInvokeError("probe unavailable", PackInvokeError.RETRYABLE)

    monkeypatch.setattr(mcp_client, "list_remote_tools", fail)
    failed = store.apply_probe(server["id"])
    assert failed["tools_snapshot"] == tools
    assert failed["status"] == "pending"
    assert failed["probe_status"] == "failed"
    assert "probe unavailable" in failed["probe_error"]
    calls = []
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: calls.append(args))
    result = datasources.execute_mcp_tool(_info(), {"tool": "query", "args": {}})
    assert not result.ok and not calls
    with pytest.raises(PackInvokeError):
        mcp_client._load_server("boundary")
    monkeypatch.setattr(mcp_client, "list_remote_tools", lambda cfg: tools)
    recovered = store.apply_probe(server["id"])
    assert recovered["probe_status"] == "connected" and recovered["probe_error"] == ""


def test_legacy_connected_server_without_probe_status_remains_usable(monkeypatch):
    cfg = {"status": "connected", "enabled": True, "url": "unused"}
    monkeypatch.setattr(mcp_client, "_server_loader", lambda code: cfg)
    assert mcp_client._load_server("legacy") == cfg


def test_schema_rejection_happens_before_transport(monkeypatch):
    _register(monkeypatch, [_tool(inputSchema={"type": "object", "additionalProperties": False})])
    calls = []
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: calls.append(args))
    result = datasources.execute_mcp_tool(_info(), {"tool": "query", "args": {"extra": True}})
    assert not result.ok and not calls


@pytest.mark.parametrize("payload,columns,rows", [
    ([{"a": 1}, {"b": 2, "a": 3}], ["a", "b"], [(1, None), (3, 2)]),
    ({"columns": ["a", "b"], "rows": [{"b": 2, "a": 1}]}, ["a", "b"], [(1, 2)]),
    ({"status": "ok", "data": None}, [], []),
    (None, [], []),
    ({}, [], []),
])
def test_tabular_union_mapping_and_empty(payload, columns, rows, monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: payload)
    result = datasources.execute_mcp_tool(_info(), {"tool": "query", "args": {}})
    assert result.ok
    assert result.columns == columns and result.rows == rows


@pytest.mark.parametrize("payload", [
    {"columns": ["a", "b"], "rows": [[1]]},
    {"columns": ["a"], "rows": [[1, 2]]},
    {"columns": ["a", "a"], "rows": [[1, 2]]},
    {"columns": ["a"], "rows": [{"a": 1, "b": 2}]},
    {"columns": ["a"]},
])
def test_bad_table_contract_fails(payload, monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: payload)
    assert not datasources.execute_mcp_tool(_info(), {"tool": "query", "args": {}}).ok


def test_empty_protocol_result_is_empty_table(monkeypatch):
    async def run(*args):
        return CallToolResult(content=[])
    monkeypatch.setattr(mcp_client, "_run_session", run)
    payload = mcp_client.call_remote_tool({"url": "unused"}, "query", {})
    assert controlled._normalize_tabular(payload) == {"columns": [], "rows": []}


@pytest.mark.parametrize("status,expected", [(401, "credential"), (403, "permanent")])
def test_http_auth_error_classification_inside_exception_groups(monkeypatch, status, expected):
    async def run(*args):
        response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid/mcp"))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ExceptionGroup("transport task", [exc])
    monkeypatch.setattr(mcp_client, "_run_session", run)
    with pytest.raises(PackInvokeError) as caught:
        mcp_client.call_remote_tool({"url": "unused"}, "query", {})
    assert caught.value.error_type == expected


def _ask_state():
    return {"ds": _info(), "content": SimpleNamespace(models=[SimpleNamespace(name="query"), SimpleNamespace(name="write")]),
            "current_tables": ["query"], "sql": json.dumps({"tool": "query", "args": {}})}


def _ctx():
    return SimpleNamespace(conv_id=None, session_state=None, emit=lambda *a, **kw: None,
                           trace=lambda *a, **kw: None)


@pytest.mark.parametrize("payload", [
    {"status": "forbidden", "message": "denied"},
    PackInvokeError("expired", PackInvokeError.CREDENTIAL),
    PackInvokeError("HTTP access denied", PackInvokeError.PERMANENT),
])
def test_ask_data_auth_denials_never_heal(payload, monkeypatch):
    _register(monkeypatch, [_tool(annotations={"readOnlyHint": True})])
    calls, heals = [], []

    def call(*args):
        calls.append(args)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(mcp_client, "call_tool", call)
    llm = SimpleNamespace(chat_json=lambda **kw: heals.append(kw) or {"tool": "query", "args": {}})
    reset_mcp_heal_breaker()
    state = _ask_state()
    AskDataTool()._execute_mcp(state, _ctx(), llm, {}, state["ds"], "schema")
    assert state.get("_error") and len(calls) == 1 and not heals


def test_write_tool_never_retried_and_read_heal_cannot_switch_to_write(monkeypatch):
    _register(monkeypatch, [_tool(annotations={"readOnlyHint": True}),
                            _tool("write", annotations={"readOnlyHint": False})])
    calls, heals = [], []

    def fail(*args):
        calls.append(args)
        return {"status": "invalid_input", "message": "bad args"}

    monkeypatch.setattr(mcp_client, "call_tool", fail)
    llm = SimpleNamespace(chat_json=lambda **kw: heals.append(kw) or {"tool": "write", "args": {}})
    state = _ask_state()
    state["sql"] = json.dumps({"tool": "write", "args": {}})
    reset_mcp_heal_breaker()
    AskDataTool()._execute_mcp(state, _ctx(), llm, {}, state["ds"], "schema")
    assert len(calls) == 1 and not heals
    calls.clear()
    state = _ask_state()
    AskDataTool()._execute_mcp(state, _ctx(), llm, {}, state["ds"], "schema")
    assert len(calls) == 1 and heals and state.get("_error")


def test_normal_empty_mcp_result_does_not_request_clarification():
    state = _ask_state()
    state["execute_result"] = datasources.ExecuteResult(True, columns=[], rows=[], rowcount=0)
    AskDataTool(settings={"sql_max_rows": 100})._step_check_result(state, _ctx())
    assert not state.get("_need_clarify") and not state.get("_result")
    assert state["check"].ok


def test_empty_success_envelope_requires_data_key(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", lambda *args: {"status": "ok", "message": "done"})
    assert not datasources.execute_mcp_tool(_info(), {"tool": "query", "args": {}}).ok


def test_ask_generation_receives_raw_schema_and_annotations(monkeypatch):
    schema = {"type": "object", "properties": {"filter": {"type": "object", "properties": {"n": {"minimum": 7}}}}}
    declaration = _tool(inputSchema=schema, outputSchema={"type": "object"}, annotations={"readOnlyHint": True})
    _register(monkeypatch, [declaration])
    monkeypatch.setattr("domains.chatbi.stores.get_vector", lambda *args: (_ for _ in ()).throw(RuntimeError("no vector")))
    monkeypatch.setattr("domains.chatbi.memory.recall_text", lambda *a, **kw: "")
    captured = []
    llm = SimpleNamespace(chat_json=lambda **kw: captured.append(kw) or {"tool": "query", "args": {}})
    state = _ask_state()
    AskDataTool(db=object())._generate_mcp_tool_call(
        state, _ctx(), llm, {}, state["ds"], state["content"], "query", "semantic schema")
    prompt = captured[0]["messages"][-1]["content"]
    assert "semantic schema" in prompt
    assert json.dumps(schema, ensure_ascii=False) in prompt
    assert "readOnlyHint" in prompt and "outputSchema" in prompt
