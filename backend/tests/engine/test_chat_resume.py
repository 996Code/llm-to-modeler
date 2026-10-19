"""D10 历史加载续答——挂起轮判定/续跑端点/防重/落库不重复写 user。"""
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import langgraph  # noqa: F401
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False

pytestmark = pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph 未安装")


@pytest.fixture()
def app_with_store(tmp_path):
    """带真实 ConversationStore 的 app(不跑真 graph——resume 端点的
    graph 状态查询用 MagicMock 注入)。"""
    from services.conversation_store import ConversationStore
    from api.config import router

    store = ConversationStore(str(tmp_path / "conv.db"))
    app = FastAPI()
    app.include_router(router)
    app.state.conversation_store = store
    app.state.graph = MagicMock()
    app.state.settings_store = None
    app.state.asset_client = MagicMock()
    return app, store


def _make_conv(store, with_assistant=True):
    conv = store.create_conversation("tester")
    store.add_message(conv_id=conv["id"], role="user", content="问个问题")
    if with_assistant:
        store.add_message(conv_id=conv["id"], role="assistant", content="答了")
    return conv["id"]


class TestPendingResumeFlag:

    def test_last_message_user_marks_pending(self, app_with_store):
        """最后一条是 user(无 assistant)→ pendingResume=true。"""
        _, store = app_with_store
        cid = _make_conv(store, with_assistant=False)
        assert store.get_conversation(cid, "tester")["pendingResume"] is True

    def test_completed_conversation_not_pending(self, app_with_store):
        """正常问答完整 → pendingResume=false。"""
        _, store = app_with_store
        cid = _make_conv(store, with_assistant=True)
        assert store.get_conversation(cid, "tester")["pendingResume"] is False

    def test_multi_turn_last_complete(self, app_with_store):
        """多轮对话末轮完整 → false;末轮悬空 → true。"""
        _, store = app_with_store
        cid = _make_conv(store)
        assert store.get_conversation(cid, "tester")["pendingResume"] is False
        store.add_message(conv_id=cid, role="user", content="再问一句")
        assert store.get_conversation(cid, "tester")["pendingResume"] is True


class TestResumeEndpoint:

    def test_requires_conversation_id(self, app_with_store):
        app, _ = app_with_store
        c = TestClient(app)
        assert c.post("/api/config/chat/resume", json={"message": ""}).status_code == 422

    def test_unknown_or_foreign_conversation_404(self, app_with_store):
        app, store = app_with_store
        cid = _make_conv(store, with_assistant=False)
        c = TestClient(app)
        # 不存在
        assert c.post("/api/config/chat/resume",
                      json={"message": "", "conversation_id": "nope"}).status_code == 404
        # 非本人(get_conversation 归属过滤)
        r = c.post("/api/config/chat/resume", json={"message": "", "conversation_id": cid},
                   headers={"X-User-Id": "someone-else"})
        assert r.status_code == 404

    def test_completed_conversation_409(self, app_with_store):
        """最后一轮已有回答 → 409 拒绝(无需续答)。"""
        app, store = app_with_store
        cid = _make_conv(store, with_assistant=True)
        c = TestClient(app)
        assert c.post("/api/config/chat/resume",
                      json={"message": "", "conversation_id": cid},
                      headers={"X-User-Id": "tester"}).status_code == 409

    def test_no_pending_checkpoint_409(self, app_with_store):
        """pendingResume=true 但 checkpoint 无未完成 task(图已跑完/不存在)
        → 409 防重(不重跑,防双答)。"""
        app, store = app_with_store
        cid = _make_conv(store, with_assistant=False)
        snap = MagicMock()
        snap.tasks = []                       # 无未完成 task
        app.state.graph.get_state.return_value = snap
        c = TestClient(app)
        assert c.post("/api/config/chat/resume",
                      json={"message": "", "conversation_id": cid},
                      headers={"X-User-Id": "tester"}).status_code == 409

    def test_pending_checkpoint_streams(self, app_with_store, monkeypatch):
        """checkpoint 有未完成 task → 200 SSE,流走 stream_graph(resume_pending)。"""
        app, store = app_with_store
        cid = _make_conv(store, with_assistant=False)
        snap = MagicMock()
        snap.tasks = [MagicMock()]            # 有未完成 task
        app.state.graph.get_state.return_value = snap

        captured = {}

        async def fake_stream(**kwargs):
            captured.update(kwargs)
            yield "event: done\ndata: {}\n\n"

        from api import config as config_mod
        import engine.stream as stream_mod
        monkeypatch.setattr(config_mod, "stream_graph", fake_stream, raising=False)
        monkeypatch.setattr(stream_mod, "stream_graph", fake_stream)

        c = TestClient(app)
        r = c.post("/api/config/chat/resume",
                   json={"message": "", "conversation_id": cid},
                   headers={"X-User-Id": "tester"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert captured.get("resume_pending") is True
        assert captured.get("conversation_id") == cid
        assert captured.get("user_id") == "tester"   # 会话归属人(作者一致)


class TestResumePersistence:
    """落库辅助只写 assistant——user 消息在请求到达时已即时落库
    (流式中断不丢问句),收尾重复写 user 会让历史同一句问两遍。"""

    def test_save_skips_user_when_resume(self, app_with_store):
        from engine.stream import _save_conversation, _save_result_conversation
        _, store = app_with_store
        cid = _make_conv(store, with_assistant=False)
        # 简单版落库:只补 assistant,不写 user(_make_conv 已预置 1 条)
        _save_conversation(store, cid, "tester", "问个问题", "续答完成")
        msgs = store.get_messages(cid)
        assert [m["role"] for m in msgs].count("user") == 1   # 不重复写 user
        assert any(m["role"] == "assistant" for m in msgs)

        cid2 = _make_conv(store, with_assistant=False)
        _save_result_conversation(store, cid2, "tester", "问个问题",
                                  {"summary": "结果"}, None)
        msgs2 = store.get_messages(cid2)
        assert [m["role"] for m in msgs2].count("user") == 1   # 不重复写 user
        assert any(m["role"] == "assistant" for m in msgs2)
