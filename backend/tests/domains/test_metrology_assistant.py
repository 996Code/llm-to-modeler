"""metrology_assistant 薄编排工具单测。

覆盖 13.1 验收故事对应的核心行为:
- 故事1: 缺用途只追问必要条件(pending 上下文落会话 KV)
- 故事2: 用途→资料库映射(改用途换库)
- 故事3: 无依据时如实"无法确定",不编造
- 故事4/5: 缺单号追问;拒权/查无/故障分别显示
- 追问恢复: clarify_answers 确定性续跑,不再烧提取
"""
import pytest

from domains.chatbi.controlled import ControlledResult
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
    """chat_json → 提取结果;chat → 三态结论文本。"""

    def __init__(self, extract=None, conclusion=""):
        self._extract = extract or {}
        self._conclusion = conclusion

    def chat_json(self, messages=None, temperature=None, stage=None,
                  conv_id=None, **kw):
        return self._extract

    def chat(self, messages=None, **kw):
        return self._conclusion


class FakeCtx:
    def __init__(self, llm, mem=None):
        self.llm_client = llm
        self.session_state = mem or FakeMem()
        self.conv_id = "conv-test"
        self.emitted = []

    def emit(self, event, *args):
        self.emitted.append((event, args))


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
                   "purpose": "贸易结算", "order_no": "BJ2026"}


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
    assert mta.parse_clarify_answers({"text": "帮我查 BJ2027 这个单"}) == {
        "order_no": "BJ2027"}
    assert mta.parse_clarify_answers({"text": "用于贸易结算的"}) == {
        "purpose": "贸易结算"}


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


# ── 场景一:强检判定 ──

def _run(tool, ctx, user_input, clarify=None, mem=None):
    state = {"user_input": user_input}
    if clarify:
        state["clarify_answers"] = clarify
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
        conclusion="结论: 属于强制检定\n依据: 列入目录 [片段:1]\n建议: 按周期送检"))
    r = _run(tool, ctx, "压力表用于贸易结算,要强检吗")
    assert r.ask is None
    assert r.artifact["type"] == "kg_search_result"
    assert r.artifact["kb"]["name"] == "强检目录"
    assert r.artifact["cited_chunks"] == [1]
    assert r.artifact["sources"]["chunks"][0]["docName"] == "强检目录2026.pdf"
    assert "属于强制检定" in r.summary


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
    mem.set("pending", {"scenario": "mandatory_check",
                        "slots": {"instrument": "压力表", "purpose": "",
                                  "order_no": ""}})
    ctx = FakeCtx(FakeLLM(conclusion="结论: 属于强制检定 [片段:1]"), mem=mem)
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
    assert "不属于当前主体" in r.reply
    assert "无权查询" in r.reply
    # general 回复链路前端展示 summary(conversation.ts),reply/summary 必须同口径
    assert r.summary == r.reply


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
    assert "未找到委托单 BJ9000" in r.reply
    assert "请核对单号后重试" in r.summary
    assert r.summary == r.reply


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
    assert r.error_for_llm is not None
    assert "unavailable" in r.error_for_llm or "查询失败" in r.error_for_llm


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
    assert r.error_for_llm is not None
    assert "有效数据" in r.error_for_llm


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
    assert "两类问题" in r.reply


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
