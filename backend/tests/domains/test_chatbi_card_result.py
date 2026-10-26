"""BI card artifacts: real tool assembly with external persistence isolated."""
import json
from types import SimpleNamespace

import pytest

from domains.chatbi.chart_engine import ChartResult, inject_data
from domains.chatbi.checker import check_result
from domains.chatbi.datasources import ExecuteResult
from domains.chatbi.tools.ask_data import AskDataTool
from domains.chatbi.tools.switch_chart import SwitchChartTool
from sdk.tool import ToolContext


@pytest.fixture
def card_env(monkeypatch):
    from domains.chatbi import fewshot, m4, memory, query_stats, stores
    import domains.chatbi.tools.ask_data as ask_data

    monkeypatch.setattr(ask_data, "_semantic_content", lambda *a: None)
    monkeypatch.setattr(memory, "extract_and_save_memory", lambda *a, **kw: None)
    monkeypatch.setattr(memory, "persist_linkage_memory", lambda *a, **kw: None)
    monkeypatch.setattr(m4, "save_query", lambda *a, **kw: {"id": "saved-1"})
    monkeypatch.setattr(fewshot, "index_fewshot_example", lambda *a, **kw: None)
    monkeypatch.setattr(stores, "get_embedder", lambda *a: None)
    monkeypatch.setattr(stores, "get_vector", lambda *a: None)
    monkeypatch.setattr(query_stats, "record_query", lambda *a, **kw: None)
    ctx = ToolContext(llm_client=None, asset_client=None, conversation=None,
                      emit=lambda *a, **kw: None, conv_id="card-conv", registry=None)
    tool = AskDataTool(db=object(), settings={"sql_max_rows": 100})
    result = ExecuteResult(True, columns=["category", "amount"],
                           rows=[["A", 12], ["B", 9]], rowcount=2)
    config = {"chart_type": "bar", "dim_col": "category", "measure_cols": ["amount"]}
    state = {
        "user_input": "amount by category", "sql": "SELECT category, amount FROM orders",
        "ds": SimpleNamespace(id="ds-1", name="Orders", db_type="postgresql"),
        "execute_result": result, "current_tables": ["orders"],
        "seed_tables": ["orders"], "expanded_tables": [],
        "chart": ChartResult(ok=True, option=inject_data(config, result.columns, result.rows), config=config),
        "check": check_result(result.rows, result.columns, "SELECT category, amount FROM orders"),
        "_execute_duration_ms": 18,
    }
    return tool, state, ctx


@pytest.mark.parametrize("kind", ["bar", "kpi", "table"])
def test_success_declares_card_and_persists_full_answer(card_env, kind):
    tool, state, ctx = card_env
    config = {"chart_type": kind, "dim_col": "category", "measure_cols": ["amount"]}
    result = state["execute_result"]
    state["chart"] = ChartResult(ok=True, option=inject_data(config, result.columns, result.rows), config=config)
    tool._step_finalize(state, ctx)
    output = state["_result"]
    artifact = json.loads(output.model_dump_json())["artifact"]
    assert artifact["pack"] == "chatbi"
    assert artifact["resultDisplay"] == "card"
    assert artifact["answer"] == output.summary
    assert artifact["saved_query_id"] == output.formatted["savedQueryId"] == "saved-1"
    assert artifact["chart_option"] == output.formatted["chart"]
    assert artifact["sql"] == output.formatted["sql"]
    assert artifact["datasource_id"] == output.formatted["datasourceId"]


def test_answer_keeps_degradation_metrics_and_truncation(card_env):
    tool, state, ctx = card_env
    state["retrieval_degraded"] = True
    state["chart"].degraded = True
    state["execute_result"].truncated = True
    state["metric_hits"] = [{"metric": "revenue", "display_name": "Revenue"}]
    tool._step_finalize(state, ctx)
    output = state["_result"]
    assert output.artifact["answer"] == output.summary
    for fragment in ("检索降级", "规则推断降级", "Revenue", "截断"):
        assert fragment in output.artifact["answer"]
    assert output.artifact["retrieval_degraded"] is True


def test_zero_rows_keeps_explanation_without_fabricating_chart(card_env):
    tool, state, ctx = card_env
    result = ExecuteResult(True, columns=["amount"], rows=[], rowcount=0)
    state["execute_result"] = result
    state["chart"] = ChartResult(ok=False)
    state["check"] = check_result([], result.columns, state["sql"])
    tool._step_finalize(state, ctx)
    output = state["_result"]
    assert output.artifact["chart_option"] is None
    assert output.artifact["answer"] == output.summary
    assert state["check"].reason in output.summary
    assert state["check"].suggestion in output.summary


def test_persistence_warnings_and_history_action_fields_survive(card_env, monkeypatch):
    from domains.chatbi import m4

    tool, state, ctx = card_env
    def fail_save(*a, **kw):
        raise RuntimeError("save unavailable")
    monkeypatch.setattr(m4, "save_query", fail_save)
    tool._step_finalize(state, ctx)
    output = state["_result"]
    restored = tool.format_result(json.loads(output.model_dump_json())["artifact"])
    assert restored["persistWarnings"] == output.artifact["persist_warnings"]
    assert "查询未自动保存" in restored["persistWarnings"][0]
    assert restored["sql"] == state["sql"]
    assert restored["datasourceId"] == "ds-1"
    assert restored["tables"] == ["orders"]
    assert restored["executeDurationMs"] == 18


def test_total_duration_is_persisted_after_real_execute(card_env):
    tool, state, ctx = card_env
    tool.steps = ["finalize"]
    output = tool.execute(state, ctx)
    assert output.artifact["total_duration_ms"] == output.formatted["totalDurationMs"]
    assert tool.format_result(output.artifact)["totalDurationMs"] == output.formatted["totalDurationMs"]


def test_error_does_not_adopt_card_contract(card_env):
    tool, state, ctx = card_env
    state["_error"] = "SQL 校验失败: write rejected"
    tool._step_finalize(state, ctx)
    output = state["_result"]
    assert output.error_for_llm == state["_error"]
    assert output.artifact is None
    assert "write rejected" in output.summary


def test_clarification_keeps_original_path(card_env):
    tool, state, ctx = card_env
    state["ds"].db_type = "mcp"
    state["execute_result"] = ExecuteResult(True, columns=["a", "b"], rows=[[None, None]], rowcount=1)
    tool._step_check_result(state, ctx)
    output = state["_result"]
    assert output.ask is not None
    assert output.artifact is None
    assert output.ask.questions[0].header == "结果异常"


def test_switch_success_uses_same_card_contract(card_env, monkeypatch):
    from domains.chatbi import datasources

    _, state, ctx = card_env
    class Session:
        values = {"prev_sql": "SELECT category, amount FROM orders", "datasource_id": "ds-1"}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value):
            self.values[key] = value
    ctx.session_state = Session()
    monkeypatch.setattr(datasources, "resolve_datasource", lambda *a: state["ds"])
    monkeypatch.setattr(datasources, "execute_readonly", lambda *a, **kw: state["execute_result"])
    output = SwitchChartTool(db=object(), settings={"sql_max_rows": 100}).execute({"user_input": "表格"}, ctx)
    assert output.artifact["pack"] == "chatbi"
    assert output.artifact["resultDisplay"] == "card"
    assert output.artifact["answer"] == output.summary
    assert output.artifact["truncated"] is False
