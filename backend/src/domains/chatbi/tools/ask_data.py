"""ask_data —— ChatBI 主查询工具(CompositeTool)。

移植来源: chat-bi backend/app/ai/agent.py(7 步状态机,475 行) +
backend/app/api/chat.py 的调用侧状态恢复逻辑。
引擎侧映射: 7 步状态机 → CompositeTool steps;ask_user 三场景 → ToolResult.ask
(interrupt/resume);StateStore 多轮继承 → session_state;意图识别 → 引擎路由
(闲聊由引擎兜底,本工具只处理数据查询类)。

步骤与 ChatBI 的对应:
  resolve_datasource  ← chat.py 会话绑定数据源解析 + datasource registry
  retrieve_schema     ← agent.py SCHEMA_SEARCH(两阶段检索 + 图谱表扩展 + 追问表继承)
  think               ← agent.py THINKING(T027 预思考)
  generate_sql        ← agent.py GENERATE_SQL(T029 prompt 分层 + T030 校验)
  execute_sql         ← agent.py EXECUTE_SQL + SELF_HEAL(AEE-002, ≤max_rounds 回环)
  check_result        ← agent.py SELF_CHECK(T033)——异常场景 → ask(结果异常澄清)
  visualize           ← agent.py VISUALIZE(T034/T035) + metric 反哺闭环

追问继承 (对标 chat-bi state_store 的 prev_sql/prev_tables):
  每轮结束把 prev_sql/prev_tables/chart_config 写 session_state;
  检索阶段合并 prev_tables(追问不丢表),history 注入 prompt。
"""
from __future__ import annotations

import logging
import time

from sdk.tool import CompositeTool, ToolResult, ToolContext, AskOption, AskQuestion, AskSpec
from sdk.relational_store import PackRelationalDB

from domains.chatbi import datasources
from domains.chatbi.checker import check_result, ResultIssue
from domains.chatbi.chart_engine import generate_chart, inject_data
from domains.chatbi.healer import (extract_sql_text, get_circuit_breaker,
                                   heal_sql)
from domains.chatbi.security.sql_validator import validate_sql

logger = logging.getLogger(__name__)

# ── SQL 生成 prompt (T029 分层;移植自 sql_agent.py,文本原样) ──
_SQL_SYSTEM_PROMPT = """你是 BI 系统的 SQL 生成器。根据用户问题和数据库 schema 生成 PostgreSQL 查询。

严格规则 (违反则拒绝执行):
1. 只能生成 SELECT 语句, 禁止任何写操作 (INSERT/UPDATE/DELETE/DROP/ALTER)
2. 只能使用下方"允许的列"里列出的列名, 禁止臆造列名
3. 遵守 data_type 约束: 不能对 VARCHAR/TEXT 做数值聚合(SUM/AVG), 不能对 DATE 做数值运算
4. 禁止危险函数: LOAD_FILE/SLEEP/BENCHMARK/INTO OUTFILE
5. 只返回 SQL, 不要解释文字, 不要 markdown 包裹
6. 为每个 SELECT 输出列添加 AS 中文别名: schema 中列名后括号标注了中文名(中文: xxx), 用它做别名。聚合列也要有中文别名, 如 COUNT(*) AS 订单数。别名用双引号包裹, 如 user_id AS "用户ID"。

只返回一条 SQL 语句。"""

# ── 预思考 prompt (T027, 移植自 thinking.py) ─────────────────
_THINKING_PROMPT = """你是 BI 分析师。在生成 SQL 前, 先分析查询思路。

用户问题: {question}

可用表/列:
{schema}

检索到的候选:
{candidates}
{history_section}
请分析 (只返回 JSON):
{{
  "tables": ["选中的表名 (附一句理由)"],
  "aggregation": "聚合方式说明 (SUM/COUNT/AVG + GROUP BY 维度)",
  "caveats": ["注意事项 (Fan-Trap/多对多JOIN/维度混淆等陷阱)"],
  "prev_sql_review": "如有对话历史, 简述上一轮 SQL 是否有可优化处 (无历史则留空)"
}}"""


def format_thinking_hint(thinking: dict | None) -> str | None:
    """预思考结果 → SQL 生成提示文本(移植自 thinking.format_thinking_hint)。"""
    if not thinking or thinking.get("error"):
        return None
    parts = []
    if thinking.get("tables"):
        parts.append("选表: " + "; ".join(str(t) for t in thinking["tables"]))
    if thinking.get("aggregation"):
        parts.append("聚合: " + str(thinking["aggregation"]))
    if thinking.get("caveats"):
        parts.append("注意: " + "; ".join(str(c) for c in thinking["caveats"]))
    if thinking.get("prev_sql_review"):
        parts.append("上轮优化: " + str(thinking["prev_sql_review"]))
    return "\n".join(parts) if parts else None


def _format_history(messages: list, max_turns: int = 4) -> str:
    """对话消息 → 历史文本(追问注入;移植自 chat.py 的 history 组装)。"""
    recent = messages[-(max_turns * 2):] if messages else []
    lines = []
    for m in recent:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "")[:300]
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


class AskDataTool(CompositeTool):
    name = "ask_data"
    description = "用自然语言查询业务数据库:自动检索表结构、生成 SQL、执行并生成图表"
    when = "用户想查数据、看指标、做统计分析、对比趋势、生成图表报表时"
    state_scope = "chatbi"  # 与 switch_chart 共享会话记忆(prev_sql/绑定数据源)

    steps = ["resolve_datasource", "retrieve_schema", "think", "generate_sql",
             "execute_sql", "check_result", "visualize", "finalize"]
    pipeline_steps = [
        {"key": "resolve_datasource", "label": "连接数据源"},
        {"key": "retrieve_schema", "label": "检索表结构"},
        {"key": "think", "label": "分析思路"},
        {"key": "generate_sql", "label": "生成 SQL"},
        {"key": "execute_sql", "label": "执行查询"},
        {"key": "check_result", "label": "结果校验"},
        {"key": "visualize", "label": "生成图表"},
    ]

    def __init__(self, app_state=None, db: PackRelationalDB | None = None,
                 settings: dict | None = None):
        self._app_state = app_state
        self._db = db
        self._settings = settings or {}

    # ── 依赖取用(懒建;测试注入 db/settings) ──────────────────
    def _get_db(self) -> PackRelationalDB:
        if self._db is None:
            from domains.chatbi.runtime import get_pack_db
            self._db = get_pack_db()
        return self._db

    def _get_settings(self, ctx: ToolContext) -> dict:
        if self._settings:
            return self._settings
        from domains.chatbi.runtime import get_settings_reader
        return get_settings_reader(ctx).all()

    def _get_llm(self, ctx: ToolContext):
        # ctx.llm_client 是引擎裸实例(chat→str);pack 各栈按 (content, meta)
        # 契约编写——统一经 LLMCompat 适配(llm_compat.py 有"unpack 症状"说明)
        from domains.chatbi.llm_compat import LLMCompat
        inner = ctx.llm_client
        return inner if isinstance(inner, LLMCompat) else LLMCompat(inner)

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    # ── Step 1: 数据源解析(chat.py 会话绑定逻辑移植) ───────────
    def _step_resolve_datasource(self, state, ctx):
        ctx.emit("stage", "resolve_datasource", "正在连接数据源...")
        db = self._get_db()
        sess = ctx.session_state
        bound_id = (sess.get("datasource_id") if sess else None)
        try:
            info = datasources.resolve_datasource(db, bound_id)
        except ValueError as e:
            state["_need_clarify"] = True
            state["_error"] = str(e)
            return
        state["ds"] = info
        if sess and bound_id != info.id:
            sess.set("datasource_id", info.id)  # 会话绑定数据源(显式切换才更新)

    # ── Step 2: 检索(SCHEMA_SEARCH: 两阶段检索+图谱扩展+追问继承) ──
    def _step_retrieve_schema(self, state, ctx):
        if state.get("_need_clarify"):
            return
        ds: datasources.DataSourceInfo = state["ds"]
        ctx.emit("stage", "retrieve_schema", f"正在检索 {ds.name} 的表结构...")
        db = self._get_db()
        settings = self._get_settings(ctx)
        sess = ctx.session_state

        # 语义层当前版本(未扫描 → 提示先扫描, fail-closed 不瞎猜)
        from domains.chatbi import semantic
        content = semantic.load_current_content(db, ds.id)
        if content is None or not content.models:
            state["_need_clarify"] = True
            state["_error"] = (f"数据源「{ds.name}」尚未扫描语义层, 请先在管理端"
                               f"执行扫描后再提问")
            return

        # 场景② resume: 用户已选定表 → 直接作为种子表(跳过检索与澄清分支)
        clarified = state.get("clarified_table")
        if clarified:
            matched = next((m.name for m in content.models
                            if m.name == clarified
                            or m.display_name == clarified), clarified)
            state["content"] = content
            state["seed_tables"] = [matched]
            state["expanded_tables"] = []
            state["current_tables"] = [matched]
            state["join_path_section"] = ""
            state.pop("clarified_table", None)
            return

        # SEC-007: 用户问题进 LLM prompt 前清洗(NFKC + 零宽/方向控制字符)
        try:
            from sdk.sanitize import sanitize_text
            state["user_input"] = sanitize_text(state.get("user_input", ""))
        except ImportError:
            pass

        # B(差异修复): normalized_question 剥离可视化措辞(对标 INT-004)——
        # "用折线图展示本月销售额" → 问题="本月销售额", chart_hint="line"。
        # 规则剥离(零 LLM 成本), 剥离后的问题进检索/SQL 生成。
        raw_input = state.get("user_input", "")
        stripped, chart_hint = _strip_visualization(raw_input)
        if stripped != raw_input:
            if stripped:
                state["user_input"] = stripped
            if chart_hint:
                state["chart_hint"] = chart_hint  # 空剥离也保留 hint(如"画个饼图")

        # 两阶段检索(向量召回 → LLM 精筛);向量设施不可用 → 全表清单降级
        from domains.chatbi import retrieval as retrieval_mod
        from domains.chatbi import stores as cb_stores
        llm = self._get_llm(ctx)
        question = state.get("user_input", "")
        try:
            store = cb_stores.get_vector(self._app_state)
            embedder = cb_stores.get_embedder(llm)
            rc = retrieval_mod.retrieve_context(
                question, content, store, embedder,
                data_source_id=ds.id, llm=llm, db=db,
                top_k=int(settings.get("retrieve_top_k", 20)),
                conv_id=ctx.conv_id)
            # 源 agent.py:250-254: 只取 type=model 的记录作表名;
            # 纯指标召回(如问"GMV"只命中 metric 记录)时指标名不得混入表名
            all_hits = [m for m in getattr(rc["retrieval"], "models", [])
                        if isinstance(m, dict)]
            seed_tables = [m["name"] for m in all_hits
                           if m.get("name") and m.get("type") != "metric"]
            # 检索降级标记(O8: 精筛失败→提示结果可能不精确;源 agent.py:223-225)
            state["retrieval_degraded"] = bool(
                getattr(rc["retrieval"], "degraded", False))
            # 指标命中拆分(反查所属表用)
            state["hit_metric_names"] = [m["name"] for m in all_hits
                                         if m.get("type") == "metric"]
            # 一站式产物直接复用(与后续步骤的重复构建避免)
            state["schema_context"] = rc["schema_context"]
            state["allowed_columns"] = rc["allowed_columns"]
            state["metrics_hint"] = rc["metrics_hint"]
        except Exception as e:  # 向量设施故障 → 全表降级(检索降级不阻断, 对标 O8)
            logger.warning("向量检索不可用, 降级全表清单: %s", e)
            seed_tables = [m.name for m in content.models]

        # 追问表继承 (ARC-04/chat_utils.inherit_prev_tables 移植):
        # 只继承语义层中有定义的表(防止表已删除/重命名仍被引用), 去重
        prev_tables = (sess.get("prev_tables") if sess else None) or []
        merged = _inherit_prev_tables(prev_tables, list(seed_tables), content)
        # 指标命中反查所属表(用户问"GMV"可能只召回 metric 记录, 补入所属表)
        if not merged and state.get("hit_metric_names"):
            metric_names = state["hit_metric_names"]
            for model in content.models:
                if any(m.name in metric_names for m in model.metrics):
                    if model.name not in merged:
                        merged.append(model.name)

        # 图谱表扩展 + JOIN 路径预计算 (最短路径 + 社区补全, 对标 agent.py stage3:
        # 请求级 SchemaGraph 单例, expand + join_path 共享同一实例)
        expanded = list(merged)
        join_path_section = ""
        if settings.get("graph_enabled", True) and len(content.models) > 1:
            from domains.chatbi.schema_graph import (get_schema_graph,
                                                     expand_with_relationships,
                                                     build_join_path_section)
            try:
                sg = get_schema_graph(content)
                expanded = expand_with_relationships(
                    content, merged,
                    max_depth=int(settings.get("graph_expand_depth", 2)),
                    graph=sg)
                join_path_section = build_join_path_section(
                    content, expanded, graph=sg, seed_names=list(seed_tables),
                    join_path_in_prompt=bool(
                        settings.get("graph_join_path_in_prompt", True)))
            except Exception as e:
                logger.warning("图谱扩展失败(降级为检索表集): %s", e)

        state["content"] = content
        state["seed_tables"] = list(seed_tables)
        state["expanded_tables"] = sorted(set(expanded) - set(seed_tables))
        state["current_tables"] = expanded
        state["join_path_section"] = join_path_section

        # 多候选低分 → 表不确定澄清 (ask_user 场景②: CLARIFICATION)
        # 注意: run_pipeline 丢弃 step 返回值——结果必须走 state["_result"]
        if (not seed_tables and not prev_tables
                and len(content.models) > int(settings.get("clarify_table_threshold", 8))):
            options = [AskOption(label=m.display_name or m.name,
                                 description=m.description or m.name)
                       for m in content.models[:4]]
            state["_need_clarify"] = True
            state["clarify_kind"] = "table_confirm"
            state["clarify_tables"] = [m.name for m in content.models]
            state["_result"] = ToolResult(
                artifact_type="data",
                ask=AskSpec(questions=[AskQuestion(
                    question=f"「{ds.name}」里有 {len(content.models)} 张表, "
                             f"你想从哪张表的数据入手?",
                    header="选择数据表",
                    options=options,
                )]),
                summary="需要确认查询的数据表",
                extra={"clarify_kind": "table_confirm",
                       "tables": [m.name for m in content.models]},
            )
            return

    # ── Step 3: 预思考(T027;失败降级空思考不阻塞) ──────────────
    def _step_think(self, state, ctx):
        if state.get("_need_clarify") or state.get("_result"):
            return
        ctx.emit("stage", "think", "正在分析查询思路...")
        llm = self._get_llm(ctx)
        settings = self._get_settings(ctx)
        question = state.get("user_input", "")
        content = state["content"]

        schema_ctx = state.get("schema_context")
        if not schema_ctx:
            from domains.chatbi.retrieval import build_schema_context
            schema_ctx = build_schema_context(content, state["current_tables"])
        if not state.get("allowed_columns"):
            from domains.chatbi.retrieval import extract_allowed_columns
            state["allowed_columns"] = extract_allowed_columns(
                content, state["current_tables"])

        history = state.get("history_text") or ""
        history_section = f"\n对话历史:\n{history}\n" if history else ""
        prompt = _THINKING_PROMPT.format(
            question=question, schema=schema_ctx,
            candidates=", ".join(state["current_tables"]) or "(无)",
            history_section=history_section)
        thinking: dict = {}
        try:
            thinking = llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0, stage="chatbi.think", conv_id=ctx.conv_id) or {}
        except Exception as e:
            logger.warning("预思考失败(降级空思考): %s", e)
            thinking = {"error": str(e)}
        state["thinking"] = thinking
        state["thinking_hint"] = format_thinking_hint(thinking)

    # ── Step 4: 生成 SQL(T029 分层注入: schema/fewshot/skills/指标/记忆/历史) ──
    def _step_generate_sql(self, state, ctx):
        if state.get("_need_clarify") or state.get("_result"):
            return
        ctx.emit("stage", "generate_sql", "正在生成 SQL...")
        llm = self._get_llm(ctx)
        settings = self._get_settings(ctx)
        ds: datasources.DataSourceInfo = state["ds"]
        content = state["content"]
        question = state.get("user_input", "")

        schema_ctx = state.get("schema_context")
        if not schema_ctx:
            from domains.chatbi.retrieval import build_schema_context
            schema_ctx = build_schema_context(content, state["current_tables"])
        # 白名单列: 无条件取整个语义层的全部列(源 agent.py:296-302 权威注释——
        # 语义层本身是安全边界;命中表只影响 schema_context,限制白名单会
        # 误拒图谱扩展/继承表的正确 JOIN)。retrieve_context 返回的受限集合
        # 仅作降级兜底, 此处一律覆盖。
        from domains.chatbi.retrieval import extract_allowed_columns
        allowed = extract_allowed_columns(content)
        if not allowed:
            # 防御: 无语义层 → Layer3 白名单失效 → fail-closed 拒绝生成
            state["_error"] = "无语义层定义, 无法做白名单约束, 拒绝生成 SQL (请先扫描数据源)"
            return
        state["allowed_columns"] = allowed

        # 注入段(全部保留 ChatBI 的注入点, 缺失则跳过)
        sections = []
        skills_text = ""
        try:
            from domains.chatbi.skills_loader import load_skills_text
            skills_text = load_skills_text(ds.db_type)
            if skills_text:
                sections.append(f"【业务规则 (Skills)】\n{skills_text}")
        except Exception as e:
            logger.warning("Skills 加载失败(跳过): %s", e)

        try:
            from domains.chatbi import fewshot as fewshot_mod
            from domains.chatbi import stores as cb_stores
            examples = fewshot_mod.find_fewshot_examples(
                question, cb_stores.get_vector(self._app_state),
                cb_stores.get_embedder(llm), data_source_id=ds.id,
                top_k=int(settings.get("fewshot_top_k", 3)),
                db=self._get_db(), conv_id=ctx.conv_id)
            fewshot_text = fewshot_mod.format_fewshot_prompt(examples)
            if fewshot_text:
                sections.append(f"【参考示例】\n{fewshot_text}")
                state["fewshot_count"] = len(examples)
        except Exception as e:
            logger.warning("Few-shot 加载失败(跳过): %s", e)

        if state.get("thinking_hint"):
            sections.append(f"【预思考提示】\n{state['thinking_hint']}")
        if state.get("join_path_section"):
            sections.append(f"【JOIN 路径】\n{state['join_path_section']}")
        metrics_hint = state.get("metrics_hint")
        if metrics_hint is None:
            from domains.chatbi.retrieval import build_metrics_hint
            metrics_hint = build_metrics_hint(content, state["current_tables"])
        if metrics_hint:
            sections.append(f"【业务指标定义】\n{metrics_hint}")

        try:
            from domains.chatbi.memory import recall_text
            mem_text = recall_text(llm, self._get_db(), question,
                                   conv_id=ctx.conv_id)
            if mem_text:
                sections.append(f"【相关记忆】\n{mem_text}")
        except Exception as e:
            logger.warning("记忆召回失败(跳过): %s", e)

        if state.get("history_text"):
            sections.append(f"【对话历史】\n{state['history_text']}")

        # A(差异修复): 上轮筛选继承(对标 prev_filters) + 用户决策(DecisionPoint)
        _sess = ctx.session_state
        prev_filters = (_sess.get("prev_filters") if _sess else None) or {}
        if prev_filters:
            _filter_lines = "\n".join(f"- {k} = {v}" for k, v in prev_filters.items())
            sections.append(
                f"【上轮筛选条件】\n{_filter_lines}\n"
                f"(自动继承供参考; 若用户本轮明确取消或变更某项, 以本轮为准, 勿自动带上)")
        prev_decisions = (_sess.get("prev_decisions") if _sess else None) or []
        if prev_decisions:
            dec_text = "\n".join(f"- {d.get('description','')}" for d in prev_decisions if d.get("description"))
            if dec_text:
                sections.append(f"【用户已确认决策】\n{dec_text}")

        sections.append(f"【用户问题】{question}\n\n请生成 SQL:")

        user_content = "\n\n".join(sections)
        content_resp, _ = llm.chat(
            messages=[{"role": "system", "content": _SQL_SYSTEM_PROMPT},
                      {"role": "user", "content": user_content}],
            temperature=0.0, stage="chatbi.generate_sql", conv_id=ctx.conv_id)

        sql = extract_sql_text(content_resp)
        if not sql:
            # LLM 彻底失败(无 SQL) → final(failed);error_is_internal 语义由引擎处理
            state["_error"] = "LLM 未返回有效 SQL"
            return
        state["sql"] = sql
        validation = validate_sql(sql, allowed)
        # 校验失败不在此终止: 与执行失败同入自愈循环(agent.py 统一 while-true)
        state["validation"] = validation

    # ── Step 5: 执行 + 自愈回环(EXECUTE_SQL + SELF_HEAL, ≤max_rounds) ──
    def _step_execute_sql(self, state, ctx):
        if state.get("_need_clarify") or state.get("_result") or state.get("_error"):
            return
        settings = self._get_settings(ctx)
        ds: datasources.DataSourceInfo = state["ds"]
        llm = self._get_llm(ctx)
        max_rounds = int(settings.get("sql_self_heal_rounds", 2))
        max_rows = int(settings.get("sql_max_rows", 10000))
        timeout = int(settings.get("sql_execution_timeout", 30))
        schema_ctx = _schema_ctx(state)

        sql = state["sql"]
        started = time.monotonic()
        allowed = state.get("allowed_columns") or set()

        # 校验首版 SQL (校验不过不执行——T030 fail-closed)
        validation = state.get("validation")
        last_error = None
        result = None
        if validation is not None and not validation.ok:
            last_error = f"校验失败 ({validation.violated_layer}): {validation.reason}"
        else:
            result = datasources.execute_readonly(ds, sql, max_rows=max_rows,
                                                  timeout_seconds=timeout)
            if not result.ok:
                last_error = result.error

        # 自愈循环: 校验失败/执行失败 → heal → 重新校验 + 执行 (AEE-002)
        heal_rounds = 0
        while last_error is not None and heal_rounds < max_rounds:
            heal_rounds += 1
            ctx.emit("stage", "execute_sql", f"自愈中 (第 {heal_rounds} 轮)...")
            if not state.get("heal_before_sql"):
                state["heal_before_sql"] = sql
            heal = heal_sql(llm, sql, last_error, allowed, schema_ctx,
                            conv_id=ctx.conv_id,
                            circuit_breaker=get_circuit_breaker(
                                int(settings.get("sql_self_heal_circuit_breaker", 3))))
            if not heal.success:
                last_error = f"SQL 自愈失败 ({heal_rounds} 轮): {heal.error}"
                break
            sql = heal.sql
            # 重新校验(自愈结果也走三层校验, v1 教训 #32)
            revalidation = validate_sql(sql, allowed)
            if not revalidation.ok:
                last_error = f"校验失败 ({revalidation.violated_layer}): {revalidation.reason}"
                continue
            result = datasources.execute_readonly(ds, sql, max_rows=max_rows,
                                                  timeout_seconds=timeout)
            last_error = result.error if not result.ok else None

        duration = int((time.monotonic() - started) * 1000)
        state["_execute_duration_ms"] = duration
        # 链路打点(引擎时间线;SQL 全文随 checkpoint 落制品)
        ctx.trace("chatbi.execute",
                  title=f"执行查询({ds.name})",
                  status="ok" if last_error is None else "error",
                  duration_ms=duration,
                  detail={"heal_rounds": heal_rounds,
                          "rows": (result.rowcount if result else 0),
                          "truncated": bool(result and result.truncated)})
        if last_error is not None:
            state["_error"] = f"SQL 失败 (自愈 {heal_rounds} 轮未解决): {last_error}"
            state["sql"] = sql
            return
        state["sql"] = sql
        state["execute_result"] = result
        state["heal_rounds"] = heal_rounds
        ctx.emit("stage", "execute_sql_done", f"查询成功, 返回 {result.rowcount} 行")

    # ── Step 6: 结果自检(T033;ALL_NULL/CARTESIAN → ask 澄清场景③) ──
    def _step_check_result(self, state, ctx):
        if state.get("_need_clarify") or state.get("_result") or state.get("_error"):
            return
        settings = self._get_settings(ctx)
        max_rows = int(settings.get("sql_max_rows", 10000))
        max_rounds = int(settings.get("sql_self_heal_rounds", 2))
        result = state["execute_result"]

        # 场景③ resume"按原样展示": 复用中断前结果, 跳过 check 直接出图
        if state.get("show_as_is"):
            state.pop("show_as_is", None)
            return
        # 场景③ resume 自由输入: 作为修正方向 heal 一次(带用户描述)
        adjust_hint = state.pop("adjust_hint", None)
        if adjust_hint:
            ctx.emit("stage", "check_result", "按您的描述调整查询...")
            llm0 = self._get_llm(ctx)
            heal0 = heal_sql(llm0, state["sql"], adjust_hint,
                             state.get("allowed_columns") or set(),
                             _schema_ctx(state), conv_id=ctx.conv_id,
                             error_detail=f"用户想要: {adjust_hint}")
            if heal0.success and validate_sql(heal0.sql, state.get("allowed_columns") or set()).ok:
                ds0: datasources.DataSourceInfo = state["ds"]
                re0 = datasources.execute_readonly(
                    ds0, heal0.sql, max_rows=max_rows,
                    timeout_seconds=int(settings.get("sql_execution_timeout", 30)))
                if re0.ok:
                    state["sql"] = heal0.sql
                    state["execute_result"] = re0
                    result = re0

        check = check_result(result.rows, result.columns, state["sql"],
                             max_rows=max_rows)
        state["check"] = check
        if check.ok:
            return

        # 结果异常 → 先自动修正(suggestion 作纠正方向, 占自愈配额, AEE-003)
        heal_rounds = int(state.get("heal_rounds", 0))
        if check.suggestion and heal_rounds < max_rounds:
            heal_rounds += 1  # 先占配额(与 SQL 自愈循环一致)
            ctx.emit("stage", "check_result", f"结果异常({check.issue.value}), 自动修正中...")
            llm = self._get_llm(ctx)
            heal = heal_sql(llm, state["sql"], check.reason,
                            state.get("allowed_columns") or set(),
                            _schema_ctx(state), conv_id=ctx.conv_id,
                            error_detail=f"{check.reason} 建议: {check.suggestion}")
            if heal.success:
                revalidate = validate_sql(heal.sql, state.get("allowed_columns") or set())
                if revalidate.ok:
                    ds: datasources.DataSourceInfo = state["ds"]
                    re_result = datasources.execute_readonly(
                        ds, heal.sql, max_rows=max_rows,
                        timeout_seconds=int(settings.get("sql_execution_timeout", 30)))
                    if re_result.ok:
                        recheck = check_result(re_result.rows, re_result.columns,
                                               heal.sql, max_rows=max_rows)
                        if recheck.ok:
                            logger.info("结果自检自动修正成功")
                            state["sql"] = heal.sql
                            state["execute_result"] = re_result
                            state["check"] = recheck
                            state["heal_rounds"] = heal_rounds
                            return  # 修正成功, 跳过 ask_user(继续后续 step)

        # 仍异常 → 主动暂停问用户 (ask_user 场景③: 结果异常)
        # 现场结果写 state(resume 重跑时 tool_state 保留,"按原样展示"直接复用)
        state["clarify_kind"] = "result_abnormal"
        state["abnormal_result"] = {"sql": state["sql"], "columns": result.columns,
                                    "rows": result.rows}
        state["_need_clarify"] = True
        state["_result"] = ToolResult(
            artifact_type="data",
            ask=AskSpec(questions=[AskQuestion(
                question=f"查询结果异常: {check.reason}。要怎么处理?",
                header="结果异常",
                options=[
                    AskOption(label="按原样展示", description="仍查看当前结果"),
                    AskOption(label="帮我调整查询", description="描述你想看的角度, 重新生成 SQL"),
                ],
            )]),
            summary=f"结果异常: {check.reason}",
            extra={"clarify_kind": "result_abnormal",
                   "issue": check.issue.value if check.issue else "",
                   "sql": state["sql"],
                   "columns": result.columns, "rows": result.rows},
        )

    # ── Step 7: 图表(T034/T035) + 指标反哺闭环 ─────────────────
    def _step_visualize(self, state, ctx):
        if state.get("_need_clarify") or state.get("_result") or state.get("_error"):
            return
        ctx.emit("stage", "visualize", "正在生成图表...")
        result = state["execute_result"]
        llm = self._get_llm(ctx)
        sess = ctx.session_state
        chart_hint = state.get("chart_hint")
        chart = generate_chart(llm, state.get("user_input", ""),
                               result.columns, result.rows,
                               chart_type_hint=chart_hint, conv_id=ctx.conv_id)
        state["chart"] = chart

        # 指标反哺闭环 (recall.persist_metric_feedback 移植):
        # SQL 命中已知指标 → co_occurrence+1;新聚合模式(单表) → suggestion
        try:
            from domains.chatbi.feedback import persist_metric_feedback
            hits = persist_metric_feedback(
                self._get_db(), llm, state["sql"], state["content"],
                datasource_id=state["ds"].id, conv_id=ctx.conv_id)
            state["metric_hits"] = hits or []
        except Exception as e:
            logger.warning("指标反哺失败(不阻断): %s", e)
            state["metric_hits"] = []

    # ── Step 8: 组装 ToolResult + 会话状态写回 ─────────────────
    def _step_finalize(self, state, ctx):
        # 错误路径: 区分业务错误(可发原文)与内部错误(脱敏文案, 对标源
        # error_is_internal——agent.py:331/372/395/474 + chat.py:554-555)。
        # error_for_llm 保留原文供引擎重试判定;summary 走脱敏文案给用户。
        if state.get("_error"):
            raw = state["_error"]
            is_business = raw.startswith(("SQL 校验失败", "无语义层定义",
                                          "尚未扫描语义层", "尚无可用数据源"))
            user_text = raw if is_business else "查询执行失败, 请稍后重试或调整问法"
            state["_result"] = ToolResult(
                artifact_type="data",
                error_for_llm=raw,
                summary=f"查询失败: {user_text}",
                extra={"sql": state.get("sql", ""),
                       "error_internal": not is_business})
            return

        result: datasources.ExecuteResult = state["execute_result"]
        chart = state.get("chart")
        check = state.get("check")
        rows_sample = result.rows[:50]  # STATE_STORE_ROW_SAMPLE_LIMIT 对齐

        artifact = {
            "sql": state["sql"],
            "columns": result.columns,
            "rows_sample": [list(r) for r in rows_sample],
            "rowcount": result.rowcount,
            "truncated": result.truncated,
            "chart_option": chart.option if chart else None,
            "chart_config": chart.config if chart else None,
            "chart_degraded": chart.degraded if chart else False,
            "thinking": state.get("thinking") or {},
            "metric_hits": state.get("metric_hits") or [],
            "fewshot_count": state.get("fewshot_count", 0),
            "heal_rounds": state.get("heal_rounds", 0),
            "datasource_id": state["ds"].id,
            "datasource_name": state["ds"].name,
            "seed_tables": state.get("seed_tables") or [],
            "expanded_tables": state.get("expanded_tables") or [],
            "join_path_section": state.get("join_path_section") or "",
        }

        # 记忆抽取 (源 chat_stream 在成功查询后调 extract_memory_from_turn
        # + persist——移植为 extract_and_save_memory, 含来源对话关联)。
        # LLM 判定该轮是否值得记;失败/不该记静默跳过(fail-open 不阻塞)。
        # B1 修复: result_summary 先组装(此前未定义, NameError 被 except 吞掉
        # → 记忆抽取链路全死, 走查发现)
        result_summary = f"查询完成, 返回 {result.rowcount} 行; SQL: {state['sql'][:200]}"
        try:
            from domains.chatbi.memory import extract_and_save_memory
            extract_and_save_memory(
                llm, self._get_db(), state.get("user_input", ""),
                state["sql"], state.get("current_tables") or [],
                result_summary, conv_id=ctx.conv_id)
        except Exception as e:
            logger.warning("记忆抽取失败(不阻塞): %s", e)

        # linkage 记忆 (E1 Task 2.1;源 chat_stream 成功查询后调
        # persist_linkage_memory): 多表 JOIN 查询沉淀表对共现经验,
        # 是图谱置信度演化(mine_implicit_relationships)的数据源。
        # state 含 current_tables/join_path_section/question/thinking——
        # 单表查询内部自跳过;失败 fail-open。
        try:
            from domains.chatbi.memory import persist_linkage_memory
            persist_linkage_memory(self._get_db(), state, conv_id=ctx.conv_id)
        except Exception as e:
            logger.warning("linkage 记忆沉淀失败(不阻塞): %s", e)

        # M4: 成功查询自动保存(应用层去重, 供导出 CSV/看板引用)
        try:
            from domains.chatbi.m4 import save_query
            _m4_uid = (ctx.forward_headers or {}).get("X-User-Id", "anonymous")
            save_query(self._get_db(), _m4_uid, state["ds"].id,
                       state.get("user_input", ""), state["sql"],
                       conversation_id=ctx.conv_id,
                       chart_config=chart.config if chart else None,
                       result_summary={"row_count": result.rowcount})
        except Exception as e:
            logger.warning("查询自动保存失败(不阻塞): %s", e)

        # few-shot 成功回流 (RAG-004;源 chat_stream.py:917-929):
        # 成功查询的 Question-SQL Pair 入向量库, 后续相似问题召回作参考。
        # 失败降级只记日志(不阻塞结果返回)。
        try:
            from domains.chatbi import fewshot as fewshot_mod
            from domains.chatbi import stores as cb_stores
            from domains.chatbi.llm_compat import LLMCompat
            llm0 = self._get_llm(ctx)
            fewshot_mod.index_fewshot_example(
                state.get("user_input", ""), state["sql"],
                cb_stores.get_embedder(llm0), state["ds"].id,
                cb_stores.get_vector(self._app_state), self._get_db(),
                conv_id=ctx.conv_id)
        except Exception as e:
            logger.warning("few-shot 回流失败(不阻塞): %s", e)

        # 会话状态写回(多轮继承的载体;对标 ChatBI StateStore)
        sess = ctx.session_state
        if sess:
            sess.set("prev_sql", state["sql"])
            sess.set("prev_tables", state["current_tables"])
            if chart and chart.config:
                sess.set("prev_chart_config", chart.config)
            # A: 筛选条件提取写回(下轮 generate 注入继承)
            extracted = _extract_filters_from_sql(state["sql"])
            sess.set("prev_filters", extracted)  # 空也写=清除旧条件(取消生效)

        # summary 文案(含 0 行提示/命中指标)
        parts = [f"查询完成, 返回 {result.rowcount} 行"]
        if check and check.issue == ResultIssue.ZERO_ROWS:
            parts.append(f"⚠ {check.reason}: {check.suggestion}")
        if chart and chart.degraded:
            parts.append("(图表为规则推断降级)")
        if state.get("metric_hits"):
            names = ", ".join(h.get("display_name") or h.get("metric", "")
                              for h in state["metric_hits"][:5])
            parts.append(f"命中指标: {names}")

        state["_result"] = ToolResult(
            artifact=artifact,
            artifact_type="data",
            summary="; ".join(parts),
            extra={"sql": state["sql"]},
            formatted={
                "chart": chart.option if chart else None,   # 前端图表卡(ECharts)
                "metricHits": state.get("metric_hits") or [],
                "rowcount": result.rowcount,
                "truncated": result.truncated,
                "datasourceName": state["ds"].name,
                "tables": state["current_tables"],
                "seedTables": state.get("seed_tables") or [],
                "expandedTables": state.get("expanded_tables") or [],
                # 对话内明细(对标原系统 complete 事件的 step_durations/self_heal_rounds)
                "totalDurationMs": state.get("_total_duration_ms", 0),
                "healRounds": state.get("heal_rounds", 0),
                "executeDurationMs": state.get("_execute_duration_ms", 0),
                "chartDegraded": bool(chart and chart.degraded),
            })

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        # 对话内明细(走查差距修复): 每步耗时采集, finalize 汇总进 formatted
        # 让用户在对话流中就能看到各步耗时(对标原系统 step_durations)
        state["_step_timings"] = {}
        state["_pipeline_start"] = time.time()
        # ── 澄清回答消费(引擎 resume 注入 state["clarify_answers"]) ──
        # 结构: {question_header: label 或 自由输入文本}
        answers = state.get("clarify_answers") or {}
        if answers:
            kind = state.get("clarify_kind")
            if kind == "table_confirm":
                # 场景②: 用户选中的表(display_name → model.name), 写入
                # clarified_table 供 retrieve_schema 跳过低分门槛直取
                chosen = next(iter(answers.values()), "")
                state["clarified_table"] = chosen
                state.pop("clarify_kind", None)
            elif kind == "result_abnormal":
                # 场景③: "按原样展示" → 直接沿用中断前的执行结果继续出图;
                # 其他回答(自由输入) → 作为修正方向在 check 步重试
                choice = next(iter(answers.values()), "")
                if choice and ("原样" in choice or "展示" in choice):
                    state["show_as_is"] = True
                    state.pop("clarify_kind", None)
                elif choice:
                    state["adjust_hint"] = choice  # 用户描述的角度

        # 会话历史文本(追问注入;供 think/generate_sql 使用)
        if ctx.conv_id and ctx.conversation:
            try:
                msgs = ctx.conversation.get_messages(ctx.conv_id)
                state["history_text"] = _format_history(msgs)
            except Exception:
                state["history_text"] = ""
        self._timings_start = state.get("_pipeline_start", time.time())
        self.run_pipeline(state, ctx)
        state["_total_duration_ms"] = int((time.time() - state["_pipeline_start"]) * 1000)
        result = state.get("_result")
        if result is not None:
            return result
        # 兜底(不应到达): steps 全过但无结果
        return ToolResult(artifact_type="data", error_for_llm="管线未产出结果",
                          summary="查询未完成")

    # ── 制品钩子(引擎压缩/标题/前端展示) ─────────────────────
    def summarize_artifact(self, artifact: dict) -> str:
        """压缩状态补偿(对标 ChatBI 压缩摘要: 当前表集/SQL/维度)。"""
        tables = (artifact.get("seed_tables") or [])
        return (f"已查询 {artifact.get('datasource_name', '')}"
                f"(表: {', '.join(tables[:5])}), 返回 {artifact.get('rowcount')} 行")

    def title_for(self, artifact: dict) -> str:
        return (artifact.get("thinking") or {}).get("tables", [""])[0][:16] if artifact.get("thinking") else ""

    def format_result(self, artifact: dict) -> dict:
        return {
            "chart": artifact.get("chart_option"),
            "rowcount": artifact.get("rowcount"),
            "metricHits": artifact.get("metric_hits") or [],
            "datasourceName": artifact.get("datasource_name"),
        }


import re as _re_viz

_VIZ_PATTERNS = [
    (_re_viz.compile(r"用?(?:折线图|线图|曲线图|line chart)", _re_viz.IGNORECASE), "line"),
    (_re_viz.compile(r"用?(?:柱状图|柱图|条形图|bar chart)", _re_viz.IGNORECASE), "bar"),
    (_re_viz.compile(r"用?(?:饼图|饼状图|pie chart)", _re_viz.IGNORECASE), "pie"),
    (_re_viz.compile(r"用?(?:散点图|scatter)", _re_viz.IGNORECASE), "scatter"),
    (_re_viz.compile(r"画成|展示为|换成|改成|用.*(?:图|chart)展示", _re_viz.IGNORECASE), None),
    (_re_viz.compile(r"(?:画|帮我画)(?:一个|一张|个)?", _re_viz.IGNORECASE), None),
    (_re_viz.compile(r"用?(?:表格|明细表?|table)", _re_viz.IGNORECASE), "table"),
]


def _strip_visualization(question: str) -> tuple:
    """剥离可视化措辞(对标 INT-004), 返回 (纯净问题, chart_type_hint 或 None)。"""
    result = question
    hint = None
    for pattern, chart_type in _VIZ_PATTERNS:
        if pattern.search(result):
            if chart_type and not hint:
                hint = chart_type
            result = pattern.sub("", result)
    result = _re_viz.sub(r"^(展示|显示|看看|看下|看)\s*", "", result)
    result = _re_viz.sub(r"\s+", " ", result).strip()
    return result, hint


def _extract_filters_from_sql(sql: str) -> dict:
    """从 WHERE 提取筛选条件(继承用; 简化解析, 只取 col=value/比较)。"""
    if not sql:
        return {}
    filters = {}
    where_m = _re_viz.search(
        r"WHERE\s+(.*?)(?:\bGROUP\b|\bORDER\b|\bHAVING\b|\bLIMIT\b|$)", sql, _re_viz.IGNORECASE | _re_viz.DOTALL)
    if not where_m:
        return {}
    for m in _re_viz.finditer(
            r"((?:\w+\.)?\w+)\s*(?:>=|<=|!=|=|>|<)\s*(?:'([^']*)'|\"([^\"]*)\"|([^'\s,)]+))", where_m.group(1)):
        col = m.group(1)
        val = next((g for g in (m.group(2), m.group(3), m.group(4)) if g is not None), "")
        if col.lower() not in ("and", "or", "not", "is", "null", "like", "in", "between"):
            filters[col] = val
    return filters


def _inherit_prev_tables(prev_tables: list, current_names: list,
                         semantic_content) -> list:
    """追问表继承(chat_utils.inherit_prev_tables 移植):
    只继承语义层中有定义的表;跳过已存在;返回新 list。"""
    if not prev_tables:
        return current_names
    result = list(current_names)
    semantic_names = set()
    if semantic_content and hasattr(semantic_content, "models"):
        semantic_names = {m.name for m in semantic_content.models}
    for t in prev_tables:
        if not t or t in result:
            continue
        if t not in semantic_names:
            logger.debug("追问表继承: '%s' 不在语义层中, 忽略", t)
            continue
        result.append(t)
    return result


def _schema_ctx(state) -> str:
    """execute_sql 阶段复用 schema_context(自愈 prompt 需要)。"""
    ctx = state.get("schema_context")
    if ctx:
        return ctx
    from domains.chatbi.retrieval import build_schema_context
    return build_schema_context(state["content"], state["current_tables"])
