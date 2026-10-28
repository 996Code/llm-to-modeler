"""metrology 场景配置管理 API 测试(P1 管理页数据面)。

覆盖:GET 默认、整档直存、校验拒绝(400)、历史数据保留、保存后
热生效、执行器使用 DB 场景表。

跑法:backend 目录 .venv/bin/python -m pytest tests/domains/ -q
(conftest 建表+每测试清 metrology_assistant schema)。
"""
import importlib

import pytest


@pytest.fixture()
def client(monkeypatch):
    """管理端 TestClient(ADMIN_TOKEN 清空=放行;conftest 注册同款)。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domains.metrology_assistant.pack import create_api_router
    app = FastAPI()
    app.include_router(create_api_router(), prefix="/api/packs/metrology_assistant")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_pack(monkeypatch):
    """每测试清 metrology 单例与缓存(表由 conftest 清)。

    根 .env 的 METROLOGY_PROGRESS_DATASOURCE_ID 会随 main.py 的
    load_dotenv 进入进程环境,全量跑时把 legacy progress 的数据源
    解析成真实 id→强校验 None→blocked,单跑(不加载 .env)才过——
    在此显式清掉,根治"全量偶发"。
    """
    monkeypatch.delenv("METROLOGY_PROGRESS_DATASOURCE_ID", raising=False)
    monkeypatch.delenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", raising=False)
    from domains.metrology_assistant import runtime
    runtime.reset_runtime_cache()
    yield
    runtime.reset_runtime_cache()


def _db():
    from domains.metrology_assistant import runtime
    return runtime.get_pack_db()


@pytest.fixture()
def legacy_demo_config(_fresh_pack, monkeypatch):
    """Opt in to stored demo configuration; empty installations stay empty."""
    from domains.metrology_assistant import store
    from domains.metrology_assistant.legacy_demo import SCENARIOS
    from domains.metrology_assistant.scenario_config import _jsonable
    from services.mcp_registry import McpServerStore

    declarations = [
        {"name": "query_progress", "description": "查询委托单检测进度", "inputSchema": {
            "type": "object", "properties": {"order_no": {
                "type": "string", "description": "委托单号"}}, "required": ["order_no"]}},
        {"name": "query_instrument", "description": "查询器具检测价目信息", "inputSchema": {
            "type": "object", "properties": {"name": {
                "type": "string", "description": "器具名称"}}, "required": ["name"]}},
    ]
    monkeypatch.setattr("sdk.mcp_client.list_remote_tools", lambda config: declarations)
    McpServerStore().create_server("计量演示平台", "metrology", "http://demo.invalid/mcp", {})

    content = _jsonable({"scenarios": SCENARIOS})
    for key in ("progress", "instrument_info"):
        content["scenarios"][key]["action"]["server"] = "metrology"
    store.seed_config_if_absent(_db(), content)
    return content


def test_get_config_seeds_factory_default(client):
    """Empty install: GET returns an empty factory config without persisting demo data."""
    from domains.metrology_assistant import store
    from domains.metrology_assistant.scenario_config import factory_default_config

    assert factory_default_config() == {"scenarios": {}}
    resp = client.get("/api/packs/metrology_assistant/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 0
    assert body["factory"] is True
    assert body["content"] == {"scenarios": {}}
    assert store.load_config(_db()) is None


def test_empty_config_can_be_saved_without_demo_seed(client):
    from domains.metrology_assistant.scenario_config import load_scenarios

    response = client.put("/api/packs/metrology_assistant/config", json={
        "content": {"scenarios": {}}})
    assert response.status_code == 200
    body = client.get("/api/packs/metrology_assistant/config").json()
    assert body["content"] == {"scenarios": {}}
    assert body["factory"] is False
    assert load_scenarios() == {}


def test_save_config_direct(client, legacy_demo_config):
    """整档直存(无 CAS):revision 服务端自增;重复保存各自生效。"""
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "请提供单号(改)"
    resp = client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "comment": "改追问"})
    assert resp.status_code == 200
    assert resp.json()["revision"] == current["revision"] + 1
    content["scenarios"]["progress"]["required"]["question"] = "再次保存"
    resp2 = client.put("/api/packs/metrology_assistant/config", json={
        "content": content})
    assert resp2.status_code == 200
    assert resp2.json()["revision"] == resp.json()["revision"] + 1
    after = client.get("/api/packs/metrology_assistant/config").json()
    assert after["content"] == content
    assert after["factory"] is False


def test_config_save_preserves_existing_history(client):
    from domains.metrology_assistant import store
    from domains.metrology_assistant.scenario_config import factory_default_config

    db = _db()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO assistant_config_history "
            "(id, scope, revision, content, updated_by, updated_at, comment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("snapshot-1", "global", 1, '{"scenarios": {}}',
             "admin", "2026-01-01T00:00:00", "snapshot"))
        before = [dict(row) for row in conn.execute(
            "SELECT * FROM assistant_config_history").fetchall()]
    assert store.seed_config_if_absent(db, factory_default_config()) == 1
    content = client.get("/api/packs/metrology_assistant/config").json()["content"]
    resp = client.put("/api/packs/metrology_assistant/config", json={"content": content})
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    with db.connect() as conn:
        after = [dict(row) for row in conn.execute(
            "SELECT * FROM assistant_config_history").fetchall()]
    assert after == before


def test_config_history_and_rollback_routes_absent(client):
    base = "/api/packs/metrology_assistant/config"
    assert client.get(f"{base}/history").status_code == 404
    assert client.get(f"{base}/history/1").status_code == 404
    assert client.post(f"{base}/rollback", json={"to_revision": 1}).status_code == 404
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "expected_revision" not in schemas["ConfigSaveBody"]["properties"]
    assert {"server", "tool"} <= schemas["CapabilityDraftBody"]["properties"].keys()
    assert "text" in schemas["DryRunBody"]["properties"]


def test_save_config_validation_rejects_bad_shape(client):
    """坏结构 400:缺 required/action/kind 枚举。"""
    before = client.get("/api/packs/metrology_assistant/config").json()
    bad = {"scenarios": {"x": {"label": "X", "intent": "x"}}}
    resp = client.put("/api/packs/metrology_assistant/config", json={
        "content": bad})
    assert resp.status_code == 400
    assert "校验失败" in resp.json()["detail"]
    assert client.get("/api/packs/metrology_assistant/config").json() == before


def test_save_hot_reloads_runtime_scenarios(client, legacy_demo_config):
    """保存后执行器读到新配置(缓存失效,热生效)。"""
    from domains.metrology_assistant.scenario_config import load_scenarios
    client.get("/api/packs/metrology_assistant/config")  # 读取显式 demo seed
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "热生效文案?"
    client.put("/api/packs/metrology_assistant/config", json={
        "content": content})
    table = load_scenarios()
    assert table["progress"]["required"]["question"] == "热生效文案?"
    # AskOption 复原:执行器消费 SDK 类型
    from sdk.tool import AskOption
    assert isinstance(table["mandatory_check"]["required"]["options"][0],
                      AskOption)


def test_executor_uses_db_scenario_table(client, monkeypatch, legacy_demo_config):
    """端到端:管理页改追问文案 → 工具缺槽追问用新文案。"""
    class FakeMem:
        def __init__(self):
            self._d = {}
        def get(self, k, default=None):
            return self._d.get(k, default)
        def set(self, k, v):
            self._d[k] = v
        def pop(self, k, default=None):
            return self._d.pop(k, default)

    class FakeCtx:
        def __init__(self, llm, mem):
            self.llm_client = llm
            self.session_state = mem
            self.conv_id = "conv-cfg"
        def emit(self, event, *args):
            pass

    class FakeLLM:
        def __init__(self):
            self.calls = 0
        def chat_json(self, messages, temperature=0.0, conv_id=None,
                      stage=None):
            self.calls += 1
            return {"scenario": "progress", "instrument": "",
                    "purpose": "", "order_no": ""}

    client.get("/api/packs/metrology_assistant/config")  # 读取显式 demo seed
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "请给出单号(新)"
    client.put("/api/packs/metrology_assistant/config", json={
        "content": content})

    from domains.metrology_assistant.tools.assistant import MetrologyAssistantTool
    tool = MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(), FakeMem())
    result = tool.execute({"user_input": "查一下进度"}, ctx)
    assert result.ask is not None
    assert "请给出单号(新)" in result.ask.questions[0].question


# ── Tab3 联调预演(设计 §9)───────────────────────────────────

class _NoAskLLM:
    """预演用 LLM:固定提取结果。"""
    def __init__(self, extract):
        self._e = extract
    def chat_json(self, messages, temperature=0.0, conv_id=None, stage=None):
        return dict(self._e)


def _dry(monkeypatch, client, payload):
    from domains.metrology_assistant import config_api
    _orig = config_api._dry_run_once
    extract = payload.pop("_extract", {
        "scenario": "progress", "instrument": "",
        "purpose": "", "order_no": ""})
    monkeypatch.setattr(config_api, "_dry_run_once",
                        lambda sc, llm, b: _orig(sc, _NoAskLLM(extract), b))
    return client.post("/api/packs/metrology_assistant/config/dry-run",
                       json=payload)


def test_dry_run_missing_slot_shows_will_ask(client, monkeypatch, legacy_demo_config):
    """缺单号:预演展示命中场景+缺口+将追问的原文(不发业务调用)。"""
    resp = _dry(monkeypatch, client, {"text": "查一下进度"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario"] == "progress"
    assert "will_ask" in body and "请提供委托单号" in body["will_ask"]["question"]
    assert "action" not in body


def test_dry_run_complete_shows_action(client, monkeypatch, legacy_demo_config):
    """槽位齐:展示将调用的数据源+工具+参数;不执行(execute=False)。"""
    resp = _dry(monkeypatch, client, {
        "text": "查 BJ2026 进度",
        "_extract": {"scenario": "progress", "instrument": "",
                     "purpose": "", "order_no": "BJ2026"}})
    body = resp.json()
    assert body["missing"] is False
    assert body["action"]["kind"] == "chatbi"
    assert body["action"]["tool"] == "query_progress"
    assert body["action"]["params"] == {"order_no": "BJ2026"}
    assert "executed" not in body  # 未勾真实执行


def test_dry_run_draft_config_diff(client, monkeypatch, legacy_demo_config):
    """草案配置:改追问文案后按草案求值(未保存不影响当前配置)。"""
    current = client.get("/api/packs/metrology_assistant/config").json()
    draft = current["content"]
    draft["scenarios"]["progress"]["required"]["question"] = "草案版文案?"
    resp = _dry(monkeypatch, client,
                {"text": "查一下进度", "config": draft})
    assert "草案版文案?" in resp.json()["will_ask"]["question"]
    # 当前配置未变(草案不落库)
    again = _dry(monkeypatch, client, {"text": "查一下进度"})
    assert "请提供委托单号" in again.json()["will_ask"]["question"]


def test_dry_run_execute_prefers_explicit_datasource_id(client, monkeypatch, legacy_demo_config):
    """真实执行:显式 datasource_id 优先于 env 缺省(能力导入的绑定形态)。

    env 未配置时,显式绑定仍能执行——env 只是部署层缺省,不是唯一来源
    (此前 dry-run 只读 env,导入场景勾真实执行必报"数据源未配置")。
    """
    from domains.chatbi import controlled as bi_controlled

    calls = {}

    def _fake_exec(app_state, ds_id, tool, params):
        calls["ds_id"] = ds_id
        return bi_controlled.ControlledResult(ok=True, status="ok",
                                              data={"columns": [], "rows": []})

    monkeypatch.setattr(bi_controlled, "execute_controlled", _fake_exec)
    monkeypatch.delenv("METROLOGY_PROGRESS_DATASOURCE_ID", raising=False)

    # 显式绑定必须是真实存在且属于该服务的 MCP 数据源——生产按
    # host/固定工具强校验,假 ID 会被正确拒绝(不再"任选一个源")
    from domains.chatbi import datasources as bi_ds, runtime as bi_rt
    explicit = bi_ds.create_datasource(
        bi_rt.get_pack_db(), "计量业务平台(显式)", "mcp", "metrology", 0,
        "query_progress", "", "")
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["action"]["datasource_id"] = explicit.id
    client.put("/api/packs/metrology_assistant/config", json={
        "content": content,
        "comment": "显式绑定"})

    resp = _dry(monkeypatch, client, {
        "text": "查 BJ2026 进度", "execute": True,
        "_extract": {"scenario": "progress", "instrument": "",
                     "purpose": "", "order_no": "BJ2026"}})
    body = resp.json()
    assert body["action"]["datasource_id"] == explicit.id
    assert body["executed"]["ok"] is True
    assert calls["ds_id"] == explicit.id


# ── MCP 能力导入(平台件+客服形状适配)──────────────────────

def test_translator_and_diff():
    from sdk.mcp_draft import translate_input_schema, capability_diff
    out = translate_input_schema({"properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "state": {"type": "string", "enum": ["a", "b"]},
    }, "required": ["name"]})
    assert out["required_slot"] == "name"
    by = {s["name"]: s for s in out["slots"]}
    assert by["count"]["slot_type"] == "number"
    assert by["state"]["slot_type"] == "enum" and by["state"]["enum_options"] == ["a", "b"]
    d = capability_diff({"srv": ["a", "b"]}, [("srv", "b"), ("srv", "gone")])
    assert d["new"] == [("srv", "a")] and d["stale"] == [("srv", "gone")]


def test_capability_inventory_and_draft(client, monkeypatch):
    """库存 diff 标新工具;起草产出可保存的场景形状(校验器过)。"""
    import json as _json
    from domains.metrology_assistant import capability_import as ci
    from domains.chatbi import datasources as bi_ds
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(ci, "_snapshots", lambda: {"metrology": [
        {"name": "query_progress", "inputSchema": {"properties": {
            "order_no": {"type": "string", "description": "委托单号"}},
            "required": ["order_no"]}},
        {"name": "query_fresh", "description": "查询器具的最新业务活动", "inputSchema": {"properties": {
            "instrument": {"type": "string"}}, "required": ["instrument"]}},
    ]})
    fake_info = _NS(id="ds-1", host="metrology", db_type="mcp",
                    database="query_fresh", is_active=True)
    monkeypatch.setattr(bi_ds, "list_datasources",
                        lambda db, active_only=False: [fake_info])
    from domains.chatbi import runtime as bi_runtime
    monkeypatch.setattr(bi_runtime, "get_pack_db", lambda: object())

    inv = client.get("/api/packs/metrology_assistant/config/capability-import").json()
    tools = {t["name"]: t for t in inv["servers"][0]["tools"]}
    assert tools["query_fresh"]["imported"] is False
    assert ("metrology", "query_fresh") in [tuple(x) for x in inv["diff"]["new"]]

    r = client.post("/api/packs/metrology_assistant/config/capability-import/draft",
                    json={"server": "metrology", "tool": "query_fresh"})
    assert r.status_code == 200
    draft = r.json()
    sc = draft["scenario"]
    assert draft["key"] == "metrology::query_fresh"
    assert sc["required"]["slot"] == "instrument"
    assert sc["action"]["server"] == "metrology"
    assert sc["action"]["tool"] == "query_fresh"
    assert sc["action"]["datasource_id"] == "ds-1"
    assert sc["action"]["card"] == "_table_card"
    # 草稿能过配置校验(可整档保存)
    from domains.metrology_assistant.scenario_config import validate_config
    validate_config({"scenarios": {draft["key"]: sc}})
    # 快照没有的工具 → 400
    r404 = client.post("/api/packs/metrology_assistant/config/capability-import/draft",
                       json={"server": "metrology", "tool": "nope"})
    assert r404.status_code == 400


def test_routing_overview_states(client, monkeypatch, legacy_demo_config):
    """路由总览四态:polished(场景表命中)/auto(可执行)/blocked(无源)/
    incomplete(声明不完整);servers 画像按服务汇总质量。"""
    from datetime import datetime as _dt, timezone as _tz
    from domains.metrology_assistant import capability_import as ci
    from domains.chatbi import datasources as bi_ds
    from domains.chatbi import runtime as bi_runtime
    from types import SimpleNamespace as _NS

    _fresh = _dt.now(_tz.utc).isoformat()
    monkeypatch.setattr(ci, "_servers_rows", lambda: [
        {"id": "s1", "code": "metrology", "name": "计量业务平台",
         "description": "计量检定业务查询", "enabled": True,
         "updated_at": _fresh, "tools_snapshot": [
            # progress 已被出厂场景表编排 → polished(声明短不算账)
            {"name": "query_progress", "description": "查进度",
             "inputSchema": {"properties": {"order_no": {"type": "string"}},
                             "required": ["order_no"]}},
            # 未编排且有通用源可执行 → auto
            {"name": "query_fresh", "description": "查询最新活动",
             "inputSchema": {"properties": {"keyword": {"type": "string"}},
                             "required": ["keyword"]}},
            # 声明缺失 → incomplete(即使有数据源也不进路由)
            {"name": "query_legacy", "description": "",
             "inputSchema": {"properties": {"x": {"type": "string"}},
                             "required": ["x"]}},
            # 声明过短(<6 字) → 同样 incomplete
            {"name": "query_vague", "description": "查询",
             "inputSchema": {"properties": {"y": {"type": "string"}},
                             "required": ["y"]}},
         ]},
        {"id": "s2", "code": "lonely", "enabled": True,
         "updated_at": _fresh, "tools_snapshot": [
            # 未编排且无数据源 → blocked
            {"name": "query_orphan", "description": "孤儿工具没有数据源绑定",
             "inputSchema": {"properties": {"x": {"type": "string"}},
                             "required": ["x"]}},
         ]},
    ])
    fake = _NS(id="ds-9", name="计量业务平台(通用)", host="metrology",
               db_type="mcp", database="", is_active=True)
    monkeypatch.setattr(bi_ds, "list_datasources",
                        lambda db, active_only=False: [fake])
    # override 绑定强校验会按 id 回读数据源元数据(生产查真实 DB)
    monkeypatch.setattr(bi_ds, "get_datasource",
                        lambda db, ds: fake if ds == "ds-9" else None)
    monkeypatch.setattr(bi_runtime, "get_pack_db", lambda: object())

    resp = client.get("/api/packs/metrology_assistant/config/routing")
    assert resp.status_code == 200
    body = resp.json()
    caps = {c["tool"]: c for c in body["capabilities"]}
    assert caps["query_progress"]["status"] == "polished"
    assert caps["query_progress"]["scenario_key"] == "progress"
    assert caps["query_fresh"]["status"] == "auto"
    assert caps["query_fresh"]["ask"]["slot"] == "keyword"
    assert caps["query_fresh"]["datasource"]["datasource_name"] == "计量业务平台(通用)"
    assert caps["query_legacy"]["status"] == "incomplete"
    assert any("描述缺失" in i for i in caps["query_legacy"]["issues"])
    assert caps["query_vague"]["status"] == "incomplete"
    assert caps["query_orphan"]["status"] == "blocked"
    assert "数据源" in caps["query_orphan"]["blocked_reason"]
    # kg 能力(出厂强检场景)也在总览里
    kg = [c for c in body["capabilities"] if c["kind"] == "kg"]
    assert kg and kg[0]["scenario_key"] == "mandatory_check"
    # 服务画像:metrology 4 工具 2 声明不完整;lonely 缺服务描述被提示
    servers = {s["code"]: s for s in body["servers"]}
    assert servers["metrology"]["tools"] == 4
    assert servers["metrology"]["incomplete"] == 2
    assert servers["metrology"]["description"] == "计量检定业务查询"
    assert servers["lonely"].get("issues")


def test_auto_scenarios_blocks_undeclared_tools(monkeypatch):
    """声明门禁:描述缺失的工具不合成自动场景(即使可执行);
    补上合格描述后即恢复自动接入。"""
    from datetime import datetime as _dt, timezone as _tz
    from domains.metrology_assistant import capability_import as ci
    from domains.chatbi import datasources as bi_ds
    from domains.chatbi import runtime as bi_runtime
    from types import SimpleNamespace as _NS

    _fresh = _dt.now(_tz.utc).isoformat()
    snapshot = {"id": "s1", "code": "metrology", "enabled": True,
                "updated_at": _fresh, "tools_snapshot": [
        {"name": "tool_ok", "description": "声明完整的查询工具",
         "inputSchema": {"properties": {"a": {"type": "string"}},
                         "required": ["a"]}},
        {"name": "tool_bad", "description": "",
         "inputSchema": {"properties": {"b": {"type": "string"}},
                         "required": ["b"]}},
    ]}

    def _rows():
        return [dict(snapshot, tools_snapshot=list(snapshot["tools_snapshot"]))]
    monkeypatch.setattr(ci, "_servers_rows", _rows)
    fake = _NS(id="ds-1", host="metrology", db_type="mcp",
               database="", is_active=True)
    monkeypatch.setattr(bi_ds, "list_datasources",
                        lambda db, active_only=False: [fake])
    monkeypatch.setattr(bi_runtime, "get_pack_db", lambda: object())

    table = {"progress": {"action": {
        "kind": "chatbi", "server": "metrology", "tool": "query_progress"}}}
    auto = ci.auto_scenarios(table)
    assert "metrology::tool_ok" in auto
    assert auto["metrology::tool_ok"]["action"]["server"] == "metrology"
    assert "metrology::tool_bad" not in auto  # 声明缺失被门禁拦截

    # 提供方补齐声明 → 下一轮自动出现
    snapshot["tools_snapshot"][1]["description"] = "补齐后的业务查询说明"
    auto2 = ci.auto_scenarios(table)
    assert "metrology::tool_bad" in auto2
