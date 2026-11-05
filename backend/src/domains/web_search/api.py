"""web_search pack 自有 HTTP API(管理端联网调研页)。

挂载:services/pack_api_mount.py 统一挂 /api/packs/web_search。
对话链路走 WebSearchTool(引擎),不经这些端点。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from sdk.pack_api import admin_required
from sdk.tool import PackInvokeError

logger = logging.getLogger(__name__)

router = APIRouter()


# ── 联网搜索(管理端调研 tab;对话链路走 WebSearchTool,不经此) ──

@router.post("/web-search", dependencies=[Depends(admin_required)])
async def web_search(request: Request, payload: Dict[str, Any]):
    """联网调研(管理端"联网调研"页):SearXNG→google,结果必带来源链接。

    每次查询落 kg_web_search_logs(操作人/结果/缓存命中),供"最近调研"
    回看。Body: {query: 搜索词, top_k?: 条数(默认5,上限10)}
    """
    query = str((payload or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(422, "query 不能为空")
    top_k = payload.get("top_k") or 5
    try:
        top_k = max(1, min(int(top_k), 10))
    except (TypeError, ValueError):
        top_k = 5

    from domains.web_search import websearch
    # 操作人标识:X-Admin-Token 摘要前 8 位(区分操作人,不存完整凭证);
    # 开放模式(未配置 ADMIN_TOKEN)留空
    _tok = request.headers.get("X-Admin-Token", "")
    operator = (hashlib.sha256(_tok.encode()).hexdigest()[:8] if _tok else "")
    try:
        # httpx 同步调用丢线程池(事件循环不冻结,与 retrieve-test 同款)
        out = await asyncio.to_thread(
            websearch.search, request.app.state, query, top_k)
    except PackInvokeError as e:
        # 结构化错误如实转达(引擎被限流/底座未配置各有其文案)——
        # 笼统的"检查 SearXNG 地址"会把限流误导成配置问题(实测踩坑)
        logger.warning(f"web-search failed: {e.message}")
        raise HTTPException(502, e.message)
    except Exception:
        # 异常详情只进服务端日志;管理端点不回显内部地址等拓扑
        logger.exception("web-search failed")
        raise HTTPException(502, "联网搜索失败,请稍后重试或检查插件设置里的 SearXNG 地址")
    # LLM 综合回答(总结式,DeepSeek/Perplexity 式;失败降级列表摘要)
    answer = await asyncio.to_thread(
        websearch.synthesize_answer, request.app.state, query,
        out["results"])
    # 调研记录落库失败不拖垮已成功的搜索+回答(审查 I3):降级返回,
    # logId 缺席 + 服务端日志留痕
    try:
        log = await asyncio.to_thread(
            websearch.log_search, request.app.state, query, out["results"],
            bool(out.get("cached")), operator=operator)
        return out | {"answer": answer, "logId": log["id"]}
    except Exception:
        logger.exception("web-search 调研记录落库失败(结果正常返回)")
        return out | {"answer": answer}


@router.get("/web-search/logs", dependencies=[Depends(admin_required)])
async def web_search_logs(request: Request, limit: int = 50):
    """最近调研记录(新→旧,默认50条)。"""
    from domains.web_search import websearch
    return await asyncio.to_thread(
        websearch.list_logs, request.app.state, max(1, min(limit, 200)))


@router.delete("/web-search/logs", dependencies=[Depends(admin_required)])
async def web_search_logs_clear(request: Request):
    """清空调研记录。"""
    from domains.web_search import websearch
    await asyncio.to_thread(websearch.clear_logs, request.app.state)
    return {"ok": True}
