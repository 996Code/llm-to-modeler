"""管理端 API —— 会话审计、调用日志、插件启停的统一入口。

【模块定位】
本服务的常规 API 无登录态(身份由上游透传 X-User-Id),但"看所有用户的
对话 / 拉全量调用日志 / 切换插件启停"是运维能力。本路由把这组能力收拢到
/api/admin 前缀下,访问模式由 ADMIN_TOKEN 决定:

  - 未配置 ADMIN_TOKEN(默认)→ 管理端**开放访问**,无口令直接用
    (内网/网关后部署的取舍;启动日志会打醒目警告)
  - 配置 ADMIN_TOKEN → 请求必须带 X-Admin-Token 头且常量时间比对相等
  - conversations.py 里 user_id == "admin" 的越权分支与管理端同模式:
    开放模式下 admin 用户名即可跨用户,口令模式下须带合法口令

【端点清单】
  GET    /api/admin/stats                     → 概览统计(会话/用户/调用)
  GET    /api/admin/conversations             → 全量会话分页列表(?userId=&q=&limit=&offset=)
  GET    /api/admin/conversations/{id}        → 任意用户会话详情(含消息)
  DELETE /api/admin/conversations/{id}        → 删除任意用户会话
  GET    /api/admin/call-logs                 → 调用日志分页(?convId=&callType=&limit=&offset=)
  GET    /api/admin/packs                     → 全部已发现 pack 的启停状态 + manifest 摘要
  POST   /api/admin/packs/{name}/enable       → 启用 pack(热生效,立即持久化)
  POST   /api/admin/packs/{name}/disable      → 禁用 pack(热生效;最后一个不可禁)

【热切换链路】
enable/disable → PackState.set_enabled(落盘 data/pack_state.json)
              → pack_manager.assemble_packs(重新加载 + nodes.configure + 换 app.state)
新请求立即生效,无需重启;重启后 PackState 从状态文件还原。
"""
import logging
import os
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)


# ── 鉴权 ──────────────────────────────────────────────────────

def get_admin_token() -> Optional[str]:
    """读取管理端口令(env ADMIN_TOKEN)。未配置返回 None = 管理端整体关闭。"""
    raw = os.getenv("ADMIN_TOKEN", "").strip()
    return raw or None


def is_admin_authorized(request: Request) -> bool:
    """判定请求是否有管理权限(布尔版,供 conversations.py 复用)。

    两种授权模式(与管理端一致):
      - 未配置 ADMIN_TOKEN → 开放模式,恒 True(内网部署取舍)
      - 已配置 → 必须携带匹配的 X-Admin-Token(compare_digest 常量时间
        比较,防时序侧信道逐字节猜口令)
    """
    token = get_admin_token()
    if not token:
        return True  # 开放模式
    supplied = request.headers.get("X-Admin-Token", "")
    return bool(supplied) and secrets.compare_digest(supplied, token)


async def require_admin(request: Request):
    """路由级依赖:口令模式下校验失败抛 401;开放模式直接放行。"""
    if get_admin_token() and not is_admin_authorized(request):
        raise HTTPException(401, "Invalid admin token")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ── 概览统计 ──────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(request: Request):
    """管理端仪表盘统计:存储聚合 + 当前 pack 启停摘要 + 鉴权模式。

    authMode 告知前端当前访问模式(open=开放直连 / token=口令守门),
    前端据此决定是否显示"退出"等口令模式才有的交互。
    """
    stats: Dict[str, Any] = request.app.state.conversation_store.get_admin_stats()
    stats["authMode"] = "open" if not get_admin_token() else "token"
    pack_state = getattr(request.app.state, "pack_state", None)
    if pack_state is not None:
        stats["packs"] = {
            "discovered": len(pack_state.discovered_names()),
            "enabled": len(pack_state.enabled_names()),
        }
    return stats


# ── 会话管理 ──────────────────────────────────────────────────

@router.get("/conversations")
async def admin_list_conversations(request: Request):
    """全量会话分页列表(不限用户)。

    Query 参数:
      limit/offset: 分页(limit 上限 200,防止一次拉爆内存)
      userId:       按用户精确过滤
      q:            按标题模糊过滤
    """
    store = request.app.state.conversation_store
    # max(1, ...):SQLite 对 LIMIT 负值按"不限制"处理,必须夹住下限防整表倾倒
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    user_id = (request.query_params.get("userId") or "").strip() or None
    q = (request.query_params.get("q") or "").strip() or None
    items = store.list_all_conversations(limit=limit, offset=offset, user_id=user_id, q=q)
    total = store.count_all_conversations(user_id=user_id, q=q)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/conversations/{conv_id}")
async def admin_get_conversation(conv_id: str, request: Request):
    """查看任意用户的会话详情（含全部消息）。"""
    conv = request.app.state.conversation_store.get_conversation_any_user(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("/conversations/{conv_id}/trace")
async def admin_conversation_trace(conv_id: str, request: Request):
    """会话链路追踪:全量事件流 + LLM/上游调用明细合并成统一时间线,按轮分组。

    分轮规则(以 assistant 事件闭轮):每轮 = 上一次回复之后的全部活动 +
    本次回复,展示序重排为「用户消息 → 链路活动(时间序)→ 助手回复」;
    末尾未闭合的段(追问挂起/异常中断)单独成轮。这样对"消息先落库"与
    "消息在轮末落库"(引擎现状,见 stream.py _save_conversation)两种
    时序都正确。

    LLM 调用的环节标注(stage)来自调用日志 request_data.stage:
      route_pack(一级路由)/route_tool(二级路由)/create_form.generate/
      image_form.analyze/compress_history 等(见 llm/client.py 与各调用点)。
    """
    store = request.app.state.conversation_store
    conv = store.get_conversation_any_user(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    events = store.load_events(conv_id)
    calls = store.get_call_logs(conv_id=conv_id, limit=500)
    return _build_trace(conv, events, calls)


def _parse_ts(iso: Optional[str]):
    """ISO 时间戳 → datetime(解析失败返回 None,排序兜底用 datetime.min)。"""
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _build_trace(conv: Dict[str, Any], events: List[Dict[str, Any]], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把事件流 + 调用日志合并为分轮时间线(纯函数,便于单测)。

    时间线排序:时间升序;同一毫秒内事件先于调用(事件是轮次边界,
    调用是边界内的活动)——Python sort 稳定,先放事件再放调用即天然满足。
    """

    timeline: List[Dict[str, Any]] = []
    for e in events:
        timeline.append({
            "at": e["created_at"],
            "type": "event",
            "kind": e["kind"],
            "payload": e["payload"],
        })
    for c in calls:
        req = c.get("request_data")
        timeline.append({
            "at": c["created_at"],
            "type": "call",
            "callType": c["call_type"],
            "endpoint": c["endpoint"],
            "stage": req.get("stage") if isinstance(req, dict) else None,
            "statusCode": c["status_code"],
            "durationMs": c["duration_ms"],
            "errorMessage": c["error_message"],
            "requestData": req,
            "responseData": c["response_data"],
        })
    timeline.sort(key=lambda x: _parse_ts(x["at"]) or datetime.min)

    # ── 分轮:以 assistant 事件"闭轮" ──
    # 为什么不用"user 事件开轮"?引擎在整轮结束时才把 user/assistant 消息
    # 落库(真实时间线上它们排在末尾,见 stream.py 的 _save_conversation),
    # 而"用户消息先落库"的旧数据/手工种子也存在。以 assistant 闭轮对两种
    # 时序都正确:每个 segment = 上一次回复之后的全部活动 + 本次回复,
    # 段内的 user 事件(无论在前在后)即该轮的用户消息。
    # 轮次耗时的口径:首项时间戳 → 末项(assistant)时间戳,即"这一轮从
    # 请求进来到回复完成"的墙钟耗时。
    turns: List[Dict[str, Any]] = []
    current: Dict[str, Any] = _new_turn()

    def _close_turn():
        items = current["items"]
        if not items:
            return
        # 段内展示重排:user 事件提到最前、assistant 压到最后,
        # 中间活动保持时间序——时间线上读起来是"用户→链路→回复"的逻辑流,
        # 每项仍带真实时间戳(落库时刻)
        users = [i for i in items if i["type"] == "event" and i["kind"] == "user"]
        assistants = [i for i in items if i["type"] == "event" and i["kind"] == "assistant"]
        middles = [i for i in items if i not in users and i not in assistants]
        current["items"] = users + middles + assistants

        user_item = users[0] if users else None
        current["userContent"] = (user_item["payload"] or {}).get("content") if user_item else None
        current["startedAt"] = items[0]["at"]
        current["endedAt"] = items[-1]["at"]
        start, end = _parse_ts(current["startedAt"]), _parse_ts(current["endedAt"])
        current["wallMs"] = (
            int((end - start).total_seconds() * 1000) if start and end and end >= start else 0
        )
        turns.append(current)

    for item in timeline:
        # assistant 特判先于通用 append:见下方注释
        if item["type"] == "event" and item["kind"] == "assistant":
            if not current["items"] and turns:
                # 本段无任何活动且上一轮存在 → 同轮的补充消息(真实场景:
                # stream.py 对工具轮会落两条 assistant——summary 与制品快照,
                # 第二条紧随第一条)。并入上一轮,不另起空轮(墙钟 0ms 的假轮)。
                prev = turns[-1]
                prev["items"].append(item)
                prev["endedAt"] = item["at"]
                s0, e0 = _parse_ts(prev["startedAt"]), _parse_ts(prev["endedAt"])
                if s0 and e0 and e0 >= s0:
                    prev["wallMs"] = int((e0 - s0).total_seconds() * 1000)
                continue
            current["items"].append(item)
            _accumulate(current, item)
            _close_turn()
            current = _new_turn()
            continue
        current["items"].append(item)
        _accumulate(current, item)
    _close_turn()  # 末尾未闭合的段(追问挂起/异常中断的轮次)

    for i, t in enumerate(turns):
        t["index"] = i + 1  # 1 起(用户视角的"第几轮");无 user 消息的残留段前端显示"初始化"

    llm_calls = [i for i in timeline if i["type"] == "call" and i["callType"] == "llm"]
    upstream_calls = [i for i in timeline if i["type"] == "call" and i["callType"] == "upstream"]
    trace_events = [e for e in events if e["kind"] == "trace"]
    user_turns = [t for t in turns if t["userContent"] is not None]
    return {
        "conversation": conv,
        "summary": {
            "turns": len(user_turns),
            "events": len(events),
            "traceEvents": len(trace_events),
            "llmCalls": len(llm_calls),
            "llmMs": sum(i["durationMs"] or 0 for i in llm_calls),
            "upstreamCalls": len(upstream_calls),
            "upstreamMs": sum(i["durationMs"] or 0 for i in upstream_calls),
            "firstAt": timeline[0]["at"] if timeline else None,
            "lastAt": timeline[-1]["at"] if timeline else None,
        },
        "turns": turns,
    }


def _new_turn() -> Dict[str, Any]:
    """创建一轮的聚合容器(userContent/时间戳由 _close_turn 收尾时统一填充)。"""
    return {
        "index": 0,
        "userContent": None,
        "startedAt": "",
        "endedAt": "",
        "wallMs": 0,
        "llmCount": 0,
        "llmMs": 0,
        "upstreamCount": 0,
        "upstreamMs": 0,
        "items": [],
    }


def _accumulate(turn: Dict[str, Any], item: Dict[str, Any]) -> None:
    """把时间线项累加进轮次聚合(调用次数/耗时)。"""
    if item["type"] != "call":
        return
    if item["callType"] == "llm":
        turn["llmCount"] += 1
        turn["llmMs"] += item["durationMs"] or 0
    elif item["callType"] == "upstream":
        turn["upstreamCount"] += 1
        turn["upstreamMs"] += item["durationMs"] or 0


@router.delete("/conversations/{conv_id}")
async def admin_delete_conversation(conv_id: str, request: Request):
    """删除任意用户的会话(级联删除事件流,语义同用户自删)。"""
    if not request.app.state.conversation_store.delete_conversation_any_user(conv_id):
        raise HTTPException(404, "Conversation not found")
    logger.info(f"admin deleted conversation {conv_id}")
    return {"success": True}


# ── 调用日志 ──────────────────────────────────────────────────

@router.get("/call-logs")
async def admin_call_logs(request: Request):
    """LLM/上游调用日志分页查询(排查"模型答了什么/上游回了什么"的审计入口)。

    Query 参数:
      limit/offset: 分页(limit 上限 200;request/response 全文可能很大)
      convId:       只看某会话的调用
      callType:     llm / upstream
    """
    store = request.app.state.conversation_store
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    conv_id = (request.query_params.get("convId") or "").strip() or None
    call_type = (request.query_params.get("callType") or "").strip() or None
    return store.query_call_logs(
        conv_id=conv_id, call_type=call_type, limit=limit, offset=offset
    )


# ── 插件(pack)管理 ────────────────────────────────────────────

def _packs_payload(request: Request) -> Dict[str, Any]:
    """组装插件管理页数据:全量发现清单 + 启停状态 + manifest 摘要。

    manifest 摘要对全部 pack 提供(含禁用的,管理端本来就有权看);
    工具清单只对已启用的 pack 提供(pack_tools 只在装配时生成)。
    """
    # 延迟导入:domains 是重型模块(触发 pack 子模块加载),管理端调用频率低,
    # 但也不必在 api 层 import 期引入(services/domains 分层保持单向)
    from domains import load_pack_configs

    pack_state = request.app.state.pack_state
    pack_tools: Dict[str, List[str]] = getattr(request.app.state, "pack_tools", {}) or {}
    # 全量 manifest(不过滤启停,供禁用中的 pack 也能展示声明信息)
    all_configs = load_pack_configs(pack_names=pack_state.discovered_names())

    items = []
    for name in pack_state.discovered_names():
        cfg = all_configs.get(name, {})
        domain = cfg.get("domain", {}) or {}
        services = cfg.get("services", {}) or {}
        items.append({
            "name": name,
            "enabled": pack_state.is_enabled(name),
            "description": domain.get("description", ""),
            "fallback": domain.get("fallback", ""),
            "artifactType": (cfg.get("artifact", {}) or {}).get("type", "config"),
            "services": sorted(services.keys()),
            "tools": pack_tools.get(name, []),
        })
    return {
        "items": items,
        "stateFile": pack_state.state_path,
        "source": pack_state.source,
    }


@router.get("/packs")
async def admin_list_packs(request: Request):
    """列出全部已发现的 pack 与启停状态(含禁用中的,便于重新启用)。"""
    return _packs_payload(request)


@router.post("/packs/{name}/enable")
async def admin_enable_pack(name: str, request: Request):
    """启用 pack:更新状态并热切换引擎装配(无需重启)。"""
    return _toggle_pack(request, name, True)


@router.post("/packs/{name}/disable")
async def admin_disable_pack(name: str, request: Request):
    """禁用 pack:更新状态并热切换引擎装配(无需重启)。"""
    return _toggle_pack(request, name, False)


def _toggle_pack(request: Request, name: str, enabled: bool):
    """启停共同实现:校验 → 改状态(落盘) → 热装配 → 返回最新列表。

    Raises:
        404: pack 不存在(未发现该目录)。
        400: 试图禁用最后一个启用的 pack(会让引擎无工具可用)。
        503: 热装配失败(状态已落盘但引擎还是旧装配——返回错误让运维感知,
             下次重启会按状态文件载入正确集合)。
    """
    # 裸包名 import(非 src.services.*):双根 sys.path 下 src.X 与 X 是两个
    # 模块对象,thread-local(如请求 services 表)跨副本不可见(见 main.py 同款注释)
    from services.pack_manager import assemble_packs

    pack_state = request.app.state.pack_state
    if not pack_state.is_discovered(name):
        raise HTTPException(404, f"Pack '{name}' not found")
    if not enabled and len(pack_state.enabled_names()) <= 1:
        raise HTTPException(400, "Cannot disable the last enabled pack")

    changed = pack_state.set_enabled(name, enabled)
    result = {"changed": changed, **_packs_payload(request)}
    # 无论状态是否变化都重新装配:装配幂等(importlib 有模块缓存,开销毫秒级),
    # 且能自愈"上次状态已落盘但装配失败"的残留(否则引擎与状态不一致要到重启才恢复)
    try:
        summary = assemble_packs(request.app.state, sorted(pack_state.enabled_names()))
        result["loaded"] = summary["loaded"]
        result["toolCount"] = summary["tools"]
    except Exception as e:
        logger.exception(f"hot-reload packs failed after toggling {name}")
        raise HTTPException(503, f"State saved but hot-reload failed: {e}")
    return result


# ── 工具 ──────────────────────────────────────────────────────

def _int_param(request: Request, name: str, default: int) -> int:
    """解析整型 query 参数,非法值回退默认(管理端 UI 传错不炸 500)。"""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
