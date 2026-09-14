"""switch_chart —— 图表切换工具 (ChatBI CHART_MODIFY 分支移植)。

移植来源: chat-bi backend/app/ai/agent.py 的 CHART_MODIFY 分支
(复用上轮 SQL 重新执行, 不重新生成 SQL) + chart_agent 的 config 复用机制。
多轮记忆: 从 session_state 读 prev_sql/prev_chart_config(state_scope=chatbi 共享)。
"""
from __future__ import annotations

import logging

from sdk.tool import Tool, ToolResult, ToolContext

from domains.chatbi import datasources
from domains.chatbi.chart_engine import inject_data, infer_chart_by_rule

logger = logging.getLogger(__name__)

# 自然语言 → chart_type 映射(CHART_MODIFY 的类型词表, 移植自 chat-bi intent 词表)
_TYPE_WORDS = {
    "饼": "pie", "占比": "pie", "分布": "pie",
    "柱": "bar", "条形": "bar", "柱状": "bar",
    "折线": "line", "趋势": "line", "线图": "line", "曲线": "line",
    "表格": "table", "明细": "table", "表格式": "table",
    "散点": "scatter", "相关": "scatter",
    "指标卡": "kpi", "大数字": "kpi", "汇总": "kpi",
}


def _guess_chart_type(text: str) -> str | None:
    for word, ctype in _TYPE_WORDS.items():
        if word in text:
            return ctype
    return None


class SwitchChartTool(Tool):
    name = "switch_chart"
    description = "切换上一个查询结果的图表类型(柱状/折线/饼图/表格/指标卡)"
    when = "用户对上一个查询结果说'换成饼图/折线图/柱状图/表格'等图表切换诉求时"
    state_scope = "chatbi"

    def __init__(self, app_state=None, db=None, settings: dict | None = None):
        self._app_state = app_state
        self._db = db
        self._settings = settings or {}

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        sess = ctx.session_state
        prev_sql = sess.get("prev_sql") if sess else None
        if not prev_sql:
            return ToolResult(artifact_type="data",
                              error_for_llm="没有可切换的历史查询",
                              summary="本会话还没有查询结果, 请先提问再切换图表")

        # SEC S7 (对标 agent.py CHART_MODIFY): prev_sql 来自持久化状态,
        # 必须重新校验——即使上轮已校验, 语义层/白名单可能已变更, 且
        # 持久化数据可能被篡改。空白名单 = 只做 AST 层校验(与源一致)。
        from domains.chatbi.security.sql_validator import validate_sql
        revalidation = validate_sql(prev_sql, allowed_columns=set())
        if not revalidation.ok:
            logger.warning("switch_chart: prev_sql 校验失败: %s", revalidation.reason)
            return ToolResult(artifact_type="data",
                              error_for_llm="上轮 SQL 已不再合规, 请重新提问",
                              summary="上轮 SQL 校验未通过, 请重新提问, 我会生成新的查询。")

        ds_id = sess.get("datasource_id") if sess else None
        db = self._db or _lazy_db()
        ds = datasources.resolve_datasource(db, ds_id)
        settings = self._settings or _lazy_settings(ctx)

        ctx.emit("stage", "switch_chart", "正在重跑查询并切换图表...")
        result = datasources.execute_readonly(
            ds, prev_sql,
            max_rows=int(settings.get("sql_max_rows", 10000)),
            timeout_seconds=int(settings.get("sql_execution_timeout", 30)))
        if not result.ok:
            return ToolResult(artifact_type="data",
                              error_for_llm=f"重跑查询失败: {result.error}",
                              summary="切换图表失败: 查询执行失败")

        prev_config = (sess.get("prev_chart_config") if sess else None) or {}
        wanted = _guess_chart_type(state.get("user_input", "")) or "bar"
        config = dict(prev_config)
        config["chart_type"] = wanted
        if wanted in ("kpi", "table") and not config.get("measure_cols"):
            pass  # inject_data 内部有回退逻辑
        option = inject_data(config, result.columns, result.rows)
        degraded = False
        if option is None:
            option = infer_chart_by_rule(result.columns, result.rows)
            degraded = True
        if sess:
            sess.set("prev_chart_config", config)

        ctx.trace("chatbi.switch_chart", title="切换图表",
                  status="ok", detail={"chart_type": wanted})
        return ToolResult(
            artifact={"sql": prev_sql, "columns": result.columns,
                      "rows_sample": [list(r) for r in result.rows[:50]],
                      "rowcount": result.rowcount,
                      "chart_option": option, "chart_config": config,
                      "chart_degraded": degraded,
                      "datasource_id": ds.id, "datasource_name": ds.name},
            artifact_type="data",
            summary=f"图表已切换为「{wanted}」",
            extra={"sql": prev_sql},
            formatted={"chart": option, "rowcount": result.rowcount,
                       "datasourceName": ds.name})


def _lazy_db():
    from domains.chatbi.runtime import get_pack_db
    return get_pack_db()


def _lazy_settings(ctx: ToolContext) -> dict:
    from domains.chatbi.runtime import get_settings_reader
    return get_settings_reader(ctx).all()
