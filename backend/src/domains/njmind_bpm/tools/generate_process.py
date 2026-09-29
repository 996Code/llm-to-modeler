"""GenerateProcessTool - 从零生成流程草稿(6 步管线)。

fetch_guide → fetch_form_catalog → parse_intents → fetch_templates
→ generate → render(含机械修复循环)

承接义务:F1(processKey/processName 宿主注入,LLM 自造值剥离)、
机械修复 ≤4 轮、重生成 ≤MAX_RETRIES、仅校验通过才产 artifact。
"""
import json
import logging
from typing import Any, Dict, Optional

from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_bpm import service_locator
from domains.njmind_bpm import keys as K
from domains.njmind_bpm.tools._pipeline_common import (
    inject_host_identity, mechanical_repair, build_form_catalog,
    host_context_from, MAX_MECHANICAL_ROUNDS,
    collect_unknown_branch_fields, apply_branch_field_answers,
    merge_rule_details,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class GenerateProcessTool(CompositeTool):
    """根据自然语言需求生成完整流程草稿(bpmnXml+规则经上游渲染)。"""

    name = "generate_process"
    description = "根据自然语言需求生成完整审批流程(拓扑+规则);画布已有流程时按新描述整体重造并回显覆盖"
    when = "任何流程生成/修改/重做需求:'做个请假审批流'、'改成...'、'重新生成...'"

    steps = ["fetch_guide", "fetch_existing", "fetch_form_catalog", "parse_intents",
             "fetch_templates", "generate", "render"]
    pipeline_steps = [
        {"key": "fetch_guide", "label": "获取流程指南"},
        {"key": "fetch_existing", "label": "读取当前流程"},
        {"key": "fetch_form_catalog", "label": "加载表单字段"},
        {"key": "parse_intents", "label": "解析流程意图"},
        {"key": "fetch_templates", "label": "匹配流程模板"},
        {"key": "generate", "label": "生成流程草稿"},
        {"key": "render", "label": "渲染与校验"},
    ]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的自然语言流程需求"}
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])
        self.run_pipeline(state, ctx)

        artifact = state.get("artifact")
        errors = state.get("validation_errors", [])
        warnings = state.get("validation_warnings", [])
        if artifact:
            nodes = len((artifact.get("draft_topology") or {}).get("nodes", []))
            summary = (f"已生成流程「{artifact.get('processName', '')}」"
                       f"({nodes} 个节点),校验通过"
                       + (f",附 {len(warnings)} 个提示" if warnings else ""))
        else:
            errs = "; ".join(
                e.get("message", str(e))[:60] for e in errors[:3]) if errors else "未知原因"
            summary = f"流程生成未完成:{errs}"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            valid=(not errors) if artifact else None,
            validation_errors=errors or None,
            formatted=self.format_result(artifact) if artifact else {},
        )

    def summarize_artifact(self, artifact: dict) -> str:
        topo = artifact.get("draft_topology") or {}
        names = [n.get("name", "") for n in topo.get("nodes", [])][:10]
        return (f"当前流程: {artifact.get('processName', '')}, 节点: "
                f"{', '.join(n for n in names if n)}")

    def title_for(self, artifact: dict) -> str:
        return artifact.get("processName", "新流程")

    def format_result(self, artifact: dict) -> dict:
        topo = artifact.get("draft_topology") or {}
        return {
            "nodeCount": len(topo.get("nodes", [])),
            "processName": artifact.get("processName", ""),
            "title": artifact.get("processName", "新流程"),
        }

    # ── Steps ──────────────────────────────────────────────────

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "fetch_guide", "正在获取流程配置指南...")
        api = service_locator.get_api()
        guide = api.get_guide()
        if not guide or not guide.get("nodeTypes"):
            raise ClarificationRaised([
                "获取流程配置指南失败(上游服务不可用),请稍后重试"
            ])
        state["guide"] = guide

    def _step_fetch_existing(self, state: dict, ctx: ToolContext) -> None:
        """读取画布当前流程作为参考上下文(整体重造语义:不是最小变更基线)。

        画布有 bpmnXml 时 parse 出旧拓扑;旧规则中用户已配置的明细(审批人
        勾选等)在 render 前按 I2 合并继承,不因重造丢失。
        """
        ctx.emit("stage", "fetch_existing", "正在读取当前流程...")
        host = host_context_from(state)
        state["existing_topology"] = None
        state["existing_rules"] = host["rules"] or {}
        if host["bpmnXml"]:
            api = service_locator.get_api()
            parsed = api.parse(host["bpmnXml"])
            draft = parsed.get("draft")
            if draft is not None:
                # 契约外元素警告如实提示,但整体替换语义不阻断(重造会丢弃它们)
                state["existing_topology"] = draft.get(K.TOPOLOGY)
                warns = parsed.get("warnings") or []
                if warns:
                    items = "; ".join(w.get("message", str(w))[:40] for w in warns[:3])
                    ctx.emit("stage", "fetch_existing",
                             f"当前流程含暂不支持元素({items}),重造后将被替换")

    def _step_fetch_form_catalog(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "fetch_form_catalog", "正在加载绑定表单字段目录...")
        host = host_context_from(state)
        form_key = host["formKey"]
        catalog: list = []
        if form_key:
            api = service_locator.get_api()
            form = api.get_form(form_key)
            if form:
                catalog = build_form_catalog(form)
            else:
                # 表单不存在是配置事实,如实告知,分支条件将不可用
                ctx.emit("stage", "fetch_form_catalog",
                         f"绑定表单 {form_key} 不存在,分支条件功能受限")
        state["form_catalog"] = catalog

    def _step_parse_intents(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "parse_intents", "AI 正在解析流程需求...")
        user_input = state.get("user_input", "")
        clarify = state.get("clarify_answers") or {}
        if clarify:
            extra = clarify.get("text") or "；".join(
                f"{k}: {v}" for k, v in clarify.items() if k != "text")
            if extra:
                user_input = f"{user_input}\n（用户补充回答：{extra}）"

        system_prompt = self._render_prompt(
            ctx, "parse", guide=state.get("guide") or {})
        user_msg = self._build_user_message(user_input, state.get("compressed_history", ""))
        parsed = ctx.llm_client.chat_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_msg}],
            conv_id=ctx.conv_id, stage="generate_process.parse")

        if parsed.get("needsClarification"):
            raise ClarificationRaised(parsed.get("clarificationQuestions") or
                                      ["请补充流程信息"])
        state["intents"] = parsed
        n_nodes = len(parsed.get("nodeIntents", []))
        ctx.emit("stage", "parse_intents_done",
                 f"已解析流程意图:{parsed.get('processSummary', '')}({n_nodes} 个节点)")

    def _step_fetch_templates(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "fetch_templates", "正在匹配流程模板...")
        api = service_locator.get_api()
        state["process_template"] = api.get_template("bpm_process_generic")
        intents = state.get("intents") or {}
        wanted: list = []
        for bi in intents.get("branchIntents", []) or []:
            wanted.append("bpm_process_branch")
            break
        for ni in intents.get("nodeIntents", []) or []:
            # N-4:词表与 Java 契约对齐为 CC_TASK(prompt 已改 emit CC_TASK)
            if ni.get("nodeType") == K.NODE_TYPE_CC:
                wanted.append("bpm_cc")
        wanted.extend(["bpm_approve_default", "bpm_branch_field_value"])
        wanted = sorted(set(w for w in wanted if w))
        templates = {}
        for name in wanted:
            t = api.get_template(name)
            if t:
                templates[name] = t
        state["rule_templates"] = templates
        ctx.emit("stage", "fetch_templates_done",
                 f"已加载 {len(templates)} 个流程模板")

    def _step_generate(self, state: dict, ctx: ToolContext) -> None:
        is_retry = bool(state.get("validation_errors"))
        if is_retry:
            ctx.emit("stage", "generate_retry",
                     f"校验失败,正在修复重新生成(第 {state.get('retry_count', 0)} 次重试)...")
        else:
            ctx.emit("stage", "generate", "AI 正在组装流程草稿...")

        system_prompt = self._render_prompt(
            ctx, "generate",
            guide=state.get("guide") or {},
            process_template=state.get("process_template") or {},
            rule_templates=state.get("rule_templates") or {},
            form_catalog=state.get("form_catalog") or [])
        user_parts = []
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])
        if state.get("existing_topology"):
            import json as _json
            # 参考拓扑脱 id:BPMN 风格 key(Activity_xxx/Flow_xxx)会被 LLM 照抄,
            # 产物 SUSPECTED_BPMN_ID_KEY 告警且规则 key 全废(实测)。改写成
            # 语义占位(node_1/edge_1),名称保留——LLM 只参考结构语义,不抄 id。
            topo = _json.loads(_json.dumps(state["existing_topology"]))
            node_name = {}
            for i, n in enumerate(topo.get("nodes") or [], 1):
                old = n.get("key")
                new = f"node_{i}"
                node_name[old] = new
                n["key"] = new
            for i, e in enumerate(topo.get("edges") or [], 1):
                e["key"] = f"edge_{i}"
                e["from"] = node_name.get(e.get("from"), e.get("from"))
                e["to"] = node_name.get(e.get("to"), e.get("to"))
            topo_json = _json.dumps(topo, ensure_ascii=False)
            user_parts.extend([
                "## 画布当前流程(仅供理解业务语义;输出必须用全新语义 key 如 manager/gateway_days,"
                "禁止使用 node_N/edge_N/Activity_xxx 等参考占位符作 key)",
                "```json\n" + topo_json + "\n```",
                "",
            ])
        if is_retry and state.get("raw_draft"):
            error_lines = [e.get("message", str(e)) if isinstance(e, dict) else str(e)
                           for e in state.get("validation_errors", [])[:8]]
            user_parts.extend([
                "## 校验失败,请修复", "\n".join(error_lines), "",
                "## 当前草稿",
                f"```json\n{json.dumps(state['raw_draft'], ensure_ascii=False)}\n```",
                "修复以上问题后输出完整草稿 JSON(紧凑,无围栏)。",
            ])
        else:
            user_parts.extend([
                "## 流程意图",
                f"```json\n{json.dumps(state.get('intents') or {}, ensure_ascii=False, indent=2)}\n```",
                "",
                "请根据意图和模板,组装完整的流程草稿 JSON(topology+规则)。",
            ])
        draft = ctx.llm_client.chat_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": "\n".join(user_parts)}],
            conv_id=ctx.conv_id, stage="generate_process.generate")
        state["raw_draft"] = draft
        state["validation_errors"] = []

    def _step_render(self, state: dict, ctx: ToolContext) -> None:
        ctx.emit("stage", "render", "正在渲染 BPMN 并校验...")
        api = service_locator.get_api()
        host = host_context_from(state)
        # I2 规则明细合并:整体重造时,新 draft 未覆盖的用户已配明细
        # (审批人勾选/按钮设置等)从宿主规则继承,不因重造丢失
        raw = state.get("raw_draft")
        if raw and host["rules"]:
            merged = merge_rule_details({
                K.APPROVE_RULES: (raw or {}).get(K.APPROVE_RULES) or [],
                K.CC_RULES: (raw or {}).get(K.CC_RULES) or [],
                K.BRANCH_RULES: (raw or {}).get(K.BRANCH_RULES) or [],
                K.FORM_KEY: (raw or {}).get(K.FORM_KEY),
            }, host["rules"],
                host_topology=state.get("existing_topology"),
                new_topology=(raw or {}).get(K.TOPOLOGY))
            raw = dict(raw)
            raw[K.APPROVE_RULES] = merged[K.APPROVE_RULES]
            raw[K.CC_RULES] = merged[K.CC_RULES]
            raw[K.BRANCH_RULES] = merged[K.BRANCH_RULES]
            if merged.get(K.FORM_KEY):
                raw[K.FORM_KEY] = merged[K.FORM_KEY]
            state["raw_draft"] = raw
        draft, err = inject_host_identity(
            state.get("raw_draft"), host["processKey"], host["processName"],
            host_form_key=host["formKey"])
        if err:
            state["validation_errors"] = [{"message": err}]
            ctx.emit("stage", "render_fail", err)
            return

        # 分支字段确认门(结构化,复用前端点选):
        # ①resume 时先确定性回填上一轮点选(不过 LLM,秒级);
        # ②仍有缺口 → 每个缺失字段一问,带候选选项(前端点击即答)。
        # 前端点选回答形如 {缺失字段key: 所选中文名}(answerClarification 发
        # {[q.header]: opt.label});自由输入走 {"text": ...} 由 parse 重生成。
        # 这里确定性回填点选映射,秒级,不过 LLM。
        clarify = state.get("clarify_answers") or {}
        if isinstance(clarify, dict):
            field_answers = {k: v for k, v in clarify.items()
                             if k not in ("text",) and not k.startswith("__")}
            if field_answers:
                apply_branch_field_answers(draft, field_answers,
                                           state.get("form_catalog") or [])
        unknown = collect_unknown_branch_fields(
            draft, state.get("form_catalog") or [])
        if unknown:
            catalog = state.get("form_catalog") or []
            raise ClarificationRaised([
                json.dumps({
                    "question": f"分支条件「{u['missing']}」用哪个表单字段？",
                    "header": u["missing"],
                    "options": [
                        {"label": c["label"] or c["value"],
                         "description": f"字段标识: {c['value']}"}
                        for c in u["candidates"]
                    ],
                }, ensure_ascii=False)
                for u in unknown
            ])

        result = api.render(draft)
        validation = result.get(K.VALIDATION) or {}
        errors = validation.get("errors") or []
        # fail-closed:校验结果缺失(网关截断/契约演进)视同失败,不让半成品过门
        passed = validation.get("pass") is True and not errors
        for _ in range(MAX_MECHANICAL_ROUNDS):
            if passed:
                break
            draft, errors = mechanical_repair(draft, errors)
            result = api.render(draft)
            validation = result.get(K.VALIDATION) or {}
            errors = validation.get("errors") or []
            passed = validation.get("pass") is True and not errors

        if passed:
            state["validation_errors"] = []
            state["validation_warnings"] = validation.get("warnings") or []
            state["artifact"] = {
                "formKey": draft.get(K.FORM_KEY),
                "bpmnXml": result.get(K.BPMN_XML),
                "rules": result.get(K.RULES),
                "processName": host["processName"],
                "processKey": host["processKey"],
                "draft_topology": draft.get(K.TOPOLOGY),
                "warnings": validation.get("warnings") or [],
            }
            w = len(validation.get("warnings") or [])
            ctx.emit("stage", "render_pass", f"渲染校验通过 ✓" + (f"({w} 个提示)" if w else ""))
            return

        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = errors
        msgs = [e.get("message", str(e))[:60] if isinstance(e, dict) else str(e)[:60]
                for e in errors[:3]]
        if state["retry_count"] < MAX_RETRIES:
            ctx.emit("stage", "render_retry",
                     f"校验失败:{'; '.join(msgs)},正在重试(第 {state['retry_count']} 次)...")
            self._step_generate(state, ctx)
            return self._step_render(state, ctx)
        ctx.emit("stage", "render_fail",
                 f"校验失败(已达最大重试):{'; '.join(msgs)}")

    # ── 辅助 ──────────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            return ctx.prompt_loader.render("njmind_bpm", name, **vars)
        logger.warning(f"No prompt_loader for {name}")
        return ""

    def _build_user_message(self, user_input: str, compressed_history: str) -> str:
        parts = []
        if compressed_history:
            parts.extend(["## 对话历史", compressed_history, ""])
        parts.extend(["## 当前流程需求", user_input, "", "请分析并输出 JSON。"])
        return "\n".join(parts)
