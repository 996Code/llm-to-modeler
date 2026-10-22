"""chatbi mcp 多轮兼容(Task 4b): switch_chart mcp 重放 + check 步 mcp 守卫。

覆盖:
  switch_chart: prev_sql 为工具调用 JSON 时——跳过 SQL 校验(此前 sqlglot
    必失败 → 误导"上轮 SQL 校验未通过")、走 execute_mcp_tool 重放(此前
    execute_readonly 对 mcp 抛 ValueError)、失败如实报错。
  S7 等价校验(终审#1): 重放前校验 tool ∈ 当前语义层 model names——
    不限定工具的 mcp 数据源(database="")下, 过期 session prev_sql
    可指向 server 上任意工具(含副作用型), 必须拒绝引导重问。
  check 步守卫: mcp 数据源的结果异常(ALL_NULL 等)不进 SQL 自动修正
    (heal_sql 吃 JSON 必产废 SQL → execute_readonly(mcp) 未捕获 ValueError
    炸管线), 直接走 ask 澄清; adjust_hint 自由输入路径同守卫。

构造参照: tests/domains/test_chatbi_mcp_pipeline.py(env/FakeLLM) +
test_chatbi_pipeline.py(TestSwitchChart 的 ToolContext 构造)。

跑法: cd backend && .venv/bin/python -m pytest tests/domains/test_chatbi_mcp_multiturn.py -v
"""
import json

import pytest

from domains.chatbi.datasources import DataSourceInfo, ExecuteResult
from domains.chatbi.tools.ask_data import AskDataTool
from domains.chatbi.tools.switch_chart import SwitchChartTool

TOOLS = [
    {"name": "query_gmv", "description": "查询 GMV",
     "inputSchema": {"type": "object",
                     "properties": {"city": {"type": "string"}},
                     "required": []}},
]


def _info(code="srv_a", database=""):
    return DataSourceInfo(id="ds1", name="外部", db_type="mcp",
                          host=code, port=0, database=database,
                          username="", password_plain="")


@pytest.fixture()
def env(monkeypatch):
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi import stores as cb_stores
    from domains.chatbi.datasources import init_store, create_datasource
    from domains.chatbi import semantic

    db = PackRelationalDB("chatbi")
    init_store(db)
    ds = create_datasource(db, name="外部报表", db_type="mcp", host="srv_a",
                           port=0, database="", username="", password="")
    content = semantic.synthesize_mcp_content(TOOLS)
    semantic.save_content(db, ds.id, content, source="scan")

    # 向量设施打爆 → 检索降级(确定性), 与 mcp_pipeline 测试同手法
    def _vector_down(app_state):
        raise RuntimeError("vector down")
    monkeypatch.setattr(cb_stores, "get_vector", _vector_down)

    from services.conversation_store import ConversationStore
    from engine.conversation import ConversationManager
    from sdk.tool import SessionStateHandle
    conv_store = ConversationStore()
    cm = ConversationManager(store=conv_store)
    conv_id = conv_store.create_conversation("tester")["id"]

    ctx_kw = dict(
        llm_client=None, asset_client=None, conversation=cm,
        emit=lambda *a, **k: None, conv_id=conv_id, registry=None,
        session_state=SessionStateHandle(conv_store, conv_id, "chatbi"),
    )
    return {"db": db, "ds": ds, "content": content,
            "ctx_kw": ctx_kw, "conv_store": conv_store, "conv_id": conv_id,
            "cm": cm}


def _sess(env):
    from sdk.tool import SessionStateHandle
    return SessionStateHandle(env["conv_store"], env["conv_id"], "chatbi")


def _ctx(env, llm=None):
    from sdk.tool import ToolContext
    kw = dict(env["ctx_kw"])
    if llm is not None:
        kw["llm_client"] = llm
    return ToolContext(**kw)


# ── switch_chart: mcp prev_sql 重放 ─────────────────────────────

class TestSwitchChartMcp:
    def _prepare_session(self, env):
        """模拟上一轮 mcp 查询写回的会话状态(prev_sql=工具调用 JSON)。"""
        sess = _sess(env)
        sess.set("datasource_id", env["ds"].id)
        sess.set("prev_sql", json.dumps({"tool": "query_gmv",
                                         "args": {"city": "上海"}}))
        sess.set("prev_chart_config", {"chart_type": "bar",
                                       "dim_col": "city",
                                       "measure_cols": ["gmv"]})
        return sess

    def test_switch_chart_replays_mcp_tool_call(self, env, monkeypatch):
        """mcp prev_sql: 跳过 SQL 校验 + execute_mcp_tool 重放 → 成功换图。

        修复前: validate_sql 吃 JSON 必失败 → 误导"上轮 SQL 校验未通过"。
        """
        from sdk import mcp_client
        seen = {}

        def fake_call(code, tool, args):
            seen.update({"code": code, "tool": tool, "args": args})
            return {"columns": ["city", "gmv"],
                    "rows": [["上海", 100], ["北京", 200]]}
        monkeypatch.setattr(mcp_client, "call_tool", fake_call)

        sess = self._prepare_session(env)
        tool = SwitchChartTool(db=env["db"], settings={})
        result = tool.execute({"user_input": "换成饼图"}, _ctx(env))
        assert result.error_for_llm is None, result.error_for_llm
        assert "校验未通过" not in result.summary
        # 重放走 MCP(原工具调用原参数), 不是 execute_readonly
        assert seen == {"code": "srv_a", "tool": "query_gmv",
                        "args": {"city": "上海"}}
        assert result.artifact["rowcount"] == 2
        assert result.formatted["chart"]["series"][0]["type"] == "pie"
        assert _sess(env).get("prev_chart_config")["chart_type"] == "pie"

    def test_switch_chart_mcp_failure_honest(self, env, monkeypatch):
        """mcp 重放失败 → 如实报错(复用"重跑查询失败"文案契约), 不炸管线。"""
        from sdk import mcp_client
        from sdk.tool import PackInvokeError

        def boom(code, tool, args):
            raise PackInvokeError("MCP server 连接失败(srv_a): refused",
                                  PackInvokeError.RETRYABLE)
        monkeypatch.setattr(mcp_client, "call_tool", boom)

        sess = self._prepare_session(env)
        tool = SwitchChartTool(db=env["db"], settings={})
        result = tool.execute({"user_input": "换成饼图"}, _ctx(env))
        assert result.error_for_llm and "重跑查询失败" in result.error_for_llm
        assert "srv_a" in result.error_for_llm  # 如实, 不静默

    def test_switch_chart_mcp_corrupted_prev_sql(self, env, monkeypatch):
        """prev_sql 既不是合法 SQL 也不是合法工具调用 JSON → 引导重新提问。"""
        from sdk import mcp_client
        called = {"n": 0}
        monkeypatch.setattr(mcp_client, "call_tool",
                            lambda c, t, a: called.__setitem__("n", called["n"] + 1))
        sess = _sess(env)
        sess.set("datasource_id", env["ds"].id)
        sess.set("prev_sql", "不是 JSON{也不是 SQL")
        tool = SwitchChartTool(db=env["db"], settings={})
        result = tool.execute({"user_input": "换成饼图"}, _ctx(env))
        assert result.error_for_llm and "重新提问" in result.summary
        assert called["n"] == 0

    def test_switch_chart_mcp_tool_outside_semantic_layer_rejected(
            self, env, monkeypatch):
        """S7 等价校验(终审#1): prev_sql 指向语义层外的工具 → 拒绝重放。

        场景: 不限定工具的 mcp 数据源(database="")——语义层只收录
        query_gmv, 但过期 session prev_sql 指向 server 上的另一工具
        (如副作用型 delete_everything)。语义层可能已变更/持久化数据
        可能被篡改(与 SQL 路径 S7 同理由), 重放前必须重校验
        tool ∈ 当前语义层 model names, 不在 → "上轮查询已失效"引导重问。
        铁证: execute_mcp_tool tripwire 计数为 0(绝未触达执行)。
        """
        import domains.chatbi.datasources as ds_mod
        calls = []

        def tripwire(*a, **kw):
            calls.append((a, kw))
            # 若被调用, 让流程可控失败(而非 None 炸 AttributeError)
            return ExecuteResult(False, error="不应执行: 语义层外工具被重放")
        monkeypatch.setattr(ds_mod, "execute_mcp_tool", tripwire)

        sess = _sess(env)
        sess.set("datasource_id", env["ds"].id)
        sess.set("prev_sql", json.dumps({"tool": "delete_everything",
                                         "args": {}}))
        sess.set("prev_chart_config", {"chart_type": "bar"})
        tool = SwitchChartTool(db=env["db"], settings={})
        result = tool.execute({"user_input": "换成饼图"}, _ctx(env))
        assert result.error_for_llm and "重新提问" in result.summary
        assert calls == []  # tripwire 计数: execute_mcp_tool 未被调用

    def test_switch_chart_mcp_unscanned_semantic_layer_rejected(
            self, env, monkeypatch):
        """语义层缺失(未扫描/已清空)时 fail-closed: 空集合不含任何工具 → 拒绝。"""
        from domains.chatbi import semantic as semantic_mod
        monkeypatch.setattr(semantic_mod, "load_current_content",
                            lambda db, ds_id: None)
        import domains.chatbi.datasources as ds_mod
        calls = []
        monkeypatch.setattr(ds_mod, "execute_mcp_tool",
                            lambda *a, **kw: calls.append(1))
        sess = _sess(env)
        sess.set("datasource_id", env["ds"].id)
        sess.set("prev_sql", json.dumps({"tool": "query_gmv", "args": {}}))
        tool = SwitchChartTool(db=env["db"], settings={})
        result = tool.execute({"user_input": "换成饼图"}, _ctx(env))
        assert result.error_for_llm and "重新提问" in result.summary
        assert calls == []


# ── check 步 mcp 守卫: 结果异常不进 SQL 自动修正 ─────────────────

class TestCheckStepMcpGuard:
    def _abnormal_state(self, env, **extra):
        """execute_result 异常(ALL_NULL: 2/2 列全 NULL)的 mcp state。"""
        state = {
            "ds": env["ds"],
            "content": env["content"],
            "current_tables": ["query_gmv"],
            "sql": json.dumps({"tool": "query_gmv", "args": {}}),
            "allowed_columns": set(),
            "execute_result": ExecuteResult(
                True, columns=["city", "gmv"],
                rows=[(None, None), (None, None)], rowcount=2),
            "heal_rounds": 0,
        }
        state.update(extra)
        return state

    def _guard_mocks(self, monkeypatch):
        """heal_sql 计数 + execute_readonly 触碰即炸(守卫铁证)。"""
        import domains.chatbi.tools.ask_data as ask_mod
        import domains.chatbi.datasources as ds_mod
        counts = {"heal": 0}

        class _NoHeal:
            success = False
            sql = ""
            error = "不应被调用"
        heal_calls = []

        def fake_heal(*a, **kw):
            counts["heal"] += 1
            heal_calls.append((a, kw))
            return _NoHeal()
        monkeypatch.setattr(ask_mod, "heal_sql", fake_heal)

        def no_execute_readonly(*a, **kw):
            raise AssertionError("mcp 数据源绝不能触发 execute_readonly")
        monkeypatch.setattr(ds_mod, "execute_readonly", no_execute_readonly)
        return counts

    def test_check_abnormal_mcp_goes_clarify_without_heal(self, env, monkeypatch):
        """mcp 结果异常 → 直接 ask 澄清, heal_sql 未被调用, 管线不崩。"""
        counts = self._guard_mocks(monkeypatch)
        tool = AskDataTool(db=env["db"], settings={})
        state = self._abnormal_state(env)
        tool._step_check_result(state, _ctx(env))  # 不抛 = 不崩
        assert counts["heal"] == 0
        assert state.get("_need_clarify") is True
        assert state.get("clarify_kind") == "result_abnormal"
        assert state["_result"].ask is not None

    def test_check_adjust_hint_mcp_skips_sql_heal(self, env, monkeypatch):
        """resume「帮我调整查询」自由输入(adjust_hint)对 mcp 不走 SQL 修正
        (跳过修正, check 原结果), 绝不触发 execute_readonly。"""
        counts = self._guard_mocks(monkeypatch)
        tool = AskDataTool(db=env["db"], settings={})
        state = self._abnormal_state(env, adjust_hint="只看北京")
        tool._step_check_result(state, _ctx(env))
        assert counts["heal"] == 0
        # 原结果仍异常 → 走澄清
        assert state.get("_need_clarify") is True
