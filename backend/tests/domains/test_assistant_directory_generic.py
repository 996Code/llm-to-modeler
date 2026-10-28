"""通用客服目录与配置边界，不依赖计量业务种子。"""
from types import SimpleNamespace

import pytest

from domains.metrology_assistant import capability_import as ci
from domains.metrology_assistant import scenario_config as config


def _server(code, enabled=True, tools=None):
    return {"code": code, "enabled": enabled, "status": "connected",
            "tools_snapshot": tools if tools is not None else [{
                "name": "lookup", "description": f"查询{code}平台的业务记录",
                "inputSchema": {"type": "object", "properties": {
                    "name": {"type": "string", "description": f"{code}记录名称"}},
                    "required": ["name"]}}]}


@pytest.fixture
def directory(monkeypatch):
    monkeypatch.setattr(ci, "_refresh_stale_snapshots", lambda rows: None)
    monkeypatch.setattr(ci, "resolve_datasource_for_tool", lambda server, tool: server)


def test_same_tool_name_on_two_servers_has_two_capabilities(monkeypatch, directory):
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("sales"), _server("assets")])
    auto = ci.auto_scenarios({})
    assert set(auto) == {"sales::lookup", "assets::lookup"}
    assert {row["action"]["server"] for row in auto.values()} == {"sales", "assets"}


def test_override_only_suppresses_its_bound_server(monkeypatch, directory):
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("sales"), _server("assets")])
    table = {"saved": {"action": {"kind": "chatbi", "server": "sales", "tool": "lookup"}}}
    assert set(ci.auto_scenarios(table)) == {"assets::lookup"}


def test_disabled_and_failed_probe_servers_not_routable(monkeypatch, directory):
    failed = {**_server("broken"), "probe_status": "failed"}
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("off", False), failed])
    assert ci.auto_scenarios({}) == {}


@pytest.mark.parametrize("props", [{}, {"limit": {"type": "integer", "description": "最多返回条数"}}])
def test_no_required_parameters_do_not_invent_a_required_slot(monkeypatch, directory, props):
    decl = {"name": "list", "description": "列出当前平台的可用记录",
            "inputSchema": {"type": "object", "properties": props}}
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("catalog", tools=[decl])])
    auto = ci.auto_scenarios({})
    row = auto["catalog::list"]
    assert row["required"] == {}
    assert row["action"]["input_schema"] == decl["inputSchema"]


def test_wrong_fixed_datasource_is_not_a_fallback(monkeypatch):
    from domains.chatbi import datasources, runtime
    monkeypatch.setattr(runtime, "get_pack_db", lambda: None)
    monkeypatch.setattr(datasources, "list_datasources", lambda *a, **kw: [
        SimpleNamespace(id="fixed-a", db_type="mcp", host="assets", database="tool_a")])
    assert ci.resolve_datasource_for_tool("assets", "tool_b") is None


def test_bound_override_gets_current_schema_without_overwriting_hints(monkeypatch, directory):
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("sales")])
    table = {"custom": {"label": "人工话术", "required": {"extract_hint": "自定义"},
                        "action": {"kind": "chatbi", "server": "sales", "tool": "lookup",
                                   "input_schema": {"type": "object", "properties": {"old": {"type": "string"}}}}}}
    merged = ci.resolve_scenarios(table)
    assert merged["custom"]["label"] == "人工话术"
    assert merged["custom"]["required"]["extract_hint"] == "自定义"
    assert set(merged["custom"]["action"]["input_schema"]["properties"]) == {"name"}
    assert "old" in table["custom"]["action"]["input_schema"]["properties"]


def test_bound_override_is_removed_when_service_is_disabled(monkeypatch, directory):
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("sales", False)])
    table = {"custom": {"action": {"kind": "chatbi", "server": "sales", "tool": "lookup"}}}
    assert ci.resolve_scenarios(table) == {}


def test_overview_keeps_explicit_override_when_generic_sources_ambiguous(monkeypatch, directory):
    """两个通用源本会让自动解析失败;显式绑定的覆盖不应被误标 blocked。"""
    from domains.chatbi import datasources, runtime
    monkeypatch.setattr(runtime, "get_pack_db", lambda: None)
    monkeypatch.setattr(datasources, "list_datasources", lambda *a, **kw: [
        SimpleNamespace(id="one", name="源一", db_type="mcp", host="sales", database="", is_active=True),
        SimpleNamespace(id="two", name="源二", db_type="mcp", host="sales", database="", is_active=True)])
    monkeypatch.setattr(datasources, "get_datasource", lambda db, ds: (
        SimpleNamespace(id=ds, db_type="mcp", host="sales", database="", is_active=True)
        if ds in ("one", "two") else None))
    monkeypatch.setattr(ci, "_servers_rows", lambda: [_server("sales")])
    table = {"saved": {"action": {"kind": "chatbi", "server": "sales", "tool": "lookup",
                                  "datasource_id": "one"}}}
    monkeypatch.setattr(ci, "_scenario_bindings", lambda t: [("sales", "lookup")])
    from domains.metrology_assistant import scenario_config
    monkeypatch.setattr(scenario_config, "load_scenarios", lambda: table)
    row = next(c for c in ci.routing_overview()["capabilities"] if c["tool"] == "lookup")
    assert row["status"] == "polished"
    assert row["datasource"]["datasource_id"] == "one"


def test_ambiguous_datasources_fail_instead_of_picking_first(monkeypatch):
    from domains.chatbi import datasources, runtime
    monkeypatch.setattr(runtime, "get_pack_db", lambda: None)
    monkeypatch.setattr(datasources, "list_datasources", lambda *a, **kw: [
        SimpleNamespace(id="one", db_type="mcp", host="sales", database=""),
        SimpleNamespace(id="two", db_type="mcp", host="sales", database="")])
    assert ci.resolve_datasource_for_tool("sales", "lookup") is None


def _config_action(**changes):
    action = {"kind": "chatbi", "server": "sales", "tool": "lookup", "params": {},
              "input_schema": {"type": "object", "properties": {}}}
    action.update(changes)
    return {"scenarios": {"sales::lookup": {
        "label": "目录", "intent": "查询目录", "required": {}, "action": action}}}


@pytest.mark.parametrize("change", [
    {"input_schema": {"type": "object", "$ref": "https://example.com/schema"}},
    {"input_schema": {"type": "object", "properties": {}, "required": ["unknown"]}},
    {"card": "__getattribute__"},
    {"stage": "处理 {missing}"},
    {"notices": {"ok": {"title": "成功", "message": "伪成功"}}},
])
def test_publish_rejects_unexecutable_or_unsafe_contract(change):
    with pytest.raises(config.ConfigValidationError):
        config.validate_config(_config_action(**change))


def test_generic_knowledge_action_requires_an_explicit_source():
    doc = {"scenarios": {"help": {"label": "知识问答", "intent": "咨询知识", "required": {},
                                   "action": {"kind": "kg", "conclusion": "answer"}}}}
    with pytest.raises(config.ConfigValidationError, match="知识库"):
        config.validate_config(doc)


def test_fresh_install_has_no_business_scenarios():
    assert config.factory_default_config() == {"scenarios": {}}


def test_empty_overrides_and_schema_driven_zero_parameter_tool_validate():
    assert config.validate_config({"scenarios": {}}) == {"scenarios": {}}
    doc = {"scenarios": {"catalog::list": {
        "label": "目录记录", "intent": "列出目录", "required": {},
        "action": {"kind": "chatbi", "server": "catalog", "tool": "list",
                   "params": {}, "input_schema": {"type": "object", "properties": {}},
                   "card": "_table_card", "notices": {}}}}}
    assert config.validate_config(doc) == doc


def test_loaded_legacy_demo_keeps_its_explicit_business_semantics(monkeypatch):
    from domains.metrology_assistant import runtime, store
    from domains.metrology_assistant.legacy_demo import SCENARIOS
    runtime.reset_runtime_cache()
    original = config._jsonable({"scenarios": SCENARIOS})
    monkeypatch.setattr(runtime, "get_pack_db", lambda: None)
    monkeypatch.setattr(store, "current_revision", lambda db: 1)
    monkeypatch.setattr(store, "load_config", lambda db: {**original, "_revision": 1})
    try:
        loaded = config.load_scenarios()
        assert loaded["mandatory_check"]["action"]["conclusion"] == "mandatory"
        assert loaded["mandatory_check"]["_param_hints"]["instrument"] == "所问器具名称"
        assert "conclusion" not in original["scenarios"]["mandatory_check"]["action"]
    finally:
        runtime.reset_runtime_cache()


def test_database_outage_does_not_restore_demo(monkeypatch):
    from domains.metrology_assistant import runtime
    runtime.reset_runtime_cache()
    monkeypatch.setattr(runtime, "get_pack_db", lambda: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(RuntimeError, match="配置"):
        config.load_scenarios()


def test_database_outage_uses_last_valid_config_only(monkeypatch):
    from domains.metrology_assistant import runtime
    runtime.reset_runtime_cache()
    expected = {"policy": {"label": "已发布配置"}}
    runtime.set_cached_config(expected, 7)
    monkeypatch.setattr(runtime, "get_pack_db", lambda: (_ for _ in ()).throw(OSError("offline")))
    try:
        assert config.load_scenarios() == expected
    finally:
        runtime.reset_runtime_cache()


def _kg_doc(conclusion="answer", **action_extra):
    action = {"kind": "kg", "conclusion": conclusion, "kb": "资料库"}
    action.update(action_extra)
    return {"scenarios": {"help": {"label": "知识问答", "intent": "咨询", "required": {},
                                   "action": action}}}


def test_publish_requires_explicit_kb_for_every_kg_mode():
    no_kb = {"scenarios": {"help": {"label": "知识问答", "intent": "咨询", "required": {},
                                    "action": {"kind": "kg", "conclusion": "turnaround"}}}}
    with pytest.raises(config.ConfigValidationError, match="知识库"):
        config.validate_config(no_kb, strict=True)
    with pytest.raises(config.ConfigValidationError, match="知识库"):
        config.validate_config(no_kb)


def test_publish_strict_rejects_legacy_mode_without_query_template():
    with pytest.raises(config.ConfigValidationError, match="查询模板"):
        config.validate_config(_kg_doc(conclusion="turnaround"), strict=True)
    # 装载宽松档不拒绝(存量回退原文检索)
    assert config.validate_config(_kg_doc(conclusion="turnaround"))["scenarios"]


def test_legacy_mode_without_template_queries_user_input_not_domain_words():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = _kg_doc(conclusion="turnaround")["scenarios"]["help"]
    cfg["required"] = {"slot": "name", "header": "名称", "question": "名称?", "options": []}
    plan = prepare_plan(cfg, {"name": "某设备"}, "设备校准要多久")
    assert plan["query"] == "设备校准要多久"
    assert "强制检定" not in plan["query"]


def test_stored_turnaround_demo_gets_legacy_query_template(monkeypatch):
    from domains.metrology_assistant import runtime, store
    from domains.metrology_assistant.legacy_demo import SCENARIOS
    runtime.reset_runtime_cache()
    original = config._jsonable({"scenarios": SCENARIOS})
    monkeypatch.setattr(runtime, "get_pack_db", lambda: None)
    monkeypatch.setattr(store, "current_revision", lambda db: 1)
    monkeypatch.setattr(store, "load_config", lambda db: {**original, "_revision": 1})
    try:
        loaded = config.load_scenarios()
        assert loaded["turnaround"]["action"]["query_template"] == "{instrument} 检测 耗时 工作日"
        assert loaded["qualification"]["action"]["query_template"] == "{instrument} 检测 资质 承接范围"
    finally:
        runtime.reset_runtime_cache()
