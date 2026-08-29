"""管理端 API 集成测试 —— 鉴权 / 会话审计 / 调用日志 / 插件热启停。

不启动完整 main.app(那需要 LLM/上游),而是按端点依赖手动组装一个
最小 FastAPI 应用:真实路由 + 真实 ConversationStore(临时 SQLite)+
真实 PackState + 哑的 llm/asset/conversation 组件(assemble_packs 只做
注入,不会真正调用它们)。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.admin import router as admin_router
from src.api.conversations import router as conversations_router
from src.services.conversation_store import ConversationStore
from src.services.pack_state import PackState

ADMIN_TOKEN = "test-admin-token"
PACKS = ["njmind_form", "leave_application"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """组装带 admin + conversations 路由的最小应用。"""
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("PACKS_ENABLED", raising=False)

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(conversations_router)

    store = ConversationStore(str(tmp_path / "conversations.db"))
    app.state.conversation_store = store
    app.state.pack_state = PackState(str(tmp_path / "pack_state.json"), PACKS)
    app.state.pack_tools = {}
    # assemble_packs 只把这几个组件注入 nodes 模块全局,测试里给哑对象即可
    app.state.llm_client = object()
    app.state.asset_client = object()
    app.state.conversation_manager = object()

    with TestClient(app) as c:
        yield c


def _auth_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def _seed(store: ConversationStore):
    """两个用户各建一个会话,各追加一条消息 + 一条调用日志。"""
    conv_a = store.create_conversation("alice", "Alice 的表单")
    conv_b = store.create_conversation("bob", "Bob 的请假")
    store.add_message(conv_a["id"], "user", "帮我做个表单")
    store.add_message(conv_a["id"], "assistant", "好的")
    store.save_call_log("llm", "qwen", {"prompt": "hi"}, {"text": "ok"}, 200, 120, conv_id=conv_a["id"])
    store.save_call_log("upstream", "/api/validate", {}, {"ok": True}, 200, 35, conv_id=conv_b["id"])
    return conv_a, conv_b


# ── 鉴权 ──────────────────────────────────────────────────────

def _minimal_admin_app(tmp_path, monkeypatch, token: str):
    """组装最小应用(带真实 store/pack_state,哑的引擎组件)。"""
    monkeypatch.setenv("ADMIN_TOKEN", token)
    from src.services.pack_state import PackState

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(conversations_router)
    app.state.conversation_store = ConversationStore(str(tmp_path / f"admin-{token or 'open'}.db"))
    app.state.pack_state = PackState(str(tmp_path / f"pack-{token or 'open'}.json"), PACKS)
    app.state.pack_tools = {}
    app.state.llm_client = object()
    app.state.asset_client = object()
    app.state.conversation_manager = object()
    return app


def test_admin_open_without_token(tmp_path, monkeypatch):
    """ADMIN_TOKEN 未配置 → 开放模式:无口令直接访问(内网部署取舍)。"""
    app = _minimal_admin_app(tmp_path, monkeypatch, token="")
    with TestClient(app) as c:
        # 无任何口令头 → 200(不再 503/401)
        stats = c.get("/api/admin/stats")
        assert stats.status_code == 200
        assert stats.json()["authMode"] == "open"  # 开放模式自我标识
        # admin 用户名在开放模式下即有跨用户视角(与管理端同模式)
        c.post("/api/conversations", json={"title": "x"}, headers={"X-User-Id": "alice"})
        body = c.get("/api/conversations", headers={"X-User-Id": "admin"}).json()
        assert len(body) == 1 and body[0]["userId"] == "alice"


def test_admin_rejects_wrong_token(client):
    assert client.get("/api/admin/stats").status_code == 401
    assert client.get("/api/admin/stats", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_accepts_valid_token(client):
    resp = client.get("/api/admin/stats", headers=_auth_headers())
    assert resp.status_code == 200
    assert "conversations" in resp.json()
    assert resp.json()["packs"]["discovered"] == 2
    assert resp.json()["authMode"] == "token"  # 口令模式自我标识(前端据此显示"退出")


# ── 会话管理 ──────────────────────────────────────────────────

def test_admin_lists_all_users_conversations(client):
    store = client.app.state.conversation_store
    _seed(store)
    resp = client.get("/api/admin/conversations", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {i["userId"] for i in body["items"]} == {"alice", "bob"}
    # messageCount 子查询生效
    by_user = {i["userId"]: i for i in body["items"]}
    assert by_user["alice"]["messageCount"] == 2
    assert by_user["bob"]["messageCount"] == 0


def test_admin_conversation_filters_and_pagination(client):
    store = client.app.state.conversation_store
    _seed(store)
    # userId 过滤
    body = client.get("/api/admin/conversations", params={"userId": "alice"}, headers=_auth_headers()).json()
    assert body["total"] == 1 and body["items"][0]["userId"] == "alice"
    # 标题模糊
    body = client.get("/api/admin/conversations", params={"q": "请假"}, headers=_auth_headers()).json()
    assert body["total"] == 1 and body["items"][0]["userId"] == "bob"
    # 分页
    body = client.get("/api/admin/conversations", params={"limit": 1, "offset": 1}, headers=_auth_headers()).json()
    assert len(body["items"]) == 1 and body["total"] == 2


def test_admin_get_and_delete_any_user_conversation(client):
    store = client.app.state.conversation_store
    conv_a, _ = _seed(store)
    detail = client.get(f"/api/admin/conversations/{conv_a['id']}", headers=_auth_headers())
    assert detail.status_code == 200
    assert detail.json()["userId"] == "alice"
    assert len(detail.json()["messages"]) == 2

    del_resp = client.delete(f"/api/admin/conversations/{conv_a['id']}", headers=_auth_headers())
    assert del_resp.status_code == 200
    assert client.get(f"/api/admin/conversations/{conv_a['id']}", headers=_auth_headers()).status_code == 404


# ── 调用日志 ──────────────────────────────────────────────────

def test_admin_call_logs_filter_and_pagination(client):
    store = client.app.state.conversation_store
    conv_a, conv_b = _seed(store)
    body = client.get("/api/admin/call-logs", headers=_auth_headers()).json()
    assert body["total"] == 2
    # callType 过滤
    body = client.get("/api/admin/call-logs", params={"callType": "llm"}, headers=_auth_headers()).json()
    assert body["total"] == 1 and body["items"][0]["endpoint"] == "qwen"
    # convId 过滤 + 反序列化生效
    body = client.get("/api/admin/call-logs", params={"convId": conv_a["id"]}, headers=_auth_headers()).json()
    assert body["total"] == 1
    assert body["items"][0]["request_data"] == {"prompt": "hi"}
    # 分页
    body = client.get("/api/admin/call-logs", params={"limit": 1, "offset": 0}, headers=_auth_headers()).json()
    assert len(body["items"]) == 1 and body["total"] == 2


# ── 插件热启停 ────────────────────────────────────────────────

def test_admin_pack_toggle_hot_reload(client):
    """禁用 → 状态落盘 + app.state 热替换;启用 → 恢复。"""
    resp = client.get("/api/admin/packs", headers=_auth_headers())
    assert resp.status_code == 200
    names = {i["name"]: i["enabled"] for i in resp.json()["items"]}
    assert names == {"njmind_form": True, "leave_application": True}

    # 禁用演示插件 → 热装配后 pack_tools 只剩 njmind_form 的工具
    resp = client.post("/api/admin/packs/leave_application/disable", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["loaded"] == ["njmind_form"]
    assert set(client.app.state.pack_tools) == {"njmind_form"}
    # 状态已持久化(文件存在且只含 njmind_form)
    import json
    data = json.loads(open(client.app.state.pack_state.state_path, encoding="utf-8").read())
    assert data["enabled"] == ["njmind_form"]
    # meta 视角的 app.state.pack_configs 也被热替换
    assert set(client.app.state.pack_configs) == {"njmind_form"}

    # 重新启用 → 工具恢复
    resp = client.post("/api/admin/packs/leave_application/enable", headers=_auth_headers())
    assert resp.status_code == 200
    assert set(client.app.state.pack_tools) == {"njmind_form", "leave_application"}


def test_admin_cannot_disable_last_pack(client):
    client.post("/api/admin/packs/leave_application/disable", headers=_auth_headers())
    resp = client.post("/api/admin/packs/njmind_form/disable", headers=_auth_headers())
    assert resp.status_code == 400


def test_admin_pack_unknown_404(client):
    assert client.post("/api/admin/packs/ghost/enable", headers=_auth_headers()).status_code == 404


# ── 链路追踪(trace) ─────────────────────────────────────────

def test_admin_conversation_trace(client):
    """trace:事件流+调用日志合并时间线,按轮分组,聚合耗时。

    种子按真实落库时序:引擎在整轮结束时才写 user/assistant 事件,
    调用日志的时间戳在两者之前(stream.py _save_conversation 的行为)。
    """
    store = client.app.state.conversation_store
    conv_a = store.create_conversation("alice", "Alice 的表单")
    cid = conv_a["id"]
    # 轮中:两次 LLM 调用(一次带 stage 标签)
    store.save_call_log(
        "llm", "http://llm/chat/completions",
        {"messages_count": 3, "stage": "route_pack"}, {"content": '{"pack": "njmind_form"}'},
        200, 120, conv_id=cid,
    )
    store.save_call_log("llm", "http://llm/chat/completions",
                        {"messages_count": 5}, {"content": "..."}, 200, 45, conv_id=cid)
    # 轮末:user + assistant 一起落库
    store.add_message(cid, "user", "帮我做个表单")
    store.add_message(cid, "assistant", "好的")

    resp = client.get(f"/api/admin/conversations/{cid}/trace", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()

    s = body["summary"]
    assert s["events"] == 3          # created checkpoint + user + assistant
    assert s["turns"] == 1
    assert s["llmCalls"] == 2
    assert s["llmMs"] == 120 + 45
    assert s["firstAt"] and s["lastAt"]

    # 分轮:全部活动归入同一轮(user 消息在展示序首位,assistant 收尾)
    assert len(body["turns"]) == 1
    t = body["turns"][0]
    assert t["userContent"] == "帮我做个表单"
    kinds = [(i["type"], i.get("kind") or i.get("callType")) for i in t["items"]]
    assert ("event", "user") == tuple(kinds[0])          # user 重排到首位
    assert ("event", "assistant") == tuple(kinds[-1])    # assistant 收尾
    assert ("event", "checkpoint") in kinds              # 创建快照在同轮
    call_items = [i for i in t["items"] if i["type"] == "call"]
    assert len(call_items) == 2
    assert {i["durationMs"] for i in call_items} == {120, 45}
    stages = {i["stage"] for i in call_items}
    assert "route_pack" in stages and None in stages
    assert t["llmCount"] == 2 and t["llmMs"] == 165
    assert t["wallMs"] >= 0


def test_admin_conversation_trace_404(client):
    assert client.get("/api/admin/conversations/no-such/trace", headers=_auth_headers()).status_code == 404


def test_admin_trace_double_assistant_merges_into_same_turn(client):
    """真实场景回归:工具轮会落两条 assistant(summary + 制品快照,紧邻),
    第二条必须并入同一轮,而不是切出墙钟 0ms 的空"下一轮"。"""
    store = client.app.state.conversation_store
    conv = store.create_conversation("alice", "表单轮")
    cid = conv["id"]
    store.save_call_log("llm", "http://llm/c", {"messages_count": 2},
                        {"content": "{}"}, 200, 30, conv_id=cid)
    store.add_message(cid, "user", "做个表单")
    store.add_message(cid, "assistant", "已生成表单")          # 第一条:summary
    store.add_message(cid, "assistant", "", config_snapshot={"a": 1})  # 第二条:制品快照

    body = client.get(f"/api/admin/conversations/{cid}/trace", headers=_auth_headers()).json()
    assert body["summary"]["turns"] == 1
    assert len(body["turns"]) == 1                     # 没有假"第 2 轮"
    t = body["turns"][0]
    assistants = [i for i in t["items"] if i["type"] == "event" and i["kind"] == "assistant"]
    assert len(assistants) == 2                        # 两条都在同一轮
    assert t["llmCount"] == 1 and t["userContent"] == "做个表单"


# ── 链路追踪打点(trace 事件体系) ─────────────────────────────

def test_tool_context_trace_writes_event(tmp_path):
    """ToolContext.trace() 写入 kind=trace 事件;无会话上下文时静默跳过。"""
    from src.engine.conversation import ConversationManager
    from src.sdk.tool import ToolContext

    store = ConversationStore(str(tmp_path / "t.db"))
    cm = ConversationManager(store=store)
    conv = store.create_conversation("alice", "t")

    ctx = ToolContext(
        llm_client=None, asset_client=None, conversation=cm,
        emit=lambda *a, **k: None, conv_id=conv["id"],
    )
    ctx.trace("create_form.fetch_template", "拉取模板", status="ok",
              duration_ms=35, detail={"template": "simple_form"})

    events = store.load_events(conv["id"], kinds=["trace"])
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["stage"] == "create_form.fetch_template"
    assert p["title"] == "拉取模板" and p["status"] == "ok"
    assert p["duration_ms"] == 35 and p["detail"] == {"template": "simple_form"}

    # trace 不进消息视图(重放忽略)
    view = cm.load(conv["id"])
    assert view["messages"] == []

    # 无 conv_id(如 MCP 单轮)静默跳过,不抛异常
    ctx2 = ToolContext(
        llm_client=None, asset_client=None, conversation=cm,
        emit=lambda *a, **k: None,
    )
    ctx2.trace("x.step")  # 不应报错
    assert len(store.load_events(conv["id"], kinds=["trace"])) == 1


def test_admin_trace_includes_trace_events(client):
    """trace 打点(轮中写入)进入链路时间线并计入 summary。"""
    store = client.app.state.conversation_store
    conv_a = store.create_conversation("alice", "t")
    cid = conv_a["id"]
    store.append_event(cid, "trace", {
        "stage": "intent_route", "title": "意图路由", "status": "ok",
        "duration_ms": None, "detail": {"pack": "njmind_form", "tool": "create_form"},
    })
    store.add_message(cid, "user", "做个表单")
    store.add_message(cid, "assistant", "好的")

    body = client.get(f"/api/admin/conversations/{cid}/trace", headers=_auth_headers()).json()
    assert body["summary"]["traceEvents"] == 1
    assert len(body["turns"]) == 1
    t = body["turns"][0]
    trace_items = [i for i in t["items"] if i["type"] == "event" and i["kind"] == "trace"]
    assert len(trace_items) == 1
    assert trace_items[0]["payload"]["stage"] == "intent_route"


def test_admin_trace_renders_compression_events(client):
    """压缩事件(compacted/compact_trace)进入链路时间线的系统事件分支。

    背景:CompressionSidechain 尚未在主流装配(见 compression.py 模块注释),
    本测试锁定"一旦压缩事件产生,链路视图能正确承接"的渲染契约。
    """
    store = client.app.state.conversation_store
    conv = store.create_conversation("alice", "压缩渲染")
    cid = conv["id"]
    store.add_message(cid, "user", "第一轮")
    store.add_message(cid, "assistant", "回复一")
    store.append_event(cid, "compacted", {"summary": "用户问了智能家居话题", "tokens_before": 4200})
    store.append_event(cid, "compact_trace", {"tokens_before": 4200, "tokens_after": 60, "degraded": False})
    store.add_message(cid, "user", "第二轮")
    store.add_message(cid, "assistant", "回复二")

    body = client.get(f"/api/admin/conversations/{cid}/trace", headers=_auth_headers()).json()
    assert body["summary"]["turns"] == 2
    kinds = [i["kind"] for t in body["turns"] for i in t["items"]
             if i["type"] == "event" and i["kind"] in ("compacted", "compact_trace")]
    assert kinds == ["compacted", "compact_trace"]
    # 压缩发生在"第 1 轮回复后、第 2 轮前"——归入第 2 轮的活动段
    # (assistant 闭轮算法:压缩正是为下一轮的上下文服务的)
    t2_kinds = [i["kind"] for i in body["turns"][1]["items"]]
    assert "compacted" in t2_kinds and "compact_trace" in t2_kinds


# ── user_id=admin 越权收紧(conversations.py) ─────────────────

def test_admin_username_alone_no_longer_bypasses(client):
    """只报 admin 用户名、无管理口令 → 只能看到自己的会话(不再是全站)。"""
    store = client.app.state.conversation_store
    _seed(store)
    store.create_conversation("admin", "管理员自己的")

    # 无 token:admin 是普通用户,只看到自己的 1 条
    body = client.get("/api/conversations", headers={"X-User-Id": "admin"}).json()
    assert len(body) == 1

    # 带 token:看到全部 3 条
    body = client.get("/api/conversations", headers={"X-User-Id": "admin", **_auth_headers()}).json()
    assert len(body) == 3


def test_admin_username_with_token_reads_any_conversation(client):
    store = client.app.state.conversation_store
    conv_a, _ = _seed(store)
    # 无 token 的 admin 读别人的会话 → 404(Fail-Closed)
    assert client.get(f"/api/conversations/{conv_a['id']}", headers={"X-User-Id": "admin"}).status_code == 404
    # 带 token → 200
    resp = client.get(f"/api/conversations/{conv_a['id']}", headers={"X-User-Id": "admin", **_auth_headers()})
    assert resp.status_code == 200
