"""Shared search orchestration for conversation tools and consuming packs."""
import logging
import time

from sdk.tool import PackInvokeError, ToolResult

from domains.web_search import websearch

logger = logging.getLogger(__name__)
_WEB_HINTS = ("上网", "联网", "网上查", "网上搜", "搜索引擎", "百度一下", "谷歌一下", "互联网上")
_STEPS = [
    {"key": "web_search.search", "label": "联网搜索"},
    {"key": "web_search.assemble", "label": "整理结果与来源"},
    {"key": "web_search.answer", "label": "综合归纳回答"},
]


def enabled(state):
    params = (state.get("pack_params") or {}).get("web_search") or {}
    return isinstance(params, dict) and params.get("enabled") is True


def requested(state):
    query = str(state.get("user_input") or state.get("query") or "")
    return enabled(state) and any(hint in query for hint in _WEB_HINTS)


def _provider(app_state):
    registry = getattr(app_state, "registry", None)
    return registry.get("web_search") if registry is not None else None


def available(app_state):
    return _provider(app_state) is not None


def request_guard(app_state):
    provider = _provider(app_state)

    def check():
        if provider is None or _provider(app_state) is not provider:
            raise PackInvokeError("联网搜索插件已停用或重载，请重新发起请求", PackInvokeError.PERMANENT)
    return check


def answer(app_state, state, ctx, owner="web_search"):
    query = str(state.get("query") or state.get("user_input") or "").strip()
    top_k = state.get("top_k") or 5
    try:
        top_k = max(1, min(int(top_k), 10))
    except (TypeError, ValueError):
        top_k = 5
    detail = {"query": query, "enabled": enabled(state), "top_k": top_k}
    ctx.trace("web_search.request", "联网搜索请求", "info", detail=detail)
    started = time.monotonic()
    check = request_guard(app_state)
    try:
        if not enabled(state):
            raise PackInvokeError("联网搜索未开启，请点亮输入框的「联网」开关", PackInvokeError.PERMANENT)
        check()
        ctx.emit("pipeline_definition", {"tool": "web_search", "steps": _STEPS})
        ctx.emit("stage", "web_search.search", "正在规划检索词并联网搜索…")
        out = websearch.search_multi(app_state, query, top_k=top_k,
                                     conv_id=ctx.conv_id, before_request=check)
        results = out["results"]
        detail.update(plannedQueries=out.get("queries") or [query],
                      cached=bool(out.get("cached")), resultCount=len(results),
                      # trace 是高频审计流,结果明细只留前 5 条(全量 20 条噪音)
                      results=[{"title": r["title"][:60], "url": r["url"]} for r in results[:5]])
        ctx.emit("stage", "web_search.assemble", "整理结果与来源…")
        ctx.emit("stage", "web_search.answer", "正在根据问题综合归纳…")
        check()
        text = websearch.synthesize_answer(app_state, query, results, conv_id=ctx.conv_id)
        check()
    except Exception as exc:
        message = exc.message if isinstance(exc, PackInvokeError) else "联网搜索处理失败，请稍后重试"
        ctx.trace("web_search.search", "联网搜索失败", "error",
                  duration_ms=int((time.monotonic() - started) * 1000),
                  detail={**detail, "error": message})
        if isinstance(exc, PackInvokeError):
            raise
        logger.exception("web search orchestration failed")
        raise PackInvokeError(message, PackInvokeError.PERMANENT) from exc
    ctx.trace("web_search.search", "联网搜索", "ok",
              duration_ms=int((time.monotonic() - started) * 1000), detail=detail)
    artifact = {"type": "web_search_result", "pack": owner, "resultDisplay": "card",
                "query": query, "queries": out.get("queries") or [query],
                "results": results, "cached": bool(out.get("cached")), "answer": text}
    return ToolResult(artifact=artifact, artifact_type="data", summary=text,
                      formatted={"query": query, "resultCount": len(results),
                                 "cached": "是" if out.get("cached") else "否"})
