"""WebSearchTool - 前台对话工具:联网搜索(经 SearXNG→mihomo→google)。

【路由】保守触发:用户明确要"网上的/联网/最新政策"类外部信息才选中
("上网查一下最新的XX规定""网上怎么说的");普通知识问题仍走 kb_search
——受控架构裁决,联网结果只做参考并必带来源 URL。

【产物】artifact_type="data":{query, results:[{title,url,content}], cached}
——前端通用数据卡展示,来源可点。

【记录】对话链路的调用走既有会话记录+链路追踪;管理端调研 tab 的
持久化在 websearch.log_search(kg_web_search_logs 表),不经本工具。
"""
import logging
from typing import Any, Optional

from sdk.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# ── 进度条步骤定义(前端 pipeline 前缀匹配推进) ──────────────
_PIPELINE_STEPS = [
    {"key": "web_search.search", "label": "联网搜索"},
    {"key": "web_search.assemble", "label": "整理结果与来源"},
]


class WebSearchTool(Tool):
    """联网搜索(可选工具:searxng_url 未配置时 preflight 拦截)。"""

    name = "web_search"
    description = ("联网搜索外部信息(经 SearXNG/google),返回带来源链接的"
                   "检索结果列表")
    when = ("用户明确要联网/网上的外部信息,如'上网查一下''网上怎么说的'"
            "'最新的XX政策/规定是什么'(知识库内的问题不走本工具)")
    state_scope = "knowledge_graph"

    def __init__(self, app_state: Any = None):
        self._app_state = app_state

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "top_k": {"type": "integer", "description": "返回条数(默认5)"},
            },
            "required": ["query"],
        }

    def validate_input(self, state: dict) -> Optional[str]:
        if not (state.get("query") or "").strip():
            return "缺少搜索词(query)"
        return None

    def preflight(self, state: dict, ctx: ToolContext) -> Optional[ToolResult]:
        """执行前提:searxng_url 已配置(未配置=插件未启用,如实告知)。"""
        from domains.knowledge_graph import websearch
        if not websearch.settings_url(self._app_state):
            return ToolResult(error_for_llm=(
                "联网搜索未启用:管理员未在知识库插件设置里配置 SearXNG 地址,"
                "请改用知识库检索或联系管理员"))
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        import time as _time
        from domains.knowledge_graph import websearch

        query = (state.get("query") or state.get("user_input") or "").strip()
        top_k = state.get("top_k") or 5
        try:
            top_k = max(1, min(int(top_k), 10))
        except (TypeError, ValueError):
            top_k = 5

        ctx.emit("pipeline_definition",
                 {"tool": self.name, "steps": _PIPELINE_STEPS})
        # message 位置传参(nodes.py 的 emit 按位置参数个数分发)
        ctx.emit("stage", "web_search.search", "正在联网搜索…")
        t0 = _time.monotonic()
        out = websearch.search(self._app_state, query, top_k=top_k)
        ctx.emit("stage", "web_search.assemble", "整理结果与来源…")

        results = out["results"]
        cached = bool(out.get("cached"))
        # 搜索条件+结果摘要入链(用户裁决:开关/条件/结果都要在链路可查)。
        # detail 只放 title+url 摘要(前5条)——完整 content 在制品里,
        # trace 是高频审计流不塞大体积
        ctx.trace("web_search.search", "联网搜索", "ok",
                  duration_ms=int((_time.monotonic() - t0) * 1000),
                  detail={
                      "query": query, "top_k": top_k, "cached": cached,
                      "resultCount": len(results),
                      "results": [{"title": r["title"][:60], "url": r["url"]}
                                  for r in results[:5]],
                  })

        artifact = {
            "type": "web_search_result",
            "query": query,
            "results": results,
            "cached": cached,
        }
        # summary 进历史与 LLM 上下文:结果条数 + 每条标题与来源 URL
        # (受控架构:联网内容必须可追溯,不裸给结论)
        lines = [f"联网搜索「{query}」{'(缓存)' if cached else ''},"
                 f"共 {len(results)} 条结果:"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']} — {r['url']}")
        summary = "\n".join(lines) if results else (
            f"联网搜索「{query}」未找到相关结果")

        return ToolResult(
            artifact=artifact,
            artifact_type="data",
            summary=summary,
            formatted=self.format_result(artifact),
        )

    def format_result(self, artifact: dict) -> dict:
        """SSE data 制品卡摘要字段(键与 config.yaml data_labels 对齐)。"""
        art = artifact or {}
        return {
            "query": art.get("query") or "",
            "resultCount": len(art.get("results") or []),
            "cached": "是" if art.get("cached") else "否",
        }

    def summarize_artifact(self, artifact: dict) -> str:
        art = artifact or {}
        return (f"联网搜索「{art.get('query', '')}」"
                f"命中 {len(art.get('results') or [])} 条结果")
