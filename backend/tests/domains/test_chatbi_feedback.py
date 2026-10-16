"""chatbi 反馈闭环(P2#14)——点赞/点踩 → apply_feedback_signals 接线。

覆盖蓝图验收:
  - 开关 feedback_enabled 默认关 → 422(零行为变化: 不调置信度、不落事件);
  - 开时 up/down → 调 apply_feedback_signals(既有语义不动, 只接线),
    对应表/关系置信度可观测变化(真实 PG 语义层读回断言);
  - 无效 feedback 值 → 422; 缺 X-User-Id → 401(用户级, 同 m4 口径);
  - 反馈事件落 conversation events 表(kind=feedback, 不污染消息重放);
  - tables 缺省时从消息制品(current_tables)解析。

跑法: PYTHONPATH=src .venv/bin/python -m pytest
    tests/domains/test_chatbi_feedback.py -q
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.chatbi.models import (
    Column, Model, Relationship, SemanticModelContent,
)


def _content() -> SemanticModelContent:
    """两表 + 一条已知关系(orders→users, confidence 0.6)。

    关系名遵循 "{from}_to_{to}" 约定——apply_feedback_signals 的
    known 表对键从关系名 split("_to_") 推导, 命名不合约定则表对
    永不命中(既有语义, 本测试按约定构造)。
    """
    orders = Model(name="orders", display_name="订单", confidence=0.5, columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
        Column(name="user_id", display_name="用户", data_type="INTEGER"),
    ], relationships=[Relationship(
        name="orders_to_users", target_model="users", join_type="LEFT",
        on="orders.user_id = users.id", type="N:1", source="fk",
        confidence=0.6)])
    users = Model(name="users", display_name="用户", confidence=0.5, columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
    ])
    return SemanticModelContent(models=[orders, users])


@pytest.fixture()
def client(monkeypatch):
    """chatbi router + 测试 pack 库 + 真会话/设置存储(用户级端点全设施)。"""
    from domains.chatbi import api as chatbi_api, runtime
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store, configure_encryption
    from cryptography.fernet import Fernet
    configure_encryption(Fernet.generate_key().decode())
    test_db = PackRelationalDB("chatbi")
    init_store(test_db)
    monkeypatch.setattr(runtime, "get_pack_db", lambda: test_db)
    monkeypatch.setattr(chatbi_api, "get_pack_db", lambda: test_db)

    from services.conversation_store import ConversationStore
    from services.pack_settings import PackSettingsStore
    app = FastAPI()
    app.include_router(chatbi_api.router)
    app.state.llm_client = object()
    app.state.conversation_store = ConversationStore()
    app.state.settings_store = PackSettingsStore()
    with TestClient(app) as c:
        yield c, test_db, app


@pytest.fixture()
def ds_id(client):
    """数据源 + 语义层(orders/users, 关系 confidence=0.6)。"""
    c, db, _ = client
    from domains.chatbi import datasources, semantic
    info = datasources.create_datasource(
        db, "反馈测库", "postgresql", "localhost", 5432, "x", "u", "p")
    semantic.save_content(db, info.id, _content(), source="scan")
    return info.id


@pytest.fixture()
def conv(client, ds_id):
    """u1 的会话 + 一条带数据制品的 assistant 消息(chatbi 查询轮形态)。"""
    c, _, app = client
    store = app.state.conversation_store
    conv_id = store.create_conversation("u1")["id"]
    msg = store.add_message(
        conv_id=conv_id, role="assistant", content="查询完成",
        data_artifact={
            "datasource_id": ds_id,
            "current_tables": ["orders", "users"],
            "sql": "SELECT 1",
        })
    return {"conv_id": conv_id, "msg_id": msg["id"], "store": store}


def _enable(app, on: bool = True):
    from services.pack_settings import PackSettingsStore
    PackSettingsStore().save_values("chatbi", {"feedback_enabled": on})


def _rel_confidence(db, ds_id) -> float:
    from domains.chatbi import semantic
    content = semantic.load_current_content(db, ds_id)
    rels = [r for m in content.models for r in m.relationships]
    return rels[0].confidence


def _model_confidence(db, ds_id, name: str) -> float:
    from domains.chatbi import semantic
    content = semantic.load_current_content(db, ds_id)
    return next(m.confidence for m in content.models if m.name == name)


H = {"X-User-Id": "u1"}


class TestFeedbackGate:
    """开关默认关 = 零行为变化(422, 不动置信度, 不落事件)。"""

    def test_disabled_returns_422(self, client, ds_id, conv):
        c, db, app = client
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        assert r.status_code == 422, r.text
        assert "未启用" in r.json()["detail"]
        # 零行为: 置信度不动
        assert _rel_confidence(db, ds_id) == pytest.approx(0.6)
        # 零行为: 不落反馈事件
        assert conv["store"].load_events(conv["conv_id"], kinds=["feedback"]) == []

    def test_invalid_feedback_value_422(self, client, ds_id, conv):
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "sideways"})
        assert r.status_code == 422, r.text

    def test_missing_user_header_401(self, client, conv):
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        assert r.status_code == 401, r.text

    def test_unknown_conversation_404(self, client, ds_id):
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": "no-such-conv", "feedback": "up",
            "tables": ["orders"]})
        assert r.status_code == 404, r.text

    def test_conversation_of_other_user_404(self, client, ds_id, conv):
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", headers={"X-User-Id": "intruder"}, json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        assert r.status_code == 404, r.text


class TestFeedbackApplies:
    """开时 up/down → apply_feedback_signals 被调 + 置信度可观测变化。"""

    def test_up_calls_apply_feedback_signals_and_boosts(self,
                                                        client, ds_id, conv,
                                                        monkeypatch):
        c, db, app = client
        _enable(app)
        import domains.chatbi.feedback as fb_mod
        from domains.chatbi.graph_infer import apply_feedback_signals as real_fn
        spy_calls = []

        def spy(corrections, praises, existing_relationships):
            spy_calls.append({"corrections": corrections, "praises": praises})
            return real_fn(corrections, praises, existing_relationships)
        monkeypatch.setattr(fb_mod, "apply_feedback_signals", spy)

        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up",
            "tables": ["orders", "users"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert set(body["applied_tables"]) == {"orders", "users"}
        # apply_feedback_signals 被调, praises 含已知表对
        assert len(spy_calls) == 1
        assert ("orders", "users") in spy_calls[0]["praises"]
        assert spy_calls[0]["corrections"] == []
        # 置信度可观测变化: 关系 0.6 → 0.65 (PRAISE_BOOST)
        assert _rel_confidence(db, ds_id) == pytest.approx(0.65)
        # 表级置信度同步微调: 0.5 → 0.55
        assert _model_confidence(db, ds_id, "orders") == pytest.approx(0.55)

    def test_down_lowers_confidence(self, client, ds_id, conv):
        c, db, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "down",
            "tables": ["orders", "users"]})
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        # 关系 0.6 → 0.45 (CORRECTION_PENALTY); 表 0.5 → 0.35
        assert _rel_confidence(db, ds_id) == pytest.approx(0.45)
        assert _model_confidence(db, ds_id, "orders") == pytest.approx(0.35)

    def test_tables_resolved_from_message_artifact(self, client, ds_id, conv):
        """body 不带 tables → 从消息制品 current_tables 解析。"""
        c, db, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        assert r.status_code == 200, r.text
        assert set(r.json()["applied_tables"]) == {"orders", "users"}
        assert _rel_confidence(db, ds_id) == pytest.approx(0.65)

    def test_unknown_tables_rejected(self, client, ds_id, conv):
        """表不在本消息查询结果表集 → 422 拒绝(安全审查 I2:body.tables
        限定制品表集子集——放行任意表名等于允许反馈者全局压低/抬高
        与本次查询无关的表,影响该源所有用户的 JOIN 路径排序)。"""
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up",
            "tables": ["no_such_table"]})
        assert r.status_code == 422
        assert "不在本消息的查询结果表集" in r.json()["detail"]

    def test_feedback_idempotent_per_user(self, client, ds_id, conv):
        """per-user 幂等(防刷):同用户同消息同方向二次反馈 → success 但
        applied_tables 空,置信度不叠加(连续点踩 N 次只生效一次)。
        幂等键含 message_id——缺省(流式新消息无 id)不参与幂等,
        见 test_feedback_no_message_id_not_dedup。"""
        c, db, app = client
        _enable(app)
        body = {"conversation_id": conv["conv_id"], "feedback": "down",
                "message_id": conv["msg_id"]}
        r1 = c.post("/feedback", headers=H, json=body)
        assert r1.status_code == 200 and r1.json()["applied_tables"]
        after_first = _rel_confidence(db, ds_id)
        r2 = c.post("/feedback", headers=H, json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"success": True, "applied_tables": [], "deduplicated": True}
        assert _rel_confidence(db, ds_id) == pytest.approx(after_first)  # 无叠加

    def test_feedback_no_message_id_not_dedup(self, client, ds_id, conv):
        """message_id 缺省(流式新消息前端无 id)不做幂等去重——
        (user, None, direction) 空键会误吞同会话不同消息的同向反馈。"""
        c, _, app = client
        _enable(app)
        # 会话里再补一轮不同消息的数据制品(模拟另一条流式查询轮)
        conv["store"].add_message(
            conv_id=conv["conv_id"], role="assistant", content="又一轮查询",
            data_artifact={
                "datasource_id": ds_id,
                "current_tables": ["orders"],
                "sql": "SELECT 2",
            })
        r1 = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        r2 = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "up"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert not r1.json().get("deduplicated")
        assert r2.json()["applied_tables"], \
            "空 message_id 被幂等键误吞(同会话不同消息的同向反馈丢失)"

    def test_feedback_event_persisted(self, client, ds_id, conv):
        """反馈事件落 events 表(kind=feedback, 不进消息重放)。"""
        c, _, app = client
        _enable(app)
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv["conv_id"], "feedback": "down",
            "comment": "JOIN 路径不对"})
        assert r.status_code == 200, r.text
        events = conv["store"].load_events(conv["conv_id"], kinds=["feedback"])
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["feedback"] == "down"
        assert payload["comment"] == "JOIN 路径不对"
        assert set(payload["tables"]) == {"orders", "users"}
        assert payload["user_id"] == "u1"
        # 不污染消息重放(get_messages 只取 user/assistant)
        kinds = {e["kind"] for e in conv["store"].load_events(conv["conv_id"])}
        assert "feedback" not in {
            k for k in kinds if k in ("user", "assistant", "tool_result")}

    def test_conversation_without_data_message_422(self, client, ds_id):
        """会话存在但无数据制品消息 → 无法定位表, 422。"""
        c, _, app = client
        _enable(app)
        store = app.state.conversation_store
        conv_id = store.create_conversation("u1")["id"]
        store.add_message(conv_id=conv_id, role="assistant", content="闲聊")
        r = c.post("/feedback", headers=H, json={
            "conversation_id": conv_id, "feedback": "up"})
        assert r.status_code == 422, r.text
