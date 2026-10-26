"""metrology 场景配置管理 API 测试(P1 管理页数据面)。

覆盖:首启种子/GET 默认、整档保存 CAS(409 冲突)、校验拒绝(400)、
历史列表/快照、回滚(旧快照生成新 revision)、保存后热生效(缓存
失效,load_scenarios 读到新配置)、执行器经 DB 场景表跑通(改追问
文案 → 追问问题变)。

跑法:backend 目录 .venv/bin/python -m pytest tests/domains/ -q
(conftest 建表+每测试清 metrology_assistant schema)。
"""
import importlib

import pytest


@pytest.fixture()
def client(monkeypatch):
    """管理端 TestClient(ADMIN_TOKEN 清空=放行;conftest 注册同款)。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from domains.metrology_assistant.pack import create_api_router
    app = FastAPI()
    app.include_router(create_api_router(), prefix="/api/packs/metrology_assistant")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_pack(monkeypatch):
    """每测试清 metrology 单例与缓存(表由 conftest 清)。"""
    from domains.metrology_assistant import runtime
    runtime.reset_runtime_cache()
    yield
    runtime.reset_runtime_cache()


def _db():
    from domains.metrology_assistant import runtime
    return runtime.get_pack_db()


def test_get_config_seeds_factory_default(client):
    """无配置行:GET 返回出厂默认(revision 0, factory=True)。"""
    resp = client.get("/api/packs/metrology_assistant/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 0
    assert body["factory"] is True
    assert set(body["content"]["scenarios"]) == {
        "mandatory_check", "progress", "instrument_info"}


def test_save_config_cas_and_conflict(client):
    """整档保存:revision 递增;旧 revision 再存 409。"""
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "请提供单号(改)"
    resp = client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": current["revision"],
        "comment": "改追问"})
    assert resp.status_code == 200
    new_rev = resp.json()["revision"]
    assert new_rev == current["revision"] + 1
    # 旧 revision 再保存 → 409
    resp2 = client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": current["revision"]})
    assert resp2.status_code == 409


def test_save_config_validation_rejects_bad_shape(client):
    """坏结构 400:缺 required/action/kind 枚举。"""
    client.get("/api/packs/metrology_assistant/config")  # 种子化
    bad = {"scenarios": {"x": {"label": "X", "intent": "x"}}}
    resp = client.put("/api/packs/metrology_assistant/config", json={
        "content": bad, "expected_revision": 1})
    assert resp.status_code == 400
    assert "校验失败" in resp.json()["detail"]
    # 空 scenarios 同样拒绝
    resp2 = client.put("/api/packs/metrology_assistant/config", json={
        "content": {"scenarios": {}}, "expected_revision": 1})
    assert resp2.status_code == 400


def test_history_and_rollback(client):
    """历史列表元信息 + 回滚生成新 revision 且内容回到旧档。"""
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    # v1:首次保存(expected_revision=0 → 种子化 revision 1)
    resp1 = client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": current["revision"],
        "comment": "v1"})
    assert resp1.status_code == 200
    # v2:改追问文案(revision 2)
    content["scenarios"]["progress"]["required"]["question"] = "v2 文案"
    resp2 = client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": resp1.json()["revision"],
        "comment": "v2"})
    assert resp2.status_code == 200
    hist = client.get("/api/packs/metrology_assistant/config/history").json()
    revisions = [h["revision"] for h in hist["items"]]
    assert 2 in revisions and 1 in revisions
    entry = client.get("/api/packs/metrology_assistant/config/history/1").json()
    assert "请提供委托单号" in entry["content"]["scenarios"]["progress"][
        "required"]["question"]
    resp = client.post("/api/packs/metrology_assistant/config/rollback",
                       json={"to_revision": 1, "comment": "回滚"})
    assert resp.status_code == 200
    after = client.get("/api/packs/metrology_assistant/config").json()
    assert after["revision"] == 3
    assert "请提供委托单号" in after["content"]["scenarios"]["progress"][
        "required"]["question"]
    # 不存在的 revision → 404
    resp404 = client.post("/api/packs/metrology_assistant/config/rollback",
                          json={"to_revision": 999})
    assert resp404.status_code == 404


def test_save_hot_reloads_runtime_scenarios(client):
    """保存后执行器读到新配置(缓存失效,热生效)。"""
    from domains.metrology_assistant.scenario_config import load_scenarios
    client.get("/api/packs/metrology_assistant/config")  # 种子化
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "热生效文案?"
    client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": current["revision"]})
    table = load_scenarios()
    assert table["progress"]["required"]["question"] == "热生效文案?"
    # AskOption 复原:执行器消费 SDK 类型
    from sdk.tool import AskOption
    assert isinstance(table["mandatory_check"]["required"]["options"][0],
                      AskOption)


def test_executor_uses_db_scenario_table(client, monkeypatch):
    """端到端:管理页改追问文案 → 工具缺槽追问用新文案。"""
    class FakeMem:
        def __init__(self):
            self._d = {}
        def get(self, k, default=None):
            return self._d.get(k, default)
        def set(self, k, v):
            self._d[k] = v
        def pop(self, k, default=None):
            return self._d.pop(k, default)

    class FakeCtx:
        def __init__(self, llm, mem):
            self.llm_client = llm
            self.session_state = mem
            self.conv_id = "conv-cfg"
        def emit(self, event, *args):
            pass

    class FakeLLM:
        def __init__(self):
            self.calls = 0
        def chat_json(self, messages, temperature=0.0, conv_id=None,
                      stage=None):
            self.calls += 1
            return {"scenario": "progress", "instrument": "",
                    "purpose": "", "order_no": ""}

    client.get("/api/packs/metrology_assistant/config")  # 种子化
    current = client.get("/api/packs/metrology_assistant/config").json()
    content = current["content"]
    content["scenarios"]["progress"]["required"]["question"] = "请给出单号(新)"
    client.put("/api/packs/metrology_assistant/config", json={
        "content": content, "expected_revision": current["revision"]})

    from domains.metrology_assistant.tools.assistant import MetrologyAssistantTool
    tool = MetrologyAssistantTool(app_state=object())
    ctx = FakeCtx(FakeLLM(), FakeMem())
    result = tool.execute({"user_input": "查一下进度"}, ctx)
    assert result.ask is not None
    assert "请给出单号(新)" in result.ask.questions[0].question
