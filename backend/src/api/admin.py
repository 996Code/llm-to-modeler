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
    """路由级依赖：token 有效时等同于管理员；否则回退 ADMIN_TOKEN 模式。"""
    from api.auth import is_token_authorized
    if is_token_authorized(request):
        return  # 新 token 认证通过
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
    # max(1, ...):LIMIT 负值等价"不限制",必须夹住下限防整表倾倒
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    user_id = (request.query_params.get("userId") or "").strip() or None
    q = (request.query_params.get("q") or "").strip() or None
    # packs:插件多选过滤(逗号分隔;空串元素=「其他」,即无路由记录的会话)
    packs_raw = (request.query_params.get("packs") or "").strip()
    packs = [p.strip() for p in packs_raw.split(",") if p.strip() != ""] if packs_raw else None
    # 显式传 packs= (空值但存在) 或含 "__other__" → 只要"其他"
    if packs_raw and not packs:
        packs = [""]
    if packs and "__other__" in packs:
        packs = [p if p != "__other__" else "" for p in packs]
    items = store.list_all_conversations(
        limit=limit, offset=offset, user_id=user_id, q=q, packs=packs)
    total = store.count_all_conversations(user_id=user_id, q=q, packs=packs)
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
      route_pack(一级路由)/route_tool(二级路由)/tool_a.generate/
      tool_b.analyze/compress_history 等(见 llm/client.py 与各调用点)。
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
    graph_calls = [i for i in timeline if i["type"] == "call" and i["callType"] == "graph"]
    vector_calls = [i for i in timeline if i["type"] == "call" and i["callType"] == "vector"]
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
            "graphCalls": len(graph_calls),
            "vectorCalls": len(vector_calls),
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
        "graphCount": 0,
        "vectorCount": 0,
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
    elif item["callType"] == "graph":
        turn["graphCount"] += 1
    elif item["callType"] == "vector":
        turn["vectorCount"] += 1


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
      callType:     llm / upstream / graph / vector
      packName:     只看某插件的调用(归属维度观测)
    """
    store = request.app.state.conversation_store
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    conv_id = (request.query_params.get("convId") or "").strip() or None
    call_type = (request.query_params.get("callType") or "").strip() or None
    pack_name = (request.query_params.get("packName") or "").strip() or None
    return store.query_call_logs(
        conv_id=conv_id, call_type=call_type, pack_name=pack_name,
        limit=limit, offset=offset
    )


@router.get("/call-stats")
async def admin_call_stats(request: Request):
    """调用统计:按环节(stage)聚合 token 用量与调用次数。

    面向"这次 LLM 调用是在做意图路由还是 SQL 生成"的按环节成本透视——
    只聚合 call_type=llm 且 request_data 带 stage 的记录,其他类型
    (upstream/graph/vector)无 token 统计意义。

    Query 参数:
      packName: 只统计该插件的调用(BI 维度成本透视)
    """
    store = request.app.state.conversation_store
    pack_name = (request.query_params.get("packName") or "").strip() or None
    return store.get_call_stats(pack_name=pack_name)


@router.get("/audit-logs")
async def admin_audit_logs(request: Request):
    """业务审计日志分页查询(谁在什么时候对什么资源做了什么)。

    Query 参数:
      limit/offset: 分页
      resourceType: 资源类型(datasource/semantic/dashboard/memory 等)
      action:       动作(create/update/delete/scan/rollback 等)
      userId:       操作者
      packName:     归属插件
    """
    store = request.app.state.conversation_store
    limit = max(1, min(_int_param(request, "limit", 20), 200))
    offset = max(0, _int_param(request, "offset", 0))
    resource_type = (request.query_params.get("resourceType") or "").strip() or None
    action = (request.query_params.get("action") or "").strip() or None
    user_id = (request.query_params.get("userId") or "").strip() or None
    pack_name = (request.query_params.get("packName") or "").strip() or None
    return store.query_audit_logs(
        resource_type=resource_type, action=action, user_id=user_id,
        pack_name=pack_name, limit=limit, offset=offset,
    )


# ── 插件(pack)管理 ────────────────────────────────────────────

def _dependency_status_for(request: Request, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取某 pack 的依赖检测状态。

    优先用最近一次装配的缓存结果(装配时已跑过探针,状态最真实);
    不在装配名单里的 pack(禁用中)即时评估配置存在性——不跑探针,
    列表接口不能被网络探测拖慢。
    """
    cached = (getattr(request.app.state, "pack_dependency_status", None) or {}).get(name)
    if cached:
        return cached
    from services.pack_dependency import evaluate_pack
    return evaluate_pack(
        name, cfg, getattr(request.app.state, "settings_store", None), use_probe=False
    )


def _packs_payload(request: Request) -> Dict[str, Any]:
    """组装插件管理页数据:全量发现清单 + 启停状态 + manifest 摘要 + 依赖状态。

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
        admin_cfg = cfg.get("admin", {}) or {}
        dep = _dependency_status_for(request, name, cfg)
        items.append({
            "name": name,
            "enabled": pack_state.is_enabled(name),
            "description": domain.get("description", ""),
            "fallback": domain.get("fallback", ""),
            "artifactType": (cfg.get("artifact", {}) or {}).get("type", "config"),
            "services": sorted(services.keys()),
            "tools": pack_tools.get(name, []),
            # 依赖检测:ok / missing_dependency / probe_failed + 缺失清单
            "dependency": dep,
            # 声明式配置页/自定义管理页的注册信息(前端渲染入口)
            "hasSettings": bool(admin_cfg.get("settings")),
            "adminPage": admin_cfg.get("page", "") or "",
            "adminTitle": admin_cfg.get("title", "") or name,
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


def _dynamic_pack_mgmt_disabled_reason() -> str:
    """四十一审 P2: 部署级动态管理开关(PACK_DYNAMIC_PACK_MGMT)。

    多副本/多 Pod 非共享状态卷时, 各副本 holders=1, 代码无法区分
    "真单副本"与"非共享卷多副本"(fail-open)。部署清单显式声明:
      on(缺省) = 单副本/共享卷, 动态管理可用
      off      = 多副本非共享卷, toggle/recheck 一律拒绝(503),
                 只允许 PACKS_ENABLED + 滚动重启
    Returns:
        空串 = 允许; 非空 = 拒绝原因。
    """
    import os as _os
    mode = _os.getenv("PACK_DYNAMIC_PACK_MGMT", "on").strip().lower()
    if mode in ("off", "false", "0", "no"):
        return ("动态插件管理已被部署配置禁用(PACK_DYNAMIC_PACK_MGMT=off, "
                "多副本部署)——请使用 PACKS_ENABLED + 滚动重启")
    return ""


def _reject_multi_worker(request: Request) -> None:
    """三十六审 P2-B: 动态 pack 管理只支持单 worker。

    多 worker 时管理请求只落到一个 worker, 其他 worker 的 registry/
    nodes/handlers/routes 不会自动重装配——即使状态文件完全正确,
    后续请求打到不同 worker 会看到不同工具集。检测状态文件持有者
    数(PID 心跳), > 1 时拒绝动态管理(503), 提示重启为单 worker
    或改用 PACKS_ENABLED + 滚动重启。
    """
    # 四十一审 P2: 部署级禁用(多副本非共享卷 fail-closed)
    disabled_reason = _dynamic_pack_mgmt_disabled_reason()
    if disabled_reason:
        raise HTTPException(503, disabled_reason)
    from services.pack_state import count_state_file_holders
    # 三十八审预审(声称3): degraded 进程拒绝管理操作(重启是唯一出路)
    if getattr(request.app.state, "pack_runtime_degraded", False):
        raise HTTPException(
            503, "Process is degraded (previous rollback failed)—"
                 "restart required before further pack management.")
    holders = count_state_file_holders(
        request.app.state.pack_state.state_path)
    if holders != 1:
        # 三十七审 P2: holders != 1 都拒绝——>1 是多 worker 共享,
        # <0(含 -1)是检测失效, 一律 fail-closed 不放行
        if holders < 0:
            raise HTTPException(
                503,
                "插件状态持有者检测失败(fail-closed)——动态启停/重检"
                "暂不可用, 请检查状态文件目录权限后重试。")
        raise HTTPException(
            503,
            f"检测到 {holders} 个进程共享插件状态文件——动态启停/重检"
            f"只支持单 worker 部署(多 worker 的其他进程不会同步热切换)。"
            f"请用单 worker 重启, 或改用 PACKS_ENABLED 配置 + 滚动重启。")


def _toggle_pack(request: Request, name: str, enabled: bool):
    """启停共同实现:校验 → 改状态(落盘) → 热装配 → 返回最新列表。

    Raises:
        404: pack 不存在(未发现该目录)。
        400: 试图禁用最后一个启用的 pack(会让引擎无工具可用);
             或启用一个依赖配置缺失的 pack(先补配置或走设置页)。
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

    # 启用守卫:依赖配置缺失的 pack 拒绝启用(fail-closed;
    # 探针失败不在此拦——装配期探针会再判一次,这里只查"配置都没配")
    if enabled:
        from domains import load_pack_configs
        from services.pack_dependency import evaluate_pack
        cfg = load_pack_configs(pack_names=[name]).get(name) or {}
        dep = evaluate_pack(
            name, cfg, getattr(request.app.state, "settings_store", None), use_probe=False
        )
        if dep["status"] != "ok":
            raise HTTPException(
                400,
                f"依赖未满足,无法启用「{name}」: {dep['detail']}。"
                f"请在 .env 配置或在插件设置页补配后重试。",
            )

    # 三十四审 P2(自查补强): toggle 本身也必须进装配总锁——
    # 此前 set_enabled 在锁外, 两个管理员交错时 A 的回滚会覆盖 B
    # 刚写入的状态(B 正在锁内按旧 snapshot 装配, 状态文件却被 A
    # 改掉)。锁内完成 "toggle → 装配 → 失败回滚" 整段, 状态文件
    # 与装配 snapshot 严格同源。
    # 三十六审 P1-A(持久化后置): 状态先改内存不落盘, runtime commit
    # 成功后才持久化——文件只在 runtime 确认切换后写一次, 失败路径零落盘。
    # 三十八审 P1-A(事务化): assemble 返回事务 handle; persist 失败按
    # **快照确定性回滚**(不再重跑完整装配——反向 assemble 会重新执行
    # 依赖检测/hook, 本身可能失败, 失败后 runtime 与状态分裂); 回滚
    # 失败进入 degraded(app.state.pack_runtime_degraded=True, health
    # 暴露, 管理端拒绝后续操作), 响应如实报告。
    # 三十八审 P2-B(finalize 后置): lifecycle/unload 只在 persist 成功
    # 后执行——persist 失败时外部副作用尚未发生, 无需撤销。
    from services.pack_manager import (
        assemble_packs, finalize_assembly, hot_reload_lock,
        rollback_assembly)
    _reject_multi_worker(request)
    # 三十八审预审(声称1): degraded 进程拒绝后续管理操作——
    # 置位后唯一出路是重启(或运维确认恢复), 不能继续 toggle/recheck
    if getattr(request.app.state, "pack_runtime_degraded", False):
        raise HTTPException(
            503, "Process is degraded (previous rollback failed)—"
                 "restart required before further pack management.")
    with hot_reload_lock():
        changed = pack_state.set_enabled(
            name, enabled, persist=False)
        logger.info(f"pack toggled: {name} enabled={enabled} changed={changed}")
        result = {"changed": changed, **_packs_payload(request)}
        # 三十九审 P1-A: 事务阶段状态机——异常处理按阶段决策, 不再从
        # summary/changed 推断:
        #   ASSEMBLED  = runtime commit 完成(persist 未写盘)
        #   PERSISTED  = 磁盘已写入新状态(finalize 未执行)
        #   FINALIZED  = 生命周期/unload 完成(全部成功)
        # 只有 ASSEMBLED 阶段的失败才允许按旧快照回滚; PERSISTED 阶段的
        # 失败(finalize 抛)必须保持新 memory/runtime/disk 并 degraded
        # ——回滚会与已写磁盘分裂(三十九审 4.1 反例)。
        stage = "PENDING"
        summary = None
        try:
            summary = assemble_packs(
                request.app.state, sorted(pack_state.enabled_names()),
                app=request.app
            )
            result["loaded"] = summary["loaded"]
            result["toolCount"] = summary["tools"]
            stage = "ASSEMBLED"
            # runtime commit 成功 → 现在才持久化(失败路径零落盘)
            if changed:
                pack_state.persist()
            stage = "PERSISTED"
            # 磁盘确认 → finalize(生命周期/unload 等不可逆副作用)
            finalize_assembly(request.app.state, summary["_tx"])
            stage = "FINALIZED"
        except Exception as e:
            if stage == "PERSISTED" or (stage == "ASSEMBLED" and not changed):
                # finalize 失败: 磁盘(或 no-op 时的内存)已是新状态——
                # 保持新 memory/runtime/disk, 置 degraded(重启后按磁盘
                # 装配即恢复完整), 不回滚
                request.app.state.pack_runtime_degraded = True
                logger.exception(
                    f"finalize 失败(阶段 {stage}), 进程已降级(degraded), "
                    f"保持新装配与磁盘状态, 建议重启: {name}")
                raise HTTPException(
                    503, f"Finalize failed: {e}. New state kept "
                         f"(disk/memory/runtime), process is degraded—"
                         f"restart required.")
            if stage == "ASSEMBLED" and changed:
                # persist 失败: 磁盘未写, 按旧快照确定性回滚。
                # 四十审 P1: 内存补偿与 runtime 回滚**分开记录**——
                # 此前 set_enabled/clear_pending_ops 失败只记日志,
                # rollback_ok 仍为 True, 接口谎报 State rolled back
                # 而 memory 已留新值(与 runtime/disk 分裂, health 仍绿)。
                memory_rollback_ok = True
                try:
                    pack_state.set_enabled(
                        name, not enabled, persist=False)
                    pack_state.clear_pending_ops()
                except Exception as rollback_err:
                    memory_rollback_ok = False
                    logger.error(
                        f"hot-reload 失败且内存回滚也失败"
                        f"(进程将降级): {rollback_err}")
                runtime_rollback_ok = True
                try:
                    runtime_rollback_ok = rollback_assembly(
                        request.app.state, summary["_tx"])
                except Exception as rollback_err:
                    runtime_rollback_ok = False
                    logger.error(f"快照回滚异常: {rollback_err}")
                if not (memory_rollback_ok and runtime_rollback_ok):
                    # 任一回滚失败: 进入 degraded——health 暴露, 拒绝
                    # 后续管理/业务操作直到重启(不能谎称已回滚)
                    request.app.state.pack_runtime_degraded = True
                    logger.exception(
                        f"hot-reload 失败且回滚不完整"
                        f"(memory={memory_rollback_ok}, "
                        f"runtime={runtime_rollback_ok})——进程已降级"
                        f"(degraded), 建议重启恢复一致性")
                    raise HTTPException(
                        503,
                        f"Rollback FAILED after hot-reload error: {e} "
                        f"(memory_rollback={memory_rollback_ok}, "
                        f"runtime_rollback={runtime_rollback_ok}). "
                        f"Process is degraded—restart required.")
                logger.warning(
                    f"hot-reload/persist 失败, 已按快照回滚 {name} "
                    f"enabled={not enabled}(内存、运行态与状态文件一致)")
                logger.exception(
                    f"hot-reload packs failed after toggling {name}")
                raise HTTPException(
                    503, f"State rolled back, hot-reload failed: {e}")
            # stage == PENDING: assemble 自身失败(Prepare/commit 抛)。
            # commit 失败时 assemble 内部已按快照回滚——但三十九审 P1-B:
            # 内部恢复可能失败, assemble 用 AssemblyRollbackFailedError
            # 传播该信号(结构化), 上层据此 degraded
            from services.pack_manager import AssemblyRollbackFailedError
            if isinstance(e, AssemblyRollbackFailedError):
                request.app.state.pack_runtime_degraded = True
                logger.exception(
                    f"assemble commit 失败且内部快照恢复失败——进程已降级"
                    f"(degraded), 建议重启: {name}")
                raise HTTPException(
                    503, f"Rollback FAILED during assembly: {e}. "
                         f"Process is degraded—restart required.")
            if changed:
                try:
                    pack_state.set_enabled(
                        name, not enabled, persist=False)
                    pack_state.clear_pending_ops()
                except Exception as rollback_err:
                    request.app.state.pack_runtime_degraded = True
                    logger.error(
                        f"assemble 失败且内存回滚也失败(进程降级): "
                        f"{rollback_err}")
            logger.exception(
                f"hot-reload packs failed after toggling {name}")
            raise HTTPException(
                503, f"State rolled back, hot-reload failed: {e}")
    return result


# ── 插件设置(声明式配置页) ─────────────────────────────────────

def _apply_settings_side_effect(pack_name: str, eff: Dict[str, Any],
                                changed: Dict[str, Any], store) -> None:
    """执行 manifest 声明的设置保存副作用(admin.settings_side_effects)。

    平台内置副作用类型在此分发;未知类型记 warning 忽略(声明笔误不崩
    保存主流程)。副作用失败只记日志——保存本身已成功,不因副作用回滚。
    """
    etype = str((eff or {}).get("type") or "")
    if etype == "rate_limit":
        # 全局限速器是进程级单例,configure 重建桶——下一个 LLM 调用
        # 即按新限额。仅当声明的限速键在本次变更集内才触发。
        watch = {k for k in ("rpm", "tpm") if eff.get(k)} & set(changed)
        if not watch:
            return
        try:
            from llm.rate_limit import get_rate_limiter
            merged = store.get_values(pack_name)
            rpm = int(merged.get(eff.get("rpm")) or 0)
            tpm = int(merged.get(eff.get("tpm")) or 0)
            get_rate_limiter().configure(rpm=rpm, tpm=tpm)
            logger.info(f"rate limiter reconfigured ({pack_name}): rpm={rpm} tpm={tpm}")
        except Exception:
            logger.exception(f"rate limiter reconfigure failed ({pack_name})")
    else:
        logger.warning(f"未知设置副作用类型「{etype}」({pack_name}),已忽略")


def _pack_or_404(request: Request, name: str):
    pack_state = request.app.state.pack_state
    if not pack_state.is_discovered(name):
        raise HTTPException(404, f"Pack '{name}' not found")
    return pack_state


@router.get("/packs/{name}/settings")
async def admin_get_pack_settings(name: str, request: Request):
    """读取插件配置:schema(表单声明) + 已保存值(secret 掩码)。

    读 schema/保存值都不 import pack 模块——依赖未满足、未加载的插件
    也能打开设置页补配(这正是"设置页救活依赖缺失插件"的前提)。
    """
    _pack_or_404(request, name)
    from services.pack_settings import mask_secrets, read_settings_schema

    schema = read_settings_schema(name)
    if schema is None:
        raise HTTPException(404, f"Pack '{name}' 未声明 settings.schema.yaml(无配置页)")
    store = getattr(request.app.state, "settings_store", None)
    saved = store.get_values(name) if store else {}
    return {
        "name": name,
        "schema": schema,
        "values": mask_secrets(schema, saved),
    }


@router.put("/packs/{name}/settings")
async def admin_put_pack_settings(name: str, request: Request, payload: Dict[str, Any]):
    """保存插件配置(部分更新:只提交要改的键)。

    - 校验按 schema 做(类型/枚举/范围),schema 外的键整体拒绝。
    - secret 哨兵(未改动)被跳过,空串 = 清除该项(回落 env/默认)。
    - 保存后返回最新依赖状态(即时评估配置存在性,不跑探针)——
      前端据此提示"配置已生效,可点重新检测加载插件"。
    """
    _pack_or_404(request, name)
    from services.pack_settings import (
        mask_secrets, read_settings_schema, validate_values,
    )

    schema = read_settings_schema(name)
    if schema is None:
        raise HTTPException(404, f"Pack '{name}' 未声明 settings.schema.yaml(无配置页)")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(422, "请求体必须是 {\"values\": {...}}")

    clean, errors = validate_values(schema, values)
    if errors:
        raise HTTPException(422, detail={"message": "配置校验失败", "errors": errors})

    store = getattr(request.app.state, "settings_store", None)
    if store is None:
        raise HTTPException(503, "设置存储未初始化")
    store.save_values(name, clean)
    # 配置变了,探针结果缓存必须作废——否则补配后 60s 内 enable 路径仍
    # 命中旧的失败缓存,把"配置已改对"的插件继续拒载(设置页救活主路径)。
    from services.pack_dependency import clear_probe_cache
    clear_probe_cache(name)
    # 热生效副作用:按 manifest 的 admin.settings_side_effects 声明执行,
    # 平台在此分发内置副作用类型——api 层不认识任何具体插件
    from domains import load_pack_configs
    _cfg = load_pack_configs(pack_names=[name]).get(name) or {}
    for _eff in ((_cfg.get("admin") or {}).get("settings_side_effects")) or []:
        _apply_settings_side_effect(name, _eff, clean, store)
    # 审计留痕:配置变更是管理端敏感操作(依赖判定/连接凭据都可能随它改变),
    # 必须能在服务日志里追溯"谁在什么时候改了哪个插件的哪些项"。
    # 只记字段名不记值——secret 类字段的明文永不进日志。
    logger.info(f"pack settings saved: {name} fields={sorted(clean.keys())}")

    # 返回最新视图(值掩码 + 依赖状态),前端免一次往返
    saved = store.get_values(name)
    from domains import load_pack_configs
    from services.pack_dependency import evaluate_pack
    cfg = load_pack_configs(pack_names=[name]).get(name) or {}
    dep = evaluate_pack(name, cfg, store, use_probe=False)
    return {
        "name": name,
        "schema": schema,
        "values": mask_secrets(schema, saved),
        "dependency": dep,
    }


@router.post("/packs/{name}/recheck")
async def admin_recheck_pack(name: str, request: Request):
    """重新检测插件依赖:清探针缓存 → 全量评估(含探针)。

    依赖满足且该 pack 处于启用态时顺触发热装配(把之前因依赖缺失
    而没加载进引擎的 pack 现场加载,含挂载其 API)——补配后无需重启。

    三十五审 P2: 热装配段进入 hot_reload_lock——与 toggle 共用
    同一把锁, 两个管理员一个 toggle 一个 recheck 并发时装配
    不交错(此前 recheck 无锁, 是另一条无串行化的热装配入口)。
    """
    pack_state = _pack_or_404(request, name)
    from domains import load_pack_configs
    from services.pack_dependency import clear_probe_cache, evaluate_pack, probe_enabled
    from services.pack_manager import assemble_packs, hot_reload_lock

    _reject_multi_worker(request)
    clear_probe_cache(name)
    cfg = load_pack_configs(pack_names=[name]).get(name) or {}
    dep = evaluate_pack(
        name, cfg, getattr(request.app.state, "settings_store", None),
        use_probe=probe_enabled(),
    )

    result: Dict[str, Any] = {"name": name, "dependency": dep, "reloaded": False}
    if dep["status"] == "ok" and pack_state.is_enabled(name):
        with hot_reload_lock():
            # 三十九审 P2-A: recheck 复用 toggle 的事务编排——
            # finalize 失败同样置 degraded(不静默 503), assemble 内部
            # rollback 失败同样传播 AssemblyRollbackFailedError → degraded
            from services.pack_manager import (
                assemble_packs, finalize_assembly,
                AssemblyRollbackFailedError)
            stage = "PENDING"
            try:
                summary = assemble_packs(
                    request.app.state, sorted(pack_state.enabled_names()), app=request.app
                )
                result["reloaded"] = name in summary["loaded"]
                result["loaded"] = summary["loaded"]
                stage = "ASSEMBLED"
                # recheck 不改启停状态(无 persist), 生命周期/unload 直接 finalize
                finalize_assembly(request.app.state, summary["_tx"])
                stage = "FINALIZED"
            except AssemblyRollbackFailedError as e:
                request.app.state.pack_runtime_degraded = True
                logger.exception(
                    f"recheck 装配失败且内部恢复失败——进程已降级: {name}")
                raise HTTPException(
                    503, f"Rollback FAILED during recheck assembly: {e}. "
                         f"Process is degraded—restart required.")
            except Exception as e:
                if stage == "ASSEMBLED":
                    # finalize 失败: runtime 是新装配(与磁盘一致, recheck
                    # 不写盘)——置 degraded + 如实报错
                    request.app.state.pack_runtime_degraded = True
                    logger.exception(
                        f"recheck finalize 失败——进程已降级: {name}")
                    raise HTTPException(
                        503, f"Finalize failed: {e}. Process is degraded—"
                             f"restart required.")
                logger.exception(f"recheck 后热装配失败: {name}")
                raise HTTPException(503, f"Dependency ok but hot-reload failed: {e}")
        # 只有真触发了热装配才用装配结果刷新——刚才是全量重探测的结论;
        # 探测失败时绝不能用装配缓存的旧 ok 覆盖(那正是故障期,谎报 ok
        # 会让"重新检测"这个诊断手段在最需要时给出相反结论)。
        result["dependency"] = (
            getattr(request.app.state, "pack_dependency_status", None) or {}
        ).get(name, dep)
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
