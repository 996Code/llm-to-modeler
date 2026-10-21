"""mcp 扫描: 工具清单合成语义层(工具=表/参数=列), 落 chatbi_semantic_models。

合成规则(设计从简, 不做 LLM 富化/指标推断):
  每工具一个 Model(name=display_name=工具名, description=工具描述),
  columns=inputSchema.properties 逐项(name=display_name=参数名,
  data_type=JSON Schema 类型映射, 不认识的给默认)。
限定过滤: 数据源 info.database 非空(=tool_name)时只合成该工具。
任务分支: _task_scan_datasource 的 mcp 分支复用进度回写/租约/CAS,
跳过 _connect_business/内省/LLM 富化/向量索引重建。

跑法: cd backend && .venv/bin/python -m pytest tests/domains/test_chatbi_mcp_scan.py -v
"""
import pytest

from domains.chatbi.semantic import synthesize_mcp_content

TOOLS = [
    {"name": "query_orders", "description": "查询订单数据",
     "inputSchema": {"type": "object",
                     "properties": {"city": {"type": "string"},
                                    "month": {"type": "string"}},
                     "required": ["city"]}},
    {"name": "query_gmv", "description": "查询 GMV",
     "inputSchema": {"type": "object", "properties": {}}},
]


@pytest.fixture()
def db():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store
    d = PackRelationalDB("chatbi")
    init_store(d)
    yield d


# ── 合成(纯函数) ────────────────────────────────────────────────

def test_synthesize():
    content = synthesize_mcp_content(TOOLS)
    assert [m.name for m in content.models] == ["query_orders", "query_gmv"]
    cols = {c.name for c in content.models[0].columns}
    assert cols == {"city", "month"}
    assert content.models[0].description == "查询订单数据"


def test_synthesize_empty():
    assert synthesize_mcp_content([]).models == []


def test_synthesize_model_shape():
    m = synthesize_mcp_content(TOOLS).models[0]
    # name=display_name=工具名; 无关系/指标/计算字段(设计从简)
    assert m.display_name == "query_orders"
    assert m.relationships == [] and m.metrics == []
    assert m.calculated_fields == []
    # sample_questions 留空
    content = synthesize_mcp_content(TOOLS)
    assert content.sample_questions == []


def test_synthesize_column_type_mapping():
    tools = [{"name": "t", "description": "",
              "inputSchema": {"type": "object", "properties": {
                  "a": {"type": "string"},
                  "b": {"type": "integer"},
                  "c": {"type": "number"},
                  "d": {"type": "boolean"},
                  "e": {"type": "array"},
                  "f": {},  # 无类型: 默认
              }}}]
    cols = {c.name: c for c in synthesize_mcp_content(tools).models[0].columns}
    assert cols["a"].data_type == "string"
    assert cols["b"].data_type == "integer"
    assert cols["c"].data_type == "number"
    assert cols["d"].data_type == "boolean"
    assert cols["e"].data_type == "array"
    assert cols["f"].data_type == "string"   # 不认识的默认 string
    for c in cols.values():
        assert c.display_name == c.name


# ── 落库往返(save_content → load_current_content) ────────────────

def test_save_and_reload_roundtrip(db):
    from domains.chatbi.semantic import load_current_content, save_content
    content = synthesize_mcp_content(TOOLS)
    version = save_content(db, "ds-mcp-1", content, source="scan")
    assert version >= 1
    reloaded = load_current_content(db, "ds-mcp-1")
    assert reloaded is not None
    assert [m.name for m in reloaded.models] == ["query_orders", "query_gmv"]


# ── 扫描任务 mcp 分支(限定过滤 + 落库 + 终态 done) ────────────────
# _task_scan_datasource 直调: monkeypatch runtime.get_pack_db 返回测试库,
# RunLease 走真实 PG(claim/heartbeat/release 全真), mcp_client.list_tools
# 打桩返回 TOOLS——验证分支接线(进度回写/租约/CAS 落库/终态)。

class _Handle:
    task_id = "mcp-scan-test"
    payload = None

    def __init__(self, ds_id):
        self.payload = {"datasource_id": ds_id}
        self.logs = []
        self.progress = []

    def log(self, msg, **kw):
        self.logs.append(msg)

    def set_progress(self, pct, stage, **kw):
        self.progress.append((pct, stage))


def _run_mcp_scan(db, monkeypatch, ds, tools=TOOLS, tool_filter=""):
    from domains.chatbi import runtime, tasks
    from domains.chatbi.datasources import create_datasource
    from sdk import mcp_client

    info = create_datasource(db, name="外部报表", db_type="mcp", host="srv_a",
                             port=0, database=tool_filter,
                             username="", password="")
    monkeypatch.setattr(runtime, "get_pack_db", lambda: db)
    monkeypatch.setattr(mcp_client, "list_tools", lambda code: list(tools))

    handle = _Handle(info.id)
    result = tasks._task_scan_datasource(handle, None)
    return info.id, handle, result


def test_task_scan_mcp_branch(db, monkeypatch):
    from domains.chatbi.datasources import get_datasource
    from domains.chatbi.semantic import load_current_content

    ds_id, handle, result = _run_mcp_scan(db, monkeypatch, None)
    assert result["ok"] is True
    assert result["models"] == 2
    content = load_current_content(db, ds_id)
    assert content is not None
    assert [m.name for m in content.models] == ["query_orders", "query_gmv"]
    # 终态: done + 进度 100
    info = get_datasource(db, ds_id, decrypt=True)
    assert info.scan_status == "done"
    assert info.scan_progress == 100
    # 进度回写确实发生(复用既有机制)
    assert handle.progress, "mcp 分支未复用进度回写"


def test_task_scan_mcp_tool_filter(db, monkeypatch):
    """限定工具过滤: info.database 非空时只合成该工具。"""
    from domains.chatbi.semantic import load_current_content

    ds_id, _, result = _run_mcp_scan(db, monkeypatch, None,
                                     tool_filter="query_gmv")
    assert result["models"] == 1
    content = load_current_content(db, ds_id)
    assert [m.name for m in content.models] == ["query_gmv"]


def test_task_scan_mcp_failure_marks_failed(db, monkeypatch):
    """list_tools 失败 → 数据源终态 failed(异常如实上抛)。"""
    from domains.chatbi import runtime, tasks
    from domains.chatbi.datasources import create_datasource, get_datasource
    from sdk import mcp_client

    info = create_datasource(db, name="坏服务", db_type="mcp", host="srv_bad",
                             port=0, database="", username="", password="")

    def boom(code):
        raise RuntimeError("MCP server 连接失败")

    monkeypatch.setattr(runtime, "get_pack_db", lambda: db)
    monkeypatch.setattr(mcp_client, "list_tools", boom)
    with pytest.raises(RuntimeError):
        tasks._task_scan_datasource(_Handle(info.id), None)
    got = get_datasource(db, info.id, decrypt=True)
    assert got.scan_status == "failed"


def test_refresh_skips_mcp(db):
    """自动刷新跳过 mcp 数据源: 无业务库可结构内省, 工具变化靠手动重扫
    (此前每轮刷新对 mcp 必报"不支持的数据库类型"假失败)。"""
    from domains.chatbi import tasks
    from domains.chatbi.datasources import create_datasource
    from domains.chatbi.semantic import save_content

    info = create_datasource(db, name="外部报表", db_type="mcp", host="srv_a",
                             port=0, database="", username="", password="")
    save_content(db, info.id, synthesize_mcp_content(TOOLS), source="scan")

    lease = tasks.RunLease(db, "run:refresh_semantics",
                           holder="task:refresh-mcp", ttl_seconds=60)
    assert lease.acquire()
    try:
        results = []
        tasks._refresh_all_datasources(_Handle(info.id), None, db, {},
                                       results, lease)
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["skipped"] == "mcp_manual_rescan_only"
    finally:
        lease.release()
