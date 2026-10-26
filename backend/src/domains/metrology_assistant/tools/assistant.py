"""计量智能客服主工具(南京院竞标 Demo 薄编排,临时件)。

七步管线的薄版(设计文档 0.5.0/C1):
  场景路由 → LLM 槽位提取 → 记忆/追问答案合并 → 必要条件追问(AskSpec)
  → 规则定来源 → 调底层(知识库证据服务 / ChatBI 受控执行) → 组业务回答

临时件边界(Demo 说了算,P1 重构):
- 不建 flows 表,多轮状态用会话 KV(session_state)轻量承载;
- 用途→资料库映射、演示单号选项是代码内演示配置,P1 移入 DB+管理页;
- 进度查询走 mock 业务平台(tools/mcp-e2e/metrology_server.py),
  拒权/超时为模拟样例——演示话术须如实说明"模拟业务平台"。
"""
import json
import os
import re
from typing import Optional

from sdk.llm_compat import chat_text
from sdk.tool import (
    AskOption, AskQuestion, AskSpec, Tool, ToolContext, ToolResult,
)

# ── 演示配置(P1 移入 DB,管理页三 tab 维护) ──────────────────

# 用途 → 资料库映射:强检目录用途(计量法框架)查「强检目录」库,
# 其余用途查「检定规程」库——"按条件选库"的最小业务分支
STRONG_INSPECTION_PURPOSES = ("贸易结算", "安全防护", "医疗卫生", "环境监测", "执法鉴定")
MANDATORY_CHECK_KB = "强检目录"
REGULATION_KB = "检定规程"

PURPOSE_HEADER = "器具用途"     # 追问 header(≤12 字,回传 answers 的键)
ORDER_HEADER = "委托单号"
PURPOSE_OPTIONS = [
    AskOption(label="贸易结算", description="贸易结算用(如买卖计价、水电气结算)"),
    AskOption(label="安全防护", description="安全防护用(如锅炉压力监控、安全阀)"),
    AskOption(label="医疗卫生", description="医疗卫生用(如血压计、医用监护仪)"),
    AskOption(label="环境监测", description="环境监测用(如环境监测站仪表)"),
]
DEMO_ORDER_OPTIONS = [
    AskOption(label="BJ2026", description="演示单(检测中)"),
    AskOption(label="BJ2027", description="演示单(已完成)"),
]
PROGRESS_TOOL = "query_progress"
PROGRESS_DATASOURCE_ENV = "METROLOGY_PROGRESS_DATASOURCE_ID"
_ORDER_RE = re.compile(r"([A-Z]{2}\d{3,})")

_PIPELINE_STEPS = [
    {"key": "assistant.extract", "label": "识别问题与关键条件"},
    {"key": "assistant.clarify", "label": "补全必要条件"},
    {"key": "assistant.retrieve", "label": "检索资料/查询业务"},
    {"key": "assistant.answer", "label": "生成业务结论"},
]


def _make_monotonic_stage(emit_fn, steps):
    """stage 上屏钳成单行道(与 kb_search 同款:回退/未知键不上屏)。"""
    order = {s["key"]: i for i, s in enumerate(steps)}
    shown = [0]

    def _on_stage(key: str, message: str) -> None:
        idx = order.get(key)
        if idx is None or idx < shown[0]:
            return
        shown[0] = idx
        emit_fn(key, message)

    return _on_stage


# ── 槽位提取与合并 ────────────────────────────────────────────

_EXTRACT_PROMPT = """你是计量业务客服的槽位提取器。从用户消息提取 JSON(只输出 JSON):

{{"scenario": "mandatory_check|progress|other", "instrument": "", "purpose": "", "order_no": ""}}

- scenario: 问器具是否强制检定/检定周期/适用规程 → mandatory_check;
  问检测进度/委托单/报告状态 → progress;其他 → other
- instrument: 器具名称(如 压力表/血压计/电子秤);指代("它/同一台")从会话背景解析
- purpose: 用途(贸易结算/安全防护/医疗卫生/环境监测/执法鉴定/内部使用/校准等);
  消息与背景都没提 → 空串
- order_no: 委托单号(如 BJ2026);没有 → 空串
- 本轮消息明确提到的值优先于背景值(用户改条件以新值为准)

会话背景(可能为空): {background}
用户消息: {user_input}"""


def extract_slots(llm, user_input: str, background: dict,
                  conv_id: Optional[str]) -> dict:
    """LLM 结构化提取(一次调用出场景+三槽位);失败上抛,不静默猜。"""
    prompt = _EXTRACT_PROMPT.format(
        background=json.dumps(background, ensure_ascii=False),
        user_input=user_input)
    data = llm.chat_json([{"role": "user", "content": prompt}],
                         temperature=0.0, conv_id=conv_id,
                         stage="assistant.extract")
    if not isinstance(data, dict):
        raise ValueError(f"槽位提取返回非对象: {type(data).__name__}")

    def _s(key: str) -> str:
        return str(data.get(key) or "").strip()

    scenario = _s("scenario")
    if scenario not in ("mandatory_check", "progress"):
        scenario = "other"
    return {"scenario": scenario, "instrument": _s("instrument"),
            "purpose": _s("purpose"), "order_no": _s("order_no").upper()}


def merge_slots(extracted: dict, mem_slots: dict) -> dict:
    """本轮明确值优先,记忆只补空缺(改条件以新值为准,不串旧值)。"""
    merged = {k: str(mem_slots.get(k) or "").strip()
              for k in ("instrument", "purpose", "order_no")}
    for k in merged:
        if extracted.get(k):
            merged[k] = extracted[k]
    return merged


def parse_clarify_answers(answers: dict) -> dict:
    """追问答案({header: label} 或 {text: 原话})→ 槽位(确定性解析)。"""
    out = {}
    purpose = answers.get(PURPOSE_HEADER)
    if purpose:
        out["purpose"] = str(purpose).strip()
    order = answers.get(ORDER_HEADER)
    if order:
        m = _ORDER_RE.search(str(order).upper())
        out["order_no"] = m.group(1) if m else str(order).strip().upper()
    text = answers.get("text")
    if text:
        m = _ORDER_RE.search(str(text).upper())
        if m:
            out["order_no"] = m.group(1)
        elif "purpose" not in out:
            for p in STRONG_INSPECTION_PURPOSES + ("内部使用", "校准"):
                if p in str(text):
                    out["purpose"] = p
                    break
    return out


# ── 强检判定:证据检索 + 三态结论 ─────────────────────────────

_CONCLUDE_PROMPT = """你是计量检定业务专家。根据资料片段判断:"{instrument}"用于"{purpose}"是否属于强制检定。

要求:
- 结论只能是三态之一:属于强制检定 / 不属于强制检定 / 无法确定(资料不足以支撑);
- 结论句需注明器具与用途(如"用于贸易结算的血压计");用途若沿用会话上文,
  结论中如实说明"用途沿用上文";
- 依据必须引用资料片段,格式 [片段:N](N 为编号);不得编造资料中没有的内容;
- 资料片段与该器具/用途无关时,选"无法确定"。

资料片段:
{chunks}

输出格式(纯文本):
结论: <三态之一>
依据: <引用片段,带 [片段:N]>
建议: <一句话,如送检周期/联系业务窗口>"""


def _linearize_chunks(chunks: list) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        text = (c.get("text") or "").strip()
        if text:
            parts.append(f"[片段:{i}] {text[:600]}")
    return "\n\n".join(parts)


def compose_conclusion(llm, instrument: str, purpose: str,
                       chunks: list, conv_id: Optional[str]) -> str:
    """三态结论由客服层生成(业务结论归编排,KG 只给依据)。"""
    prompt = _CONCLUDE_PROMPT.format(
        instrument=instrument or "该器具", purpose=purpose,
        chunks=_linearize_chunks(chunks))
    answer = chat_text(llm, messages=[{"role": "user", "content": prompt}],
                       temperature=0.0, conv_id=conv_id,
                       stage="assistant.conclude")
    return (answer or "").strip() or "结论: 无法确定"


def resolve_kb_for_purpose(purpose: str) -> str:
    """用途→资料库(演示业务分支;空/未知用途查规程库)。"""
    return (MANDATORY_CHECK_KB
            if purpose in STRONG_INSPECTION_PURPOSES else REGULATION_KB)


# ── 主工具 ────────────────────────────────────────────────────

class MetrologyAssistantTool(Tool):
    """计量智能客服(强检判定/进度查询)——Demo 薄编排。"""

    name = "metrology_assistant"
    description = ("计量智能客服:回答器具是否强制检定(按用途检索强检目录/"
                   "检定规程并给出带依据的三态结论)、查询委托单检测进度"
                   "(经业务平台接口,含归属校验)")
    when = ("用户咨询计量器具强检/检定周期/适用规程,或查询委托单/订单"
            "检测进度、报告状态")
    state_scope = "metrology_assistant"

    def __init__(self, app_state=None):
        self._app_state = app_state

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def preflight(self, state: dict, ctx: ToolContext) -> Optional[ToolResult]:
        if not self._app_state:
            return ToolResult(error_for_llm="计量客服插件未正确装配(app_state 未注入)")
        try:
            from domains.knowledge_graph import runtime
            runtime.get_kg_store(self._app_state).list_kbs()
        except Exception as e:
            return ToolResult(error_for_llm=f"知识库服务暂不可用: {e}")
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        user_input = (state.get("user_input") or "").strip()
        mem = getattr(ctx, "session_state", None)
        ctx.emit("pipeline_definition",
                 {"tool": self.name, "steps": _PIPELINE_STEPS})
        stage = _make_monotonic_stage(
            lambda key, message: ctx.emit("stage", key, message), _PIPELINE_STEPS)
        stage("assistant.extract", "识别问题类型与关键条件…")

        mem_slots = dict(mem.get("slots") or {}) if mem else {}
        mem_scenario = str(mem.get("scenario") or "") if mem else ""
        answers = state.get("clarify_answers") or {}

        if answers:
            # 追问恢复:从 pending 上下文确定性续跑(不再烧一次提取)
            pending = (mem.get("pending") or {}) if mem else {}
            scenario = pending.get("scenario") or mem_scenario or "other"
            merged = merge_slots(parse_clarify_answers(answers),
                                 pending.get("slots") or mem_slots)
            if mem:
                mem.pop("pending", None)
        else:
            extracted = extract_slots(ctx.llm_client, user_input, mem_slots,
                                      getattr(ctx, "conv_id", None))
            scenario = (extracted["scenario"] if extracted["scenario"] != "other"
                        else (mem_scenario or "other"))
            merged = merge_slots(extracted, mem_slots)

        if scenario == "mandatory_check":
            return self._mandatory_check(ctx, mem, merged, stage)
        if scenario == "progress":
            return self._progress(ctx, mem, merged, stage)
        text = ("当前演示版本覆盖两类问题:①器具是否强制检定"
                "(强检判定,按用途检索依据);②委托单检测进度查询。"
                "请换个问法,或联系业务窗口人工咨询。")
        return ToolResult(reply=text, summary=text)

    # ── 场景一:强检判定 ──

    def _mandatory_check(self, ctx: ToolContext, mem, merged: dict,
                         stage) -> ToolResult:
        instrument = merged.get("instrument") or ""
        purpose = merged.get("purpose") or ""
        if not purpose:
            # 只追问影响判断的必要条件(设计 13.1 故事 1:不编条件)
            if mem:
                mem.set("pending", {"scenario": "mandatory_check",
                                    "slots": merged})
            subject = f"「{instrument}」" if instrument else "该器具"
            return ToolResult(
                ask=AskSpec(questions=[AskQuestion(
                    question=f"{subject}主要用于什么用途?用途决定适用资料"
                            "(强检目录/检定规程)",
                    header=PURPOSE_HEADER, options=list(PURPOSE_OPTIONS))]),
                summary="需要确认器具用途")

        kb_name = resolve_kb_for_purpose(purpose)
        stage("assistant.retrieve",
              f"用途「{purpose}」→ 检索「{kb_name}」资料…")
        from domains.knowledge_graph import service as kg_service
        query = (f"{instrument} {purpose} 强制检定 检定周期" if instrument
                 else f"{purpose} 强制检定")
        try:
            evidence = kg_service.retrieve_evidence(
                self._app_state, kb_name, query,
                conv_id=getattr(ctx, "conv_id", None))
        except ValueError as e:
            return ToolResult(error_for_llm=str(e))
        except Exception as e:
            return ToolResult(error_for_llm=f"知识库检索失败: {e}")

        chunks = evidence.get("chunks") or []
        if not chunks:
            stage("assistant.answer", "未检索到依据,如实告知…")
            conclusion = (f"结论: 无法确定\n依据: 在「{kb_name}」库中未检索到"
                          f"「{instrument or '该器具'}({purpose})」的相关依据\n"
                          "建议: 请确认器具名称是否准确,或联系业务窗口人工确认")
            cited = []
        else:
            stage("assistant.answer", f"依据 {len(chunks)} 个片段生成结论…")
            conclusion = compose_conclusion(
                ctx.llm_client, instrument, purpose, chunks,
                getattr(ctx, "conv_id", None))
            from domains.knowledge_graph.retrieval import parse_cited_chunks
            cited = parse_cited_chunks(conclusion)

        artifact = {
            "type": "kg_search_result",
            "kb": evidence.get("kb") or {},
            "subgraph": evidence.get("subgraph") or {"nodes": [], "edges": []},
            "sources": {
                "entities": [n.get("name") for n in
                             (evidence.get("subgraph") or {}).get("nodes") or []][:20],
                "chunks": [{
                    "kbId": (evidence.get("kb") or {}).get("id"),
                    "docId": c.get("docId"),
                    "docName": c.get("docName"),
                    "score": c.get("score"),
                    "seq": c.get("seq"),
                    "childSeq": c.get("childSeq"),
                    "parentContext": c.get("parentContext"),
                    "text": (c.get("text") or "").strip()[:1200],
                } for c in chunks],
            },
            "cited_chunks": cited,
            "scenario": "强检判定",
            "instrument": instrument,
            "purpose": purpose,
        }
        if mem:
            mem.set("scenario", "mandatory_check")
            mem.set("slots", merged)
        return ToolResult(artifact=artifact, artifact_type="data",
                          summary=conclusion, formatted=self.format_result(artifact))

    # ── 场景二:进度查询 ──

    def _progress(self, ctx: ToolContext, mem, merged: dict,
                  stage) -> ToolResult:
        order_no = (merged.get("order_no") or "").strip().upper()
        if not order_no:
            if mem:
                mem.set("pending", {"scenario": "progress", "slots": merged})
            return ToolResult(
                ask=AskSpec(questions=[AskQuestion(
                    question="请提供委托单号(BJ 开头,可在委托单回执上查看)",
                    header=ORDER_HEADER, options=list(DEMO_ORDER_OPTIONS))]),
                summary="需要委托单号")

        ds_id = os.getenv(PROGRESS_DATASOURCE_ENV, "").strip()
        if not ds_id:
            return ToolResult(
                error_for_llm=("进度查询数据源未配置: 请设置环境变量 "
                               f"{PROGRESS_DATASOURCE_ENV} 为计量业务平台"
                               "数据源 ID(Demo 配置项)"))

        stage("assistant.retrieve", f"向业务平台查询委托单 {order_no}…")
        from domains.chatbi.controlled import execute_controlled
        result = execute_controlled(self._app_state, ds_id, PROGRESS_TOOL,
                                    {"order_no": order_no})

        if result.ok:
            record = (result.data or {}).get("record") or {}
            if not record.get("order_no"):
                # 空记录不装成功(信封 ok 但无有效业务数据=契约异常)
                return ToolResult(
                    error_for_llm=f"业务平台未返回委托单 {order_no} 的有效数据,"
                                  "请稍后重试。")
            stage("assistant.answer", "整理查询结果…")
            artifact = {
                "type": "metrology_progress",
                "orderNo": record.get("order_no") or order_no,
                "instrument": record.get("instrument") or "",
                "status": record.get("status") or "",
                "progress": record.get("progress") or "",
                "currentStep": record.get("current_step") or "",
                "eta": record.get("eta") or "",
                "reportNo": record.get("report_no") or "",
                "finishedAt": record.get("finished_at") or "",
                "validUntil": record.get("valid_until") or "",
                "queriedAt": record.get("queried_at") or "",
                "source": {"datasource": result.source.get("datasource"),
                           "tool": result.source.get("tool")},
            }
            if mem:
                mem.set("scenario", "progress")
                mem.set("slots", merged)
            return ToolResult(artifact=artifact, artifact_type="data",
                              summary=_format_progress_reply(record),
                              formatted=self.format_result(artifact))

        # 业务状态如实区分(13.1 故事 4/5:拒权/查无不是故障,故障不装查无)
        # summary 放完整用户话术:general 回复链路前端展示的是 summary
        # (conversation.ts general 分支),reply/summary 需同口径
        if result.status == "forbidden":
            text = (f"委托单 {order_no} 不属于当前主体,无权查询。"
                    "请核对单号,或联系业务窗口。")
            return ToolResult(reply=text, summary=text)
        if result.status == "not_found":
            text = f"未找到委托单 {order_no},请核对单号后重试。"
            return ToolResult(reply=text, summary=text)
        message = result.error.message if result.error else str(result.status)
        return ToolResult(
            error_for_llm=f"业务接口查询失败({result.status}): {message}。"
                          "已如实上报,请稍后重试。")

    def format_result(self, artifact: dict) -> dict:
        art = artifact or {}
        if art.get("type") == "metrology_progress":
            return {k: art[k] for k in
                    ("orderNo", "status", "progress", "eta", "reportNo",
                     "queriedAt") if art.get(k)}
        return {"kb": (art.get("kb") or {}).get("name") or "",
                "conclusion": (art.get("_conclusion") or "")[:40],
                "chunkCount": len(((art.get("sources") or {}).get("chunks"))
                                  or [])}


def _format_progress_reply(record: dict) -> str:
    order = record.get("order_no") or ""
    instrument = record.get("instrument") or ""
    head = f"委托单 {order}" + (f"({instrument})" if instrument else "")
    head += f"当前状态: {record.get('status') or '未知'}"
    details = []
    if record.get("progress"):
        details.append(f"进度 {record['progress']}")
    if record.get("current_step"):
        details.append(f"当前环节 {record['current_step']}")
    if record.get("eta"):
        details.append(f"预计完成 {record['eta']}")
    if record.get("report_no"):
        details.append(f"报告号 {record['report_no']}")
    if record.get("finished_at"):
        details.append(f"完成时间 {record['finished_at']}")
    if record.get("valid_until"):
        details.append(f"证书有效期至 {record['valid_until']}")
    tail = f"(查询时间 {record['queried_at']})" if record.get("queried_at") else ""
    return head + (";" + ",".join(details) if details else "") + tail
