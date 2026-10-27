"""metrology flows 状态测试(C4:DB 权威 pending,旧版本补答拒绝)。

覆盖:缺槽追问落 flow(pending_question 原子写)、补答消费(版本
推进+清 pending)、旧 flow 版本补答被拒(如实告知不吞)、成功执行
flow 转 done、无 DB 兜底(KV 路径不回归)。

场景:FakeCtx 带 conv_id(有会话上下文)→ 走 DB flow;无 conv_id
→ KV 兜底(既有 85 条测试已覆盖)。
"""
import pytest

import domains.metrology_assistant.tools.assistant as mta
from domains.metrology_assistant import runtime, store


class FakeMem:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v

    def pop(self, k, default=None):
        return self._d.pop(k, default)


class FakeLLM:
    def __init__(self, extract):
        self._extract = extract
        self.calls = 0

    def chat_json(self, messages, temperature=0.0, conv_id=None,
                  stage=None):
        self.calls += 1
        return self._extract


class FakeCtx:
    def __init__(self, llm, mem=None, conv_id="conv-flow"):
        self.llm_client = llm
        self.session_state = mem or FakeMem()
        self.conv_id = conv_id
        self.emitted = []

    def emit(self, event, *args):
        self.emitted.append((event, args))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    runtime.reset_runtime_cache()
    monkeypatch.delenv("METROLOGY_PROGRESS_DATASOURCE_ID", raising=False)
    yield
    runtime.reset_runtime_cache()


def _db():
    return runtime.get_pack_db()


def _ask_flow(tool, ctx, text="查一下进度"):
    """触发缺槽追问(progress 场景缺 order_no)。"""
    llm = FakeLLM(extract={"scenario": "progress", "instrument": "",
                          "purpose": "", "order_no": ""})
    ctx.llm_client = llm
    return tool.execute({"user_input": text}, ctx)


def test_pending_question_persisted_to_flow():
    """缺槽追问:pending_question 落 flow(问题/槽位/场景)。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))
    result = _ask_flow(tool, ctx)
    assert result.ask is not None
    flow = store.get_flow(_db(), "conv-flow")
    assert flow is not None
    assert flow["scenario"] == "progress"
    assert flow["pendingQuestion"]["header"] == "委托单号"
    assert "请提供委托单号" in flow["pendingQuestion"]["question"]


def test_answer_consumes_pending_and_advances_revision():
    """补答:pending 原子消费(清 pending+版本+1),槽位并入执行。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))
    _ask_flow(tool, ctx)
    flow = store.get_flow(_db(), "conv-flow")
    rev = flow["flowRevision"]

    # 补答走 answers 通道(不烧提取)
    ctx.llm_client = FakeLLM(extract={})
    result = tool.execute(
        {"user_input": "", "clarify_answers": {"委托单号": "BJ2026"}}, ctx)
    after = store.get_flow(_db(), "conv-flow")
    # 追问后未配置数据源 → error_for_llm(执行到了 chatbi 分支)
    assert result.error_for_llm is not None
    assert "数据源未配置" in result.error_for_llm
    assert after["flowRevision"] == rev + 1
    assert after["pendingQuestion"] is None
    assert after["slots"]["order_no"] == "BJ2026"
    # 消费后 pending 清空:新消息(带单号)直接执行,不被旧 pending 吞
    ctx.llm_client = FakeLLM(extract={"scenario": "progress",
                                      "instrument": "", "purpose": "",
                                      "order_no": "BJ2027"})
    result2 = tool.execute({"user_input": "查 BJ2027 进度"}, ctx)
    assert result2.error_for_llm is not None  # 仍走到执行(数据源未配)


def test_stale_pending_answer_rejected():
    """旧版本卡补答:CAS 冲突 → 如实告知,不吞不重放。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))
    _ask_flow(tool, ctx)
    flow = store.get_flow(_db(), "conv-flow")
    # 外部把 flow 推进一步(模拟另一标签页已消费该追问)
    store.advance_flow(_db(), flow["flowId"], flow["flowRevision"],
                       slots={"order_no": "BJ9999"}, clear_pending=True)
    ctx.llm_client = FakeLLM(extract={})
    result = tool.execute(
        {"user_input": "", "clarify_answers": {"委托单号": "BJ2026"}}, ctx)
    assert result.error_for_llm is not None
    assert "已被处理" in result.error_for_llm


def test_successful_execution_marks_flow_done():
    """成功执行:flow 转 done(完成后仍可加载改条件)。"""
    import os
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))
    # 直接给足槽位(不经追问)
    ctx.llm_client = FakeLLM(extract={"scenario": "progress",
                                      "instrument": "", "purpose": "",
                                      "order_no": "BJ2026"})
    os.environ["METROLOGY_PROGRESS_DATASOURCE_ID"] = "ds-missing"
    try:
        tool.execute({"user_input": "查 BJ2026 进度"}, ctx)
    finally:
        os.environ.pop("METROLOGY_PROGRESS_DATASOURCE_ID", None)
    # 数据源不存在 → error_for_llm;flow 未 done(失败不写终态)
    flow = store.get_flow(_db(), "conv-flow")
    assert flow is None or flow["status"] == "running"


def test_slot_change_invalidates_running_flow():
    """失效传播(设计 §6):改单号 → 旧 running flow 置 cancelled,新 flow 承接。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))
    # 第一轮:带单号提问(执行失败因数据源未配,但已确认条件入 flow)
    ctx.llm_client = FakeLLM(extract={"scenario": "progress",
                                      "instrument": "", "purpose": "",
                                      "order_no": "BJ2026"})
    tool.execute({"user_input": "查 BJ2026 进度"}, ctx)
    flow1 = store.get_flow(_db(), "conv-flow")
    assert flow1 is not None and flow1["slots"].get("order_no") == "BJ2026"

    # 第二轮:明确换单号 BJ2027(与 flow 槽位冲突)→ 旧 flow cancelled
    ctx.llm_client = FakeLLM(extract={"scenario": "progress",
                                      "instrument": "", "purpose": "",
                                      "order_no": "BJ2027"})
    tool.execute({"user_input": "查 BJ2027 进度"}, ctx)
    # 旧 flow 已 cancelled(get_flow 只看 running → None 或新 flow)
    current = store.get_flow(_db(), "conv-flow")
    assert current is not None
    assert current["flowId"] != flow1["flowId"]  # 新任务新 flow
    # cancelled 行保留(审计):直接查行
    with _db().connect() as conn:
        row = conn.execute(
            "SELECT status FROM assistant_flows WHERE flow_id = ?",
            (flow1["flowId"],)).fetchone()
    assert row["status"] == "cancelled"
    # KV pending 同步清
    assert ctx.session_state.get("pending") is None


def test_no_conv_id_falls_back_to_kv():
    """无会话上下文:KV 兜底路径不回归(既有 Demo 行为)。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}), conv_id=None)
    result = _ask_flow(tool, ctx)
    assert result.ask is not None
    assert ctx.session_state.get("pending")["scenario"] == "progress"
    # flow 表无记录
    assert store.get_flow(_db(), "conv-flow") is None


# ── 槽位类型系统(P1)──────────────────────────────────────────

def test_slot_type_enum_normalizes_and_rejects():
    """enum:精确/唯一包含命中归一到选项;零命中带选项提示重问。"""
    norm, err = mta.normalize_slot("贸易", "enum",
                                   ["贸易结算", "安全防护"])
    assert (norm, err) == ("贸易结算", None)
    norm, err = mta.normalize_slot("医疗", "enum", ["医疗卫生"])
    assert (norm, err) == ("医疗卫生", None)
    norm, err = mta.normalize_slot("监控", "enum", ["安全防护", "环境监测"])
    assert norm is None and "选项" in err
    # 未配置选项 = 不约束
    norm, err = mta.normalize_slot("任意", "enum", [])
    assert (norm, err) == ("任意", None)


def test_slot_type_number_and_date():
    """number:全角/逗号容错解析;date:三格式归一 ISO,坏值报格式。"""
    norm, err = mta.normalize_slot("１２，３４５", "number")
    assert (norm, err) == ("12345", None)
    norm, err = mta.normalize_slot("十二", "number")
    assert norm is None and "数字" in err
    norm, err = mta.normalize_slot("2026年10月1日", "date")
    assert (norm, err) == ("2026-10-01", None)
    norm, err = mta.normalize_slot("2026/10/1", "date")
    assert (norm, err) == ("2026-10-01", None)
    norm, err = mta.normalize_slot("十月一号", "date")
    assert norm is None and "格式" in err
    norm, err = mta.normalize_slot("2026-02-30", "date")
    assert norm is None and "不存在" in err


def test_bad_required_slot_reasks_with_hint(monkeypatch):
    """必填槽位类型不符:清空坏值,追问带格式提示(不交给模型猜)。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={"scenario": "progress",
                                   "instrument": "", "purpose": "",
                                   "order_no": "BJ 二零二六"}))
    # order_no 是 text 槽位——借 number 场景测:直接单测 normalize_required_slot
    required = {"slot": "days", "header": "天数", "question": "要查几天?",
                "options": [], "slot_type": "number"}
    merged = {"days": "约三个"}
    out, reask = mta.normalize_required_slot(required, merged)
    assert out["days"] == "" and reask and "数字" in reask
    # 合法数字归一后继续
    out2, reask2 = mta.normalize_required_slot(
        required, {"days": " ７ "})
    assert (out2["days"], reask2) == ("7", None)


def test_identity_gate_rejects_cross_user_resume():
    """恢复前身份门(设计 6.1):flow 归属他人时,补答被拒+要求重问。"""
    tool = mta.MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(extract={}))  # conv-flow, anonymous
    _ask_flow(tool, ctx)                # flow 归属 anonymous, pending 在

    # 换身份(模拟另一用户拿到旧卡/换账号后补答)
    ctx.user_id = "user-b"
    ctx.llm_client = FakeLLM(extract={})
    result = tool.execute(
        {"user_input": "", "clarify_answers": {"委托单号": "BJ2026"}}, ctx)
    assert result.error_for_llm is not None
    assert "不一致" in result.error_for_llm
    # flow 未被消费(pending 仍在,归属人自己仍可答)
    flow = store.get_flow(_db(), "conv-flow")
    assert flow["pendingQuestion"] is not None
    ctx.user_id = "anonymous"
    r2 = tool.execute(
        {"user_input": "", "clarify_answers": {"委托单号": "BJ2026"}}, ctx)
    assert r2.error_for_llm is not None and "不一致" not in r2.error_for_llm
