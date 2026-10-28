"""metrology_assistant 薄编排工具单测。

覆盖 13.1 验收故事对应的核心行为:
- 故事1: 缺用途只追问必要条件(pending 上下文落会话 KV)
- 故事2: 用途→资料库映射(改用途换库)
- 故事3: 无依据时如实"无法确定",不编造
- 故事4/5: 缺单号追问;拒权/查无/故障分别显示
- 追问恢复: clarify_answers 确定性续跑,不再烧提取
"""
from types import SimpleNamespace

import pytest

from domains.chatbi.controlled import ControlledError, ControlledResult
from domains.metrology_assistant.tools import assistant as mta


class FakeMem:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v

    def pop(self, k, default=None):
        return self.d.pop(k, default)


class FakeLLM:
    """按 stage 返回槽位或结构化结论。"""

    def __init__(self, extract=None, conclusion=None):
        self._extract = extract or {}
        self._conclusion = conclusion

    def chat_json(self, messages=None, temperature=None, stage=None,
                  conv_id=None, **kw):
        if stage == "assistant.conclude":
            return self._conclusion
        return self._extract

    def chat(self, messages=None, **kw):
        raise AssertionError("强检结论必须直接请求结构化 JSON")


def _answer(decision="mandatory", refs=None):
    return {
        "decision": decision,
        "conclusion": {
            "mandatory": "用于贸易结算的压力表属于强制检定",
            "not_mandatory": "用于贸易结算的压力表不属于强制检定",
            "uncertain": "无法确定用于贸易结算的压力表是否属于强制检定",
        }[decision],
        "evidence": [{"text": "**依据**: 列入目录", "chunk_refs": [1] if refs is None else refs}],
        "suggestion": "按周期送检",
    }


class FakeCtx:
    def __init__(self, llm, mem=None):
        self.llm_client = llm
        self.session_state = mem or FakeMem()
        self.conv_id = "conv-test"
        self.emitted = []

    def emit(self, event, *args):
        self.emitted.append((event, args))


@pytest.fixture(autouse=True)
def explicit_legacy_demo(monkeypatch):
    from domains.metrology_assistant.legacy_demo import demo_scenarios
    from domains.metrology_assistant import capability_import as ci
    monkeypatch.setattr(mta, '_current_scenarios', demo_scenarios)
    monkeypatch.setattr(ci, 'resolve_scenarios', lambda table: table)
    from domains.chatbi import datasources
    monkeypatch.setattr(datasources, 'get_datasource', lambda db, ds: SimpleNamespace(
        host='metrology', database='', is_active=True, db_type='mcp'))


@pytest.fixture
def tool():
    return mta.MetrologyAssistantTool(app_state=object())


# ── 纯函数 ────────────────────────────────────────────────────

def test_extract_slots_normalizes():
    llm = FakeLLM(extract={"scenario": "mandatory_check",
                           "instrument": "压力表",
                           "purpose": "贸易结算",
                           "order_no": "bj2026"})
    got = mta.extract_slots(llm, "压力表要强检吗", {}, "c1")
    assert got == {"scenario": "mandatory_check", "instrument": "压力表",
                   "purpose": "贸易结算"}


def test_extract_slots_invalid_scenario_falls_to_other():
    llm = FakeLLM(extract={"scenario": "价格", "instrument": "",
                           "purpose": "", "order_no": ""})
    assert mta.extract_slots(llm, "多少钱", {}, "c1")["scenario"] == "other"


def test_merge_slots_current_turn_wins():
    merged = mta.merge_slots(
        {"instrument": "血压计", "purpose": "医疗卫生", "order_no": ""},
        {"instrument": "压力表", "purpose": "贸易结算", "order_no": "BJ2026"})
    assert merged == {"instrument": "血压计", "purpose": "医疗卫生",
                      "order_no": "BJ2026"}


def test_parse_clarify_answers_structured_and_text():
    assert mta.parse_clarify_answers({"器具用途": "安全防护"}) == {
        "purpose": "安全防护"}
    assert mta.parse_clarify_answers({"委托单号": "BJ2026"}) == {
        "order_no": "BJ2026"}
    assert mta.parse_clarify_answers({'text': 'BJ2027'}, pending={
        'slot': 'order_no', 'header': '委托单号'}) == {'order_no': 'BJ2027'}
    assert mta.parse_clarify_answers({'text': '安全防护'}, pending={
        'slot': 'purpose', 'header': '器具用途'}) == {'purpose': '安全防护'}
    assert mta.parse_clarify_answers({'text': '帮我查 BJ2027 这个单'}) == {}


def test_resolve_kb_for_purpose():
    assert mta.resolve_kb_for_purpose("贸易结算") == "强检目录"
    assert mta.resolve_kb_for_purpose("医疗卫生") == "强检目录"
    assert mta.resolve_kb_for_purpose("内部使用") == "检定规程"
    assert mta.resolve_kb_for_purpose("") == "检定规程"


def test_format_progress_reply():
    reply = mta._format_progress_reply({
        "order_no": "BJ2026", "instrument": "数字压力计", "status": "检测中",
        "progress": "3/5", "current_step": "压力示值误差检定",
        "eta": "2026-10-30", "queried_at": "2026-10-26 10:00"})
    assert "委托单 BJ2026(数字压力计)当前状态: 检测中" in reply
    assert "进度 3/5" in reply and "预计完成 2026-10-30" in reply
    assert "查询时间 2026-10-26 10:00" in reply


@pytest.mark.parametrize("invalid", [
    None, [], "结论: 属于强制检定", {},
    {**_answer(), "decision": "yes"},
    {**_answer(), "decision": []},
    {**_answer(), "conclusion": " "},
    {**_answer(), "conclusion": 1},
    {**_answer(), "suggestion": None},
    {**_answer(), "suggestion": ""},
    {**_answer(), "evidence": {}},
    {**_answer(), "evidence": []},
    {**_answer("not_mandatory"), "evidence": []},
    {**_answer(), "evidence": ["目录"]},
    {**_answer(), "evidence": [{"text": "", "chunk_refs": [1]}]},
    {**_answer(), "evidence": [{"text": 1, "chunk_refs": [1]}]},
    {**_answer(), "evidence": [{"text": "目录"}]},
    _answer(refs=[]), _answer(refs="1"), _answer(refs=[0]),
    _answer(refs=[2]), _answer(refs=[-1]), _answer(refs=[True]),
    _answer(refs=[1.0]), _answer(refs=["1"]),
])
def test_compose_conclusion_rejects_invalid_result(invalid):
    with pytest.raises(ValueError, match="结论"):
        mta.compose_conclusion(FakeLLM(conclusion=invalid), "压力表", "贸易结算",
                               [{"text": "目录"}], "c1")


@pytest.mark.parametrize("missing", ["decision", "conclusion", "evidence", "suggestion"])
def test_compose_conclusion_requires_all_fields(missing):
    result = _answer()
    result.pop(missing)
    with pytest.raises(ValueError, match="结论"):
        mta.compose_conclusion(FakeLLM(conclusion=result), "压力表", "贸易结算",
                               [{"text": "目录"}], "c1")


@pytest.mark.parametrize("decision", ["mandatory", "not_mandatory", "uncertain"])
def test_compose_conclusion_returns_structured_result(decision):
    answer = _answer(decision)
    assert mta.compose_conclusion(FakeLLM(conclusion=answer), "压力表", "贸易结算",
                                  [{"text": "目录"}], "c1") == answer


def test_compose_conclusion_uncertain_can_have_no_evidence():
    answer = {**_answer("uncertain"), "conclusion": "资料不足,无法确定", "evidence": []}
    assert mta.compose_conclusion(FakeLLM(conclusion=answer), "压力表", "贸易结算",
                                  [{"text": "无关资料"}], "c1") == answer


# ── 场景一:强检判定 ──

def _run(tool, ctx, user_input, clarify=None, mem=None):
    state = {"user_input": user_input}
    if clarify:
        state["clarify_answers"] = {**clarify, '__question_id': (ctx.session_state.get('pending') or {}).get('question_id')}
    return tool.execute(state, ctx)


def test_missing_purpose_asks_only_necessary_condition(tool):
    ctx = FakeCtx(FakeLLM(extract={"scenario": "mandatory_check",
                                   "instrument": "压力表",
                                   "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "压力表要强检吗")
    assert r.ask is not None
    q = r.ask.questions[0]
    assert q.header == "器具用途"
    assert ctx.session_state.get("pending")["scenario"] == "mandatory_check"


def test_purpose_routes_kb_and_concludes_with_citation(tool, monkeypatch):
    """用途→库映射 + 依据引用编号进 artifact(故事 2/3 的机制)。"""
    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence",
        lambda *a, **kw: {
            "kb": {"id": "kb1", "name": "强检目录"},
            "subgraph": {"nodes": [{"name": "压力表"}], "edges": []},
            "chunks": [{"docId": "d1", "docName": "强检目录2026.pdf",
                        "seq": 1, "score": 0.9,
                        "text": "用于贸易结算的压力表列入强制检定目录"}],
            "diagnostics": {}})
    ctx = FakeCtx(FakeLLM(
        extract={"scenario": "mandatory_check", "instrument": "压力表",
                 "purpose": "贸易结算", "order_no": ""},
        conclusion=_answer(refs=[1, 1])))
    r = _run(tool, ctx, "压力表用于贸易结算,要强检吗")
    assert r.ask is None
    assert r.artifact["type"] == "kg_search_result"
    assert r.artifact["kb"]["name"] == "强检目录"
    assert r.artifact["cited_chunks"] == [1]
    assert r.artifact["sources"]["chunks"][0]["docName"] == "强检目录2026.pdf"
    assert r.artifact_type == "data"
    assert r.artifact["pack"] == "metrology_assistant"
    assert r.model_dump(mode="json")["artifact"]["resultDisplay"] == "card"
    assert r.artifact["answer"] == _answer(refs=[1, 1])
    assert r.summary == ("结论: 用于贸易结算的压力表属于强制检定\n"
                         "依据: **依据**: 列入目录 [片段:1]\n建议: 按周期送检")
    assert r.formatted == {"kb": "强检目录", "conclusion": r.summary[:40],
                           "chunkCount": 1}
    assert tool.format_result(r.artifact) == {"kb": "强检目录", "chunkCount": 1}


def test_no_evidence_honest_unknown(tool, monkeypatch):
    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence",
        lambda *a, **kw: {
            "kb": {"id": "kb1", "name": "检定规程"},
            "subgraph": {"nodes": [], "edges": []}, "chunks": [],
            "diagnostics": {}})
    ctx = FakeCtx(FakeLLM(extract={"scenario": "mandatory_check",
                                   "instrument": "扭矩扳手",
                                   "purpose": "内部使用", "order_no": ""}))
    r = _run(tool, ctx, "内部用的扭矩扳手要强检吗")
    assert "无法确定" in r.summary
    assert "未检索到" in r.summary
    assert r.artifact["sources"]["chunks"] == []
    assert r.artifact["pack"] == "metrology_assistant"
    assert r.model_dump(mode="json")["artifact"]["resultDisplay"] == "card"
    assert r.artifact["answer"] == {
        "decision": "uncertain",
        "conclusion": "无法确定",
        "evidence": [],
        "evidence_gap": "在「检定规程」库中未检索到「扭矩扳手(内部使用)」的相关依据",
        "suggestion": "请确认器具名称是否准确,或联系业务窗口人工确认",
    }
    assert r.artifact["cited_chunks"] == []
    assert r.summary == ("结论: 无法确定\n依据: 在「检定规程」库中未检索到"
                         "「扭矩扳手(内部使用)」的相关依据\n"
                         "建议: 请确认器具名称是否准确,或联系业务窗口人工确认")


@pytest.mark.parametrize("chunks", [[], [{"text": "无关资料"}]])
def test_card_evidence_gap_survives_real_engine_result(tool, monkeypatch, chunks):
    from engine import nodes

    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence",
        lambda *a, **kw: {"chunks": chunks})
    answer = {**_answer("uncertain"), "evidence": []}
    ctx = FakeCtx(FakeLLM(
        extract={"scenario": "mandatory_check", "instrument": "压力表", "purpose": "安全防护"},
        conclusion=answer))
    result = _run(tool, ctx, "压力表用于安全防护,要强检吗")
    events = nodes.handle_result_node({"tool_result": result.model_dump(mode="json")})["sse_events"]
    payload = events[0]["data"]
    expected_gap = ("资料不足以支撑" if chunks else
                    "在「强检目录」库中未检索到「压力表(安全防护)」的相关依据")
    assert payload["artifactType"] == "data"
    assert payload["data"]["pack"] == "metrology_assistant"
    assert payload["data"]["resultDisplay"] == "card"
    assert payload["data"]["answer"]["decision"] == "uncertain"
    assert payload["data"]["answer"]["evidence"] == []
    assert payload["data"]["answer"]["evidence_gap"] == expected_gap
    assert expected_gap in payload["summary"]
    assert payload["data"]["cited_chunks"] == []


def test_clarify_resume_skips_extraction(tool, monkeypatch):
    """追问恢复走 pending 上下文,不依赖 LLM 再提取(确定性续跑)。"""
    seen = {}

    def fake_retrieve(*a, **kw):
        seen["query"] = a[2] if len(a) > 2 else kw.get("query")
        return {"kb": {"id": "kb1", "name": "强检目录"},
                "subgraph": {"nodes": [], "edges": []},
                "chunks": [{"docId": "d1", "text": "贸易结算压力表强检"}],
                "diagnostics": {}}
    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence", fake_retrieve)
    mem = FakeMem()
    cfg = mta._current_scenarios()['mandatory_check']
    mem.set("pending", {"scenario": "mandatory_check", 'slot': 'purpose', 'header': '器具用途',
                        'question_id': 'legacy-explicit-question',
                        'snapshot': mta.declaration_snapshot(cfg), 'user_input': '压力表要强检吗',
                        "slots": {"instrument": "压力表", "purpose": ""}})
    ctx = FakeCtx(FakeLLM(conclusion=_answer()), mem=mem)
    r = _run(tool, ctx, "贸易结算", clarify={"器具用途": "贸易结算"})
    assert "属于强制检定" in r.summary
    assert mem.get("pending") is None
    assert mem.get("slots")["purpose"] == "贸易结算"


# ── 场景二:进度查询 ──

def test_missing_order_asks(tool):
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "帮我查一下检测进度")
    assert r.ask is not None
    assert r.ask.questions[0].header == "委托单号"
    assert ctx.session_state.get("pending")["scenario"] == "progress"


def test_progress_ok(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=True, status="ok",
            data={"record": {"order_no": "BJ2026", "instrument": "数字压力计",
                             "status": "检测中", "progress": "3/5",
                             "current_step": "压力示值误差检定",
                             "eta": "2026-10-30",
                             "queried_at": "2026-10-26 10:00"}},
            source={"datasource": "计量业务平台", "tool": "query_progress"}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ2026"}))
    r = _run(tool, ctx, "查一下 BJ2026 到哪一步了")
    assert r.artifact["type"] == "metrology_progress"
    assert r.artifact["status"] == "检测中"
    assert "进度 3/5" in r.summary
    assert r.formatted["orderNo"] == "BJ2026"
    assert r.artifact["pack"] == "metrology_assistant"
    assert r.model_dump(mode="json")["artifact"]["resultDisplay"] == "card"


@pytest.mark.parametrize("status", [
    "forbidden", "not_found", "unsupported",
])
def test_notice_does_not_shadow_artifact_with_reply(status):
    result = mta._notice(status, "业务通知", "完整用户话术", "BJ2026")
    assert result.reply is None
    assert result.error_for_llm is None
    assert result.ask is None
    assert result.artifact_type == "data"
    assert result.artifact["message"] == result.summary == "完整用户话术"
    assert result.model_dump(mode="json")["artifact"]["resultDisplay"] == "card"


def test_progress_forbidden_reply(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status="forbidden",
            error=type("E", (), {"message": "该委托单不属于当前主体"})(),
            source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ8888"}))
    r = _run(tool, ctx, "查 BJ8888")
    assert "不属于当前主体" in r.artifact["message"]
    assert "无权查询" in r.artifact["message"]
    assert r.reply is None
    assert r.summary == r.artifact["message"]
    assert r.artifact == {
        "type": "metrology_notice", "status": "forbidden",
        "title": "无权查询", "message": r.summary, "orderNo": "BJ8888",
        "pack": "metrology_assistant", "resultDisplay": "card"}
    assert r.artifact_type == "data"


def test_progress_not_found_reply(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status="not_found",
            error=type("E", (), {"message": "未找到"})(), source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ9000"}))
    r = _run(tool, ctx, "BJ9000 什么进度了")
    assert "未找到委托单 BJ9000" in r.artifact["message"]
    assert "请核对单号后重试" in r.summary
    assert r.reply is None
    assert r.summary == r.artifact["message"]
    assert r.artifact["type"] == "metrology_notice"
    assert r.artifact["status"] == "not_found"
    assert r.artifact["title"] == "未找到委托单"
    assert r.artifact["message"] == r.summary
    assert r.artifact["orderNo"] == "BJ9000"
    assert r.artifact_type == "data"


def test_progress_timeout_reports_error_not_empty(tool, monkeypatch):
    """接口故障如实报错,不冒充无数据(故事 5/演示④)。"""
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status="unavailable",
            error=type("E", (), {"message": "timeout"})(), source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ0000"}))
    r = _run(tool, ctx, "BJ0000 进度")
    assert r.artifact is None
    assert r.reply is None and r.ask is None
    assert r.error_for_llm == "业务接口查询失败(unavailable): timeout。已如实上报,请稍后重试。"


def test_progress_empty_record_not_fake_success(tool, monkeypatch):
    """信封 ok 但 record 空 → 不装成功(实测教训:BJ0000 曾返回空状态卡)。"""
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=True, status="ok", data={"record": {}}, source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ0000"}))
    r = _run(tool, ctx, "查 BJ0000")
    assert r.artifact is None
    assert r.error_for_llm == "业务平台未返回委托单 BJ0000 的有效数据,请稍后重试。"


def test_progress_datasource_unconfigured(tool, monkeypatch):
    monkeypatch.delenv("METROLOGY_PROGRESS_DATASOURCE_ID", raising=False)
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "instrument": "",
                                   "purpose": "", "order_no": "BJ2026"}))
    r = _run(tool, ctx, "查 BJ2026")
    assert r.error_for_llm is not None
    assert "METROLOGY_PROGRESS_DATASOURCE_ID" in r.error_for_llm


def test_other_scenario_honest_scope(tool):
    ctx = FakeCtx(FakeLLM(extract={"scenario": "other", "instrument": "",
                                   "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "今天天气怎么样")
    assert "5类问题" in r.artifact["message"]
    assert "价目信息" in r.artifact["message"]
    assert r.reply is None
    assert r.artifact_type == "data"
    assert r.artifact == {"type": "metrology_notice", "status": "unsupported",
                           "title": "暂不支持此问题", "message": r.summary,
                           "pack": "metrology_assistant", "resultDisplay": "card"}
    assert r.summary == r.artifact["message"]


def test_format_result_legacy_kg_fields_unchanged(tool):
    assert tool.format_result({
        "type": "kg_search_result", "kb": {"name": "旧知识库"},
        "sources": {"chunks": [{"text": "旧片段"}]},
    }) == {"kb": "旧知识库", "chunkCount": 1}


def test_progress_execution_exception_returns_safe_error(tool, monkeypatch, caplog):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")

    def fail(*a, **kw):
        raise RuntimeError("password=secret /internal/path")

    monkeypatch.setattr("domains.chatbi.controlled.execute_controlled", fail)
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "order_no": "BJ2026"}))
    r = _run(tool, ctx, "查 BJ2026")
    assert r.artifact is None
    assert r.error_for_llm == "业务接口查询失败,已如实上报,请稍后重试。"
    assert "secret" not in str(r.model_dump())
    assert any(record.name == mta.__name__ and record.exc_info for record in caplog.records)


def test_structured_refs_drive_citations_and_survive_serialization(tool, monkeypatch):
    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence",
        lambda *a, **kw: {"chunks": [{"text": "目录一"}, {"text": "目录二"}]})
    answer = _answer(refs=[2, 1, 2])
    answer["evidence"].append({"text": "另一条依据", "chunk_refs": [1]})
    ctx = FakeCtx(FakeLLM(extract={"scenario": "mandatory_check", "purpose": "贸易结算"},
                          conclusion=answer))
    result = _run(tool, ctx, "要强检吗")
    saved = result.model_dump(mode="json")["artifact"]
    assert saved["answer"] == answer
    assert saved["pack"] == "metrology_assistant"
    assert saved["resultDisplay"] == "card"
    assert saved["cited_chunks"] == [1, 2]
    assert "[片段:2] [片段:1]" in result.summary
    assert "renderer" not in saved and "presentation" not in saved


def test_invalid_conclusion_propagates_without_artifact(tool, monkeypatch):
    monkeypatch.setattr(
        "domains.knowledge_graph.service.retrieve_evidence",
        lambda *a, **kw: {"chunks": [{"text": "目录"}]})
    ctx = FakeCtx(FakeLLM(extract={"scenario": "mandatory_check", "purpose": "贸易结算"},
                          conclusion=_answer(refs=[2])))
    with pytest.raises(ValueError, match="结论"):
        _run(tool, ctx, "要强检吗")
    assert ctx.session_state.get("scenario") is None


@pytest.mark.parametrize("status", ["unavailable", "transport", "credential", "invalid_input", "error"])
def test_progress_technical_failure_returns_error(tool, monkeypatch, status):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status=status,
            error=ControlledError(status, "安全的完整诊断", "permanent", False)))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "order_no": "BJ2026"}))
    result = _run(tool, ctx, "查 BJ2026")
    assert result.artifact is None
    assert result.reply is None and result.ask is None
    assert result.error_for_llm == (
        f"业务接口查询失败({status}): 安全的完整诊断。已如实上报,请稍后重试。")


def test_progress_failure_without_diagnostic_returns_error(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(ok=False, status="invalid_input"))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress", "order_no": "BJ2026"}))
    result = _run(tool, ctx, "查 BJ2026")
    assert result.artifact is None
    assert result.error_for_llm == (
        "业务接口查询失败(invalid_input): invalid_input。已如实上报,请稍后重试。")


@pytest.mark.parametrize("status", [
    "unavailable", "transport", "credential", "invalid_input", "error",
    "exception", "empty_record", "forbidden", "not_found", "unsupported",
])
def test_real_engine_routes_failure_and_notice(tool, monkeypatch, status):
    from engine import nodes
    from sdk.registry import ToolRegistry

    # Restore every process-global dependency replaced by configure.
    for name in ("_registry", "_llm_client", "_asset_client", "_conversation",
                 "_prompt_loader", "_pack_routers", "_pack_configs"):
        monkeypatch.setattr(nodes, name, getattr(nodes, name))
    monkeypatch.setattr(
        "domains.knowledge_graph.runtime.get_kg_store",
        lambda app: SimpleNamespace(list_kbs=lambda: []))
    monkeypatch.setenv("METROLOGY_PROGRESS_DATASOURCE_ID", "ds-1")

    def controlled(*a, **kw):
        if status == "exception":
            raise RuntimeError("password=secret /internal/path")
        if status == "empty_record":
            return ControlledResult(ok=True, status="ok", data={"record": {}})
        return ControlledResult(
            ok=False, status=status,
            error=ControlledError(status, "安全的完整诊断", "permanent", False))

    monkeypatch.setattr("domains.chatbi.controlled.execute_controlled", controlled)
    registry = ToolRegistry()
    registry.register(tool)
    traces = []
    nodes.configure(
        registry=registry,
        llm_client=FakeLLM(extract={
            "scenario": "other" if status == "unsupported" else "progress",
            "order_no": "BJ2026"}),
        asset_client=None,
        conversation=SimpleNamespace(append=lambda *args: traces.append(args[2])),
        prompt_loader=None, pack_routers={}, pack_configs={})
    state = {"tool_name": tool.name, "conversation_id": "conv-test",
             "tool_state": {"user_input": "查 BJ2026"}}
    update = nodes.execute_tool_node(state)
    events = nodes.handle_result_node({**state, **update})["sse_events"]
    trace = next(t for t in traces if t["stage"] == "tool_execute")
    assert len(events) == 1 and events[0]["type"] == "result"
    payload = events[0]["data"]
    if status in ("forbidden", "not_found", "unsupported"):
        assert trace["status"] == "ok"
        assert "error" not in payload and "reply" not in payload
        assert payload["artifactType"] == "data"
        assert payload["data"]["type"] == "metrology_notice"
        assert payload["data"]["status"] == status
        assert payload["data"]["pack"] == "metrology_assistant"
        assert payload["data"]["resultDisplay"] == "card"
        assert payload["data"]["message"] == payload["summary"]
    else:
        assert trace["status"] == "error"
        assert trace["detail"]["error"] == update["tool_result"]["error_for_llm"]
        assert payload["error"] is True
        assert payload["message"] == update["tool_result"]["error_for_llm"]
        assert "artifactType" not in payload and "data" not in payload
        assert "secret" not in str(payload)


def test_unexpected_knowledge_service_exception_is_safe(tool, monkeypatch, caplog):
    def fail(*a, **kw):
        raise RuntimeError("password=secret /internal/path")

    ctx = FakeCtx(FakeLLM(extract={"scenario": "mandatory_check", "purpose": "贸易结算"}))
    monkeypatch.setattr("domains.knowledge_graph.service.retrieve_evidence", fail)
    result = _run(tool, ctx, "要强检吗")
    assert result.error_for_llm == "知识库检索失败,请稍后重试。"
    assert result.artifact is None
    assert "secret" not in str(result.model_dump())
    assert any(record.name == mta.__name__ and record.exc_info for record in caplog.records)


def test_preflight_only_checks_assembly(tool, monkeypatch):
    """自动路由后本域承载 MCP 工具查询:preflight 不再硬卡知识库
    (Neo4j 故障不应挡住 workload 类问题);kg 故障在执行层如实报错
    (上一测试锁死);未装配仍前置拦截。"""

    def fail(*a, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr("domains.knowledge_graph.runtime.get_kg_store", fail)
    assert tool.preflight({}, FakeCtx(FakeLLM())) is None
    unmounted = mta.MetrologyAssistantTool(app_state=None)
    r = unmounted.preflight({}, FakeCtx(FakeLLM()))
    assert r is not None and "未正确装配" in r.error_for_llm


def test_auto_optional_param_extracted_and_sent(tool, monkeypatch):
    """多参数自动工具:可选参数进提取提示(_param_hints),用户说到
    才随请求出站,没说不出站(回归锁:此前可选参数永不可提取)。"""
    from domains.metrology_assistant import capability_import as ci
    auto = {"query_rich": {
        "label": "富查询", "intent": "问富查询相关问题",
        "required": {"slot": "name", "header": "名称",
                     "question": "请提供名称:", "options": []},
        "action": {"kind": "chatbi", "server": "metrology",
                   "datasource_id": "", "env": "", "tool": "query_rich",
                   "params": {"name": "name", "model": "model"},
                   "stage": "调用 query_rich 查询…", "error_noun": "富查询",
                   "notices": {}, "card": "_table_card"},
        "_param_hints": {"model": "型号规格过滤"},
        "_auto": True}}
    monkeypatch.setattr(ci, "auto_scenarios", lambda table: auto)
    monkeypatch.setattr(ci, "resolve_datasource_for_tool",
                        lambda server, t: "ds-9")
    seen = {}

    def fake_exec(app, ds_id, tool_name, params):
        seen.clear()
        seen.update(params)
        return ControlledResult(
            ok=True, status="ok",
            data={"columns": ["name"], "rows": [["压力表"]]},
            source={"datasource": "通用源", "tool": "query_rich"})
    monkeypatch.setattr("domains.chatbi.controlled.execute_controlled",
                        fake_exec)

    ctx = FakeCtx(FakeLLM(extract={"scenario": "query_rich",
                                   "name": "压力表", "model": "0.4级"}))
    r = _run(tool, ctx, "查一下 0.4级 的压力表")
    assert r.artifact["type"] == "metrology_table"
    assert seen == {"name": "压力表", "model": "0.4级"}

    ctx2 = FakeCtx(FakeLLM(extract={"scenario": "query_rich",
                                    "name": "压力表", "model": ""}))
    _run(tool, ctx2, "查压力表")
    assert seen == {"name": "压力表"}


def test_router_direct_no_llm_call():
    """确定性路由:单主工具直达,零路由 LLM(设计 5.1 层2/§9)。"""
    from domains.metrology_assistant import pack as mpack

    class BoomLLM:
        def chat_json(self, *a, **kw):
            raise AssertionError("路由不应调用 LLM")

    router = mpack.create_router(mpack.create_registry(None))
    assert router.route("随便问点什么", None, history="",
                        llm_client=BoomLLM(), conv_id="c1") \
        == ("metrology_assistant", 1.0)


# ── 场景三: 器具信息查询(价目/周期/强检归类, 走 query_instrument) ──

_KERATOMETER_ROWS = {"columns": ["name", "model", "measure_range", "accuracy",
                                 "fee_yuan", "mandatory_l1", "mandatory_l2",
                                 "period_months", "regulation"],
                     "rows": [["角膜曲率计", None, "半径(6.5~9.4)mm", None, 0,
                               "验光仪器", "角膜曲率计", 12, None]]}


def test_instrument_info_card(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", "ds-fee")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=True, status="ok", data=_KERATOMETER_ROWS, source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "角膜曲率计", "purpose": "",
                                   "order_no": ""}))
    r = _run(tool, ctx, "角膜曲率计的检测校准周期是多久")
    art = r.artifact
    assert art["type"] == "metrology_instrument"
    assert art["pack"] == "metrology_assistant"
    assert art["resultDisplay"] == "card"
    rec = art["instruments"][0]
    assert rec["name"] == "角膜曲率计" and rec["periodMonths"] == 12
    assert "12" in r.summary and "角膜曲率计" in r.summary


def test_instrument_info_multi_row_keeps_all(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", "ds-fee")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=True, status="ok", data={"columns": ["name", "period_months"],
                                        "rows": [["无线温度记录器", 3], ["无线温度记录器", 12]]},
            source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "无线温度记录器", "purpose": "",
                                   "order_no": ""}))
    r = _run(tool, ctx, "无线温度记录器多久校准一次")
    assert len(r.artifact["instruments"]) == 2


def test_instrument_info_not_found_notice(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", "ds-fee")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status="not_found",
            error=type("E", (), {"message": "未找到"})(), source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "光速引擎", "purpose": "",
                                   "order_no": ""}))
    r = _run(tool, ctx, "光速引擎的检测费")
    assert r.artifact["type"] == "metrology_notice"
    assert r.artifact["status"] == "not_found"


def test_instrument_info_missing_name_asks(tool):
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "", "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "检测费多少钱")
    assert r.ask is not None


def test_instrument_info_unconfigured(tool, monkeypatch):
    monkeypatch.delenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", raising=False)
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "电子秤", "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "电子秤检测费")
    assert r.error_for_llm is not None
    assert "METROLOGY_INSTRUMENT_DATASOURCE_ID" in r.error_for_llm


def test_instrument_info_transport_failure_is_error(tool, monkeypatch):
    monkeypatch.setenv("METROLOGY_INSTRUMENT_DATASOURCE_ID", "ds-fee")
    monkeypatch.setattr(
        "domains.chatbi.controlled.execute_controlled",
        lambda *a, **kw: ControlledResult(
            ok=False, status="unavailable",
            error=type("E", (), {"message": "timeout"})(), source={}))
    ctx = FakeCtx(FakeLLM(extract={"scenario": "instrument_info",
                                   "instrument": "电子秤", "purpose": "", "order_no": ""}))
    r = _run(tool, ctx, "电子秤检测费")
    assert r.error_for_llm is not None
    assert r.artifact is None


# ── 自动路由(注册即识别):未编排 MCP 工具零配置可用 ──────────────

_FRESH_AUTO = {
    "label": "查询最新活动",
    "intent": "问查询最新活动相关问题",
    "required": {"slot": "keyword", "header": "关键词",
                 "question": "请提供关键词:", "options": []},
    "action": {"kind": "chatbi", "server": "metrology",
               "datasource_id": "", "env": "", "tool": "query_fresh",
               "params": {"keyword": "keyword", "note": "note"},
               "stage": "调用 query_fresh 查询…",
               "error_noun": "活动", "notices": {},
               "card": "_table_card"},
    "_auto": True,
}


def test_runtime_scenarios_merges_auto_and_table_wins(monkeypatch):
    """运行时表 = 场景表 ∪ 自动合成;同 key 场景表(润色)优先。"""
    from domains.metrology_assistant import capability_import as ci
    monkeypatch.setattr(ci, "auto_scenarios",
                        lambda table: {"query_fresh": _FRESH_AUTO})
    merged = mta._runtime_scenarios()
    assert "query_fresh" in merged and "progress" in merged
    # 同 key(场景表已有 progress):自动合成不得覆盖润色
    monkeypatch.setattr(ci, "auto_scenarios",
                        lambda table: {"progress": {"label": "AUTO"}})
    assert mta._runtime_scenarios()["progress"]["label"] != "AUTO"


def test_auto_scenarios_skips_edited_and_unexecutable(monkeypatch):
    """合成规则:场景表已编排的工具(action.tool 判定)不合成;无
    数据源可执行的工具跳过;有源工具产出可执行的同构场景行。"""
    from types import SimpleNamespace as _NS
    from domains.metrology_assistant import capability_import as ci
    from domains.chatbi import datasources as bi_ds
    from domains.chatbi import runtime as bi_runtime

    from datetime import datetime as _dt, timezone as _tz
    _fresh = _dt.now(_tz.utc).isoformat()
    monkeypatch.setattr(ci, "_servers_rows", lambda: [
        {"id": "sid-1", "code": "metrology", "enabled": True,
         "updated_at": _fresh, "tools_snapshot": [
            {"name": "query_progress", "inputSchema": {
                "properties": {"order_no": {"type": "string"}},
                "required": ["order_no"]}},   # 已编排(场景表 action.tool)
            {"name": "query_fresh", "description": "查询最新活动",
             "inputSchema": {"properties": {"keyword": {"type": "string",
                                              "description": "关键词"}},
                             "required": ["keyword"]}},
         ]},
        {"id": "sid-2", "code": "lonely", "enabled": True,
         "updated_at": _fresh, "tools_snapshot": [
            {"name": "query_orphan", "inputSchema": {
                "properties": {"x": {"type": "string"}}, "required": ["x"]}},
         ]},
    ])
    fake_info = _NS(id="ds-9", host="metrology", db_type="mcp",
                    database="", is_active=True)
    monkeypatch.setattr(bi_ds, "list_datasources",
                        lambda db, active_only=False: [fake_info])
    monkeypatch.setattr(bi_runtime, "get_pack_db", lambda: object())

    table = mta._current_scenarios()
    auto = ci.auto_scenarios(table)
    assert "query_progress" not in auto    # 润色优先,不重复
    assert "query_orphan" not in auto      # 无数据源可执行 → 跳过
    fresh = auto["metrology::query_fresh"]
    assert fresh["action"]["server"] == "metrology"
    assert fresh["required"]["slot"] == "keyword"
    assert fresh["required"]["question"].startswith("请提供keyword")
    assert fresh["action"]["card"] == "_table_card"
    assert fresh["_auto"] is True


def test_auto_tool_asks_then_executes_with_runtime_ds(tool, monkeypatch):
    """未编排工具全链路:缺槽通用追问 → 补答 → 执行期按 server+tool
    解析数据源;未提到的参数不传出站(空串过滤)。"""
    from domains.metrology_assistant import capability_import as ci
    monkeypatch.setattr(ci, "auto_scenarios",
                        lambda table: {"query_fresh": _FRESH_AUTO})
    monkeypatch.setattr(ci, "resolve_datasource_for_tool",
                        lambda server, t: "ds-9" if t == "query_fresh" else None)

    def fake_exec(app, ds_id, tool, params):
        if ds_id == "ds-9" and tool == "query_fresh":
            assert params == {"keyword": "K"}, f"空参数必须过滤: {params}"
            return ControlledResult(
                ok=True, status="ok",
                data={"columns": ["keyword", "title"],
                      "rows": [["K", "十月检定活动"]]},
                source={"datasource": "计量业务平台(通用)",
                        "tool": "query_fresh"})
        return ControlledResult(
            ok=False, status="invalid_input",
            error=ControlledError(code="invalid_input",
                                  message=f"unexpected {ds_id}/{tool}",
                                  error_type="permanent", retryable=False))
    monkeypatch.setattr("domains.chatbi.controlled.execute_controlled",
                        fake_exec)

    # ① 缺槽 → 通用追问(合成场景的 header/文案)
    ctx = FakeCtx(FakeLLM(extract={"scenario": "query_fresh", "keyword": ""}))
    r = _run(tool, ctx, "帮我查下最新活动")
    assert r.ask is not None and r.ask.questions[0].header == "关键词"

    # ② 补答 → 执行期解析数据源 → 通用表格卡
    r2 = _run(tool, ctx, "K", clarify={"关键词": "K"})
    assert r2.artifact is not None
    assert r2.artifact["type"] == "metrology_table"
    assert r2.artifact["rows"] == [["K", "十月检定活动"]]
    assert r2.artifact["source"]["tool"] == "query_fresh"
