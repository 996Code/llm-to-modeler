"""query_stats —— 数据源查询质量统计工具 (D4 工具输出类型试点)。

返回按数据源聚合的查询质量指标(调用量/耗时/错误率/慢查询/自愈)。
统计 JSON 全量进 artifact(data 制品,前端渲染明细卡),但声明
suppress_output=True:LLM 可见历史只留短指针+link——统计明细对
后续对话的推理价值低,全量 JSON 进上下文纯属烧 token(Dify 对标:
工具输出按类型分流,大 JSON 走制品不走进上下文)。
"""
from __future__ import annotations

import logging

from sdk.tool import Tool, ToolResult, ToolContext

from domains.chatbi import datasources, query_stats

logger = logging.getLogger(__name__)

# 管理端指标页(前端图表化展示;link 给用户/LLM 的完整数据入口)
_METRICS_LINK = "/ai-modeler/admin.html#/chatbi"


class QueryStatsTool(Tool):
    name = "query_stats"
    description = "查看数据源最近的查询质量统计(调用量/平均耗时/错误率/慢查询次数)"
    when = "用户问'查询质量怎么样/最近查询统计/慢查询多不多'等数据源运行质量问题时"
    state_scope = "chatbi"

    def __init__(self, app_state=None, db=None):
        self._app_state = app_state
        self._db = db

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        db = self._db or _lazy_db()
        # 目标数据源:会话绑定 > 第一个 active(与查询链路同口径)
        sess = ctx.session_state
        ds_id = sess.get("datasource_id") if sess else None
        try:
            ds = datasources.resolve_datasource(db, ds_id, decrypt=False)
        except ValueError as e:
            return ToolResult(artifact_type="data",
                              error_for_llm=str(e),
                              summary=f"查询统计不可用: {e}")

        rows = query_stats.datasource_metrics(db, data_source_id=ds.id)
        if not rows:
            return ToolResult(
                artifact={"items": [], "dataSourceName": ds.name},
                artifact_type="data",
                summary=f"数据源「{ds.name}」暂无查询记录(还没人查过数据)",
            )

        m = rows[0]
        # summary 是 LLM 可见文本:只给一句话结论(数字结论够 LLM 回答
        # "质量怎么样");逐源明细 JSON 全量在 artifact 里,suppress_output
        # 保证它不进对话上下文
        summary = (f"数据源「{ds.name}」共 {m['query_count']} 次查询, "
                   f"平均 {m['avg_ms']}ms, 错误率 {m['error_rate']}%, "
                   f"慢查询 {m['slow_count']} 次")
        ctx.trace("chatbi.query_stats", title="查询统计",
                  status="ok", detail={"query_count": m["query_count"],
                                       "error_rate": m["error_rate"]})
        return ToolResult(
            artifact={"items": rows, "dataSourceName": ds.name},
            artifact_type="data",
            summary=summary,
            suppress_output=True,   # D4:明细 JSON 不进 LLM 上下文
            link=_METRICS_LINK,     # 完整图表化入口
        )


def _lazy_db():
    from domains.chatbi.stores import get_db
    return get_db()
