"""web_search pack 底层 —— SearXNG 联网搜索(工具/管理端调研/客服直连共用)。

独立公用插件(用户裁决):知识库/客服直连本模块(照客服直连 kg/chatbi 底层模式)。

【链路】settings_reader 读 searxng_url(设置页/env;空=未启用 credential)
→ httpx GET {url}/search?q=..&format=json&engines=google(engines 必须传
引擎全名——SearXNG 的 shortcut 参数实测不生效) → 裁剪 top_k(title/url/
content 三字段,来源 URL 必带——受控架构的诚实原则) → TTL 缓存 10 分钟
(同 query 不重复出站,LLM 降频同款思路)。

【调研记录】web_search_logs 表(PackRelationalDB 独立 schema,幂等懒建):
管理端"联网调研"每次查询落库,对话链路的记录走既有会话+链路追踪,
不经此表。

【错误信封】未配置 URL=credential;上游超时/网络=retryable
(引擎对 retryable 同参数自动重试一次)。
"""
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from sdk.relational_store import PackRelationalDB
from sdk.tool import PackInvokeError

logger = logging.getLogger(__name__)

_prompt_loader = None


def _prompts():
    """KG pack 提示词加载器(照 retrieval._prompt_loader 模式)。"""
    global _prompt_loader
    if _prompt_loader is None:
        from sdk.prompt_loader import PromptLoader
        _prompt_loader = PromptLoader(
            packs_root=Path(__file__).resolve().parent.parent)
    return _prompt_loader

# 同 query 10 分钟内直接回缓存(运营调研高频重复同一问题的场景)
CACHE_TTL_S = 600.0
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()

# 引擎固定 google(实测:百度机房 IP 秒撞验证、Bing 中文专业词质量差,
# 3.110 栈上 google 修复版 20/20 全绿——见 doc/agents/deploy-searxng-3110.md)
_ENGINE = "google"
_TIMEOUT_S = 15.0
_client: Optional[httpx.Client] = None


def reset_cache() -> None:
    """清空查询缓存(测试用;生产无需——TTL 自然过期)。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def settings_url(app_state) -> str:
    """读 searxng_url 设置(设置页保存值 > env > 空)。"""
    try:
        from sdk.pack_api import settings_reader as _sdk_reader
        return (_sdk_reader(app_state, "web_search").get("searxng_url")
                or "").strip()
    except Exception:
        return ""


def _get_client() -> httpx.Client:
    """懒建单例(照 llm/client.py rerank 客户端模式)。"""
    global _client
    if _client is None:
        _client = httpx.Client(timeout=_TIMEOUT_S)
    return _client


def search(app_state, query: str, top_k: int = 5) -> Dict[str, Any]:
    """联网搜索:返回 {query, results:[{title,url,content}], cached}。

    未配置 searxng_url 抛 credential;上游不可达抛 retryable。
    """
    query = (query or "").strip()
    if not query:
        raise PackInvokeError("搜索词不能为空", PackInvokeError.PERMANENT)

    with _CACHE_LOCK:
        hit = _CACHE.get(query)
        if hit and time.monotonic() - hit["at"] < CACHE_TTL_S:
            return {"query": query, "results": hit["results"][:top_k],
                    "cached": True}

    base = settings_url(app_state)
    if not base:
        raise PackInvokeError(
            "联网搜索未启用:知识库插件设置里未配置 SearXNG 地址(searxng_url)",
            PackInvokeError.CREDENTIAL)
    base = base.rstrip("/")

    try:
        resp = _get_client().get(
            f"{base}/search", params={
                "q": query, "format": "json", "engines": _ENGINE})
        resp.raise_for_status()
        payload = resp.json()
    except httpx.TimeoutException as e:
        # 文案不含底层异常串(可能带内部地址,端点会透传给管理端);
        # 细节进日志
        logger.warning(f"searxng 请求超时: {e}")
        raise PackInvokeError(
            f"联网搜索超时({_TIMEOUT_S}s),请稍后重试",
            PackInvokeError.RETRYABLE) from e
    except httpx.HTTPError as e:
        logger.warning(f"searxng 不可达: {e}")
        raise PackInvokeError(
            "联网搜索服务不可达,请稍后重试或检查 SearXNG 容器状态",
            PackInvokeError.RETRYABLE) from e

    # 引擎被挂起(SearXNG 200 但 google 限流/验证,unresponsive 列表点名):
    # 视为瞬时故障抛 retryable(引擎自动重试一次),不把"引擎挂了"当"没搜到"
    for u in (payload.get("unresponsive_engines") or []):
        if isinstance(u, (list, tuple)) and u and u[0] == _ENGINE:
            raise PackInvokeError(
                "搜索引擎暂时不可用(可能被限流),请稍后重试",
                PackInvokeError.RETRYABLE)

    # 裁剪三字段;无结果也如实返回空列表(诚实原则,不硬凑)
    results = [
        {"title": (r.get("title") or "").strip(),
         "url": (r.get("url") or "").strip(),
         "content": (r.get("content") or "").strip()}
        for r in (payload.get("results") or [])
        if (r.get("url") or "").strip()
    ][:top_k]

    # 只缓存非空结果:引擎瞬时抖动产出的空结果若入缓存,10 分钟内同词
    # 全部"未找到",把瞬时故障放大成持续故障(实测踩坑)。真零命中不缓存
    if results:
        with _CACHE_LOCK:
            _CACHE[query] = {"results": results, "at": time.monotonic()}

    return {"query": query, "results": results, "cached": False}


def synthesize_answer(app_state, query: str, results: List[dict],
                      conv_id: Optional[str] = None) -> str:
    """LLM 综合回答(DeepSeek/Perplexity 式总结,非链接列表)。

    搜索结果注入 web_answer.j2 模板 → LLM 归纳成连贯回答,句尾带
    [来源:N] 引用(前端渲染成角标徽标,点击跳来源卡)。LLM 不可用时
    降级为纯结果列表摘要(fail-open:搜索已成功,不因综合失败丢结果)。
    """
    if not results:
        return f"联网搜索「{query}」未找到相关结果"
    try:
        prompt = _prompts().render("web_search", "web_answer",
                                   query=query, results=results)
        answer = app_state.llm_client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2, conv_id=conv_id, stage="kg.web_answer",
        ).strip()
        if answer:
            return answer
        logger.warning("web_answer LLM 返回空,降级结果列表")
    except Exception as e:
        logger.warning(f"web_answer 综合失败(降级结果列表): {e}")
    # 降级:带来源的列表摘要(诚实原则:来源 URL 必带)
    lines = [f"联网搜索「{query}」,共 {len(results)} 条结果:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} — {r['url']}")
    return "\n".join(lines)


# ── 调研记录(web_search_logs;管理端调研页持久化) ─────────────

_db: Optional[PackRelationalDB] = None


def _conn(app_state):
    """本 pack 关系存储(web_search schema,chatbi 同款 PackRelationalDB)。"""
    global _db
    if _db is None:
        _db = PackRelationalDB("web_search")
    return _db.connect()


def _ensure_table(app_state) -> None:
    """web_search_logs 建表(幂等懒建;原 KG 时期表 kg_web_search_logs 留孤儿)。"""
    with _conn(app_state) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_search_logs (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                results_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                cached INTEGER NOT NULL DEFAULT 0,
                operator TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""")


def log_search(app_state, query: str, results: List[dict], cached: bool,
               operator: str = "") -> Dict[str, Any]:
    """一条调研记录落库,返回 {id, query, resultCount, cached, operator}。"""
    _ensure_table(app_state)
    lid = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _conn(app_state) as conn:
        conn.execute(
            """INSERT INTO web_search_logs
               (id, query, results_json, result_count, cached, operator, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lid, query, json.dumps(results, ensure_ascii=False),
             len(results), 1 if cached else 0, operator, now))
    return {"id": lid, "query": query, "resultCount": len(results),
            "cached": cached, "operator": operator}


def list_logs(app_state, limit: int = 50) -> List[Dict[str, Any]]:
    """最近调研记录(新→旧)。"""
    _ensure_table(app_state)
    with _conn(app_state) as conn:
        rows = conn.execute(
            """SELECT id, query, results_json, result_count, cached,
                      operator, created_at
               FROM web_search_logs ORDER BY created_at DESC
               LIMIT ?""", (int(limit),)).fetchall()
    out = []
    for r in rows:
        try:
            results = json.loads(r["results_json"] or "[]")
        except Exception:
            results = []
        out.append({
            "id": r["id"], "query": r["query"], "results": results,
            "resultCount": r["result_count"], "cached": bool(r["cached"]),
            "operator": r["operator"], "createdAt": r["created_at"]})
    return out


def clear_logs(app_state) -> int:
    """清空调研记录,返回删除条数。"""
    _ensure_table(app_state)
    with _conn(app_state) as conn:
        cur = conn.execute("DELETE FROM web_search_logs")
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
