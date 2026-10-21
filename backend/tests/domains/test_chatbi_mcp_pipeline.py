"""ask_data mcp 分支: 工具调用 JSON 生成(mock LLM) + execute_mcp_tool 映射 + 全管线。

覆盖:
  execute_mcp_tool 单元(形状映射/限定拒绝/如实报错/截断/坏 JSON)
  管线级(generate 产工具调用 JSON → execute 调 MCP → check/visualize/finalize
  零改动跑通)/自愈回环(执行失败 → mcp 变体 heal → 成功)/生成校验
  fail-closed(工具不在语义层)/prev_sql 重放(state["sql"] 双语义)。

与 brief 骨架的适配:
  - test_execute_mcp_rejects_unknown_tool: brief 的 _info() database="" 无从
    本地拒绝未知工具(限定为空=不限), 改用 database="query_gmv" 限定形态
    (与接口描述"工具不在 info.database 限定内(若限定)→ok=False"一致)。
  - FakeLLM/最小 state 构造参照 tests/domains/test_chatbi_pipeline.py。

跑法: cd backend && .venv/bin/python -m pytest tests/domains/test_chatbi_mcp_pipeline.py -v
"""
import json

import pytest

from domains.chatbi.datasources import DataSourceInfo, execute_mcp_tool
from domains.chatbi.tools.ask_data import AskDataTool


def _info(code="srv_a", database=""):
    return DataSourceInfo(id="ds1", name="外部", db_type="mcp",
                          host=code, port=0, database=database,
                          username="", password_plain="")


# ── execute_mcp_tool: 结果形状映射 ─────────────────────────────

def test_execute_mcp_dict_columns_rows(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: {"columns": ["city", "gmv"],
                                         "rows": [["上海", 100], ["北京", 200]]})
    r = execute_mcp_tool(_info(), {"tool": "query_gmv", "args": {}})
    assert r.ok and r.columns == ["city", "gmv"]
    assert r.rows == [("上海", 100), ("北京", 200)] and r.rowcount == 2


def test_execute_mcp_dict_rows_already_tuples(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: {"columns": ["x"],
                                         "rows": [(1,), (2,)]})
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.rows == [(1,), (2,)]


def test_execute_mcp_list_of_dicts(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: [{"city": "上海", "gmv": 1},
                                         {"city": "北京", "gmv": 2}])
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.columns == ["city", "gmv"] and r.rowcount == 2
    assert r.rows == [("上海", 1), ("北京", 2)]


def test_execute_mcp_bare_dict_record(monkeypatch):
    # 无 columns/rows 的裸 dict = 单条记录(键即列名)
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: {"city": "上海", "gmv": 1})
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.columns == ["city", "gmv"] and r.rows == [("上海", 1)]


def test_execute_mcp_list_of_lists_rejected(monkeypatch):
    # list[list] 无列名 → 报错(不猜列名)
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: [[1, 2], [3, 4]])
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert not r.ok and "列名" in r.error


def test_execute_mcp_empty_list(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool", lambda c, t, a: [])
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.rowcount == 0 and r.rows == []


def test_execute_mcp_scalar_single_value(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool", lambda c, t, a: 42)
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.columns == ["value"] and r.rows == [(42,)]


def test_execute_mcp_list_of_scalars_single_column(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool", lambda c, t, a: [1, 2, 3])
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.columns == ["value"] and r.rows == [(1,), (2,), (3,)]


def test_execute_mcp_truncates(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: {"columns": ["x"],
                                         "rows": [[i] for i in range(5)]})
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}}, max_rows=3)
    assert r.ok and r.rowcount == 3 and r.truncated


# ── execute_mcp_tool: 错误路径(如实, 不静默) ────────────────────

def test_execute_mcp_error_honest(monkeypatch):
    from sdk.tool import PackInvokeError
    from sdk import mcp_client

    def boom(c, t, a):
        raise PackInvokeError("MCP server 连接失败(srv_a): refused",
                              PackInvokeError.RETRYABLE)
    monkeypatch.setattr(mcp_client, "call_tool", boom)
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert not r.ok and "srv_a" in r.error      # 如实, 不静默


def test_execute_mcp_rejects_unknown_tool():
    # 限定过滤: info.database=tool_name 非空时只允许该工具
    r = execute_mcp_tool(_info(database="query_gmv"), {"tool": "nope", "args": {}})
    assert not r.ok and "nope" in r.error


def test_execute_mcp_rejects_bad_payload():
    r = execute_mcp_tool(_info(), "不是 JSON{")
    assert not r.ok and "解析" in r.error
    r2 = execute_mcp_tool(_info(), {"tool": "t", "args": "not-a-dict"})
    assert not r2.ok and "args" in r2.error
    r3 = execute_mcp_tool(_info(), {"args": {}})
    assert not r3.ok and "tool" in r3.error


# ── 管线级(AskDataTool 直调; mock LLM + mock call_tool + 合成语义层) ──

TOOLS = [
    {"name": "query_orders", "description": "查询订单数据",
     "inputSchema": {"type": "object",
                     "properties": {"city": {"type": "string"},
                                    "month": {"type": "string"}},
                     "required": ["city"]}},
    {"name": "query_gmv", "description": "查询 GMV",
     "inputSchema": {"type": "object", "properties": {}}},
]


class FakeLLM:
    """按 stage 可编程(chat 返回 (content, meta); chat_json 返回编排值)。"""

    def __init__(self, script: dict):
        self.script = script
        self.calls = []

    def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
        self.calls.append({"stage": stage, "user": messages[-1]["content"]})
        for key in (stage, "*"):
            if key in self.script:
                v = self.script[key]
                if callable(v):
                    v = v(messages=messages)
                return (json.dumps(v, ensure_ascii=False)
                        if isinstance(v, dict) else v), {}
        raise RuntimeError(f"FakeLLM: stage {stage} 未编排")

    def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None):
        self.calls.append({"stage": stage, "user": messages[-1]["content"]})
        for key in (stage, "*"):
            if key in self.script:
                v = self.script[key]
                return v() if callable(v) else v
        raise RuntimeError(f"FakeLLM.chat_json: stage {stage} 未编排")


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

    # 向量设施打爆 → 检索全表降级(2 工具 ≤ 阈值 → 种子=全部工具),
    # 管线确定性地走完整 think→generate→execute 路径
    def _vector_down(app_state):
        raise RuntimeError("vector down")
    monkeypatch.setattr(cb_stores, "get_vector", _vector_down)

    # 熔断器重置(进程级单例, 用例间隔离)
    from domains.chatbi.healer import reset_circuit_breaker
    reset_circuit_breaker()

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
    tool = AskDataTool(db=db, settings={
        "graph_enabled": False,       # 合成层无 relationships, 关图谱
        "clarify_table_threshold": 10_000,
    })
    return {"db": db, "ds": ds, "content": content, "tool": tool,
            "ctx_kw": ctx_kw, "conv_store": conv_store, "conv_id": conv_id}


def _run(env, question, llm):
    from sdk.tool import ToolContext
    ctx = ToolContext(**{**env["ctx_kw"], "llm_client": llm})
    state = {"user_input": question}
    result = env["tool"].execute(state, ctx)
    return result, state


def _sess(env):
    from sdk.tool import SessionStateHandle
    return SessionStateHandle(env["conv_store"], env["conv_id"], "chatbi")


class TestMcpPipeline:
    def test_end_to_end_tool_call_and_rows(self, env, monkeypatch):
        from sdk import mcp_client
        monkeypatch.setattr(mcp_client, "call_tool",
                            lambda c, t, a: {"columns": ["city", "gmv"],
                                             "rows": [["上海", 100], ["北京", 200]]})
        llm = FakeLLM({
            "chatbi.think": {"tables": ["query_gmv"], "aggregation": "", "caveats": []},
            "chatbi.generate_mcp": {"tool": "query_gmv", "args": {}},
            "chatbi.chart": {"chart_type": "table"},
        })
        result, state = _run(env, "查一下 GMV", llm)
        assert result.error_for_llm is None, result.error_for_llm
        # execute_result 契约不变(columns/rows), 后续步骤零改动
        assert state["execute_result"].columns == ["city", "gmv"]
        assert state["execute_result"].rows == [("上海", 100), ("北京", 200)]
        # state["sql"] 双语义: mcp 数据源时 = 合法工具调用 JSON
        parsed = json.loads(state["sql"])
        assert parsed == {"tool": "query_gmv", "args": {}}
        # 会话写回 prev_sql = 同一 JSON(重放走同一 execute 分支)
        assert json.loads(_sess(env).get("prev_sql"))["tool"] == "query_gmv"
        assert result.artifact["rowcount"] == 2

    def test_self_heal_loop_regenerates_tool_call(self, env, monkeypatch):
        from sdk import mcp_client
        from sdk.tool import PackInvokeError
        calls = {"n": 0}

        def flaky(code, tool, args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PackInvokeError(
                    "MCP 工具 query_gmv 执行失败: 参数 city 缺失",
                    PackInvokeError.PERMANENT)
            return {"columns": ["city", "gmv"], "rows": [["上海", 100]]}
        monkeypatch.setattr(mcp_client, "call_tool", flaky)

        llm = FakeLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []},
            "chatbi.generate_mcp": {"tool": "query_gmv", "args": {}},
            "chatbi.heal_mcp": {"tool": "query_orders", "args": {"city": "上海"}},
            "chatbi.chart": {"chart_type": "table"},
        })
        result, state = _run(env, "查 GMV", llm)
        assert result.error_for_llm is None, result.error_for_llm
        assert state["heal_rounds"] == 1
        assert json.loads(state["sql"])["tool"] == "query_orders"
        assert state["execute_result"].rows == [("上海", 100)]
        # 自愈 prompt 含原调用 + 错误文本(mcp 变体)
        heal_prompts = [c["user"] for c in llm.calls
                        if c.get("stage") == "chatbi.heal_mcp"]
        assert len(heal_prompts) == 1
        assert "query_gmv" in heal_prompts[0]
        assert "参数 city 缺失" in heal_prompts[0]

    def test_generate_rejects_tool_not_in_semantic_layer(self, env, monkeypatch):
        from sdk import mcp_client
        calls = {"n": 0}

        def spy(code, tool, args):
            calls["n"] += 1
            return {"columns": ["x"], "rows": [["y"]]}
        monkeypatch.setattr(mcp_client, "call_tool", spy)
        llm = FakeLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []},
            "chatbi.generate_mcp": {"tool": "nope", "args": {}},
        })
        result, state = _run(env, "查 GMV", llm)
        # fail-closed: 工具不在合成模型名集合 → _error, 不执行任何 MCP 调用
        assert result.error_for_llm and "nope" in result.error_for_llm
        assert "不在" in result.error_for_llm
        assert calls["n"] == 0
        assert state.get("execute_result") is None

    def test_prev_sql_replay_through_execute_step(self, env, monkeypatch):
        """state["sql"] 双语义(控制器裁决): mcp 时存工具调用 JSON——
        引擎 resume/重放带着 sql 进来(无 execute_result)时, 走同一
        execute 分支重新执行, 无需 SQL 解析。"""
        from sdk import mcp_client
        from sdk.tool import ToolContext
        monkeypatch.setattr(mcp_client, "call_tool",
                            lambda c, t, a: {"columns": ["gmv"], "rows": [[100]]})
        ctx = ToolContext(**{**env["ctx_kw"], "llm_client": FakeLLM({})})
        state = {"ds": env["ds"], "content": env["content"],
                 "current_tables": ["query_gmv"],
                 "sql": json.dumps({"tool": "query_gmv", "args": {}}),
                 "allowed_columns": set()}
        env["tool"]._step_execute_sql(state, ctx)
        assert state.get("execute_result") is not None
        assert state["execute_result"].rows == [(100,)]
        assert state.get("_error") is None
