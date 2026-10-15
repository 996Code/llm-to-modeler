"""P2#13 平台费用观测 —— 模型单价配置 + call_logs 按 pack/stage/天费用聚合。

不启动完整 main.app,按 test_admin_api 的模式组装最小应用:
真实 admin 路由 + 真实 ConversationStore / PlatformSettingsStore(测试库,
conftest 每测试清表)。call_logs 全程只读观测——种子经 save_call_log 写入,
聚合不改表不加列。
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.admin import router as admin_router
from src.services.conversation_store import ConversationStore

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture()
def client(monkeypatch):
    """组装带 admin 路由的最小应用(store 走 conftest 引导的测试库)。"""
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN_TOKEN)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.conversation_store = ConversationStore()
    with TestClient(app) as c:
        yield c


def _auth():
    return {"X-Admin-Token": ADMIN_TOKEN}


def _llm_log(store: ConversationStore, model: str, stage: str = "route_pack",
             pack: str = "njmind_form", tokens_in: int = 1000, tokens_out: int = 500,
             created_at: str = None) -> str:
    """造一条 LLM 调用日志(与 llm/client.py 落库口径一致:
    request_data 带 model/stage,response_data.usage 带 token 数)。"""
    log_id = store.save_call_log(
        call_type="llm", endpoint="http://llm/chat/completions",
        request_data={"model": model, "messages_count": 3, "stage": stage},
        response_data={"content": "ok", "usage": {
            "prompt_tokens": tokens_in, "completion_tokens": tokens_out}},
        status_code=200, duration_ms=50, pack_name=pack,
    )
    if created_at:
        # 回拨时间戳(模拟历史调用,验证时间窗口);测试侧 UPDATE 不改表结构
        with store._get_conn() as conn:
            conn.execute("UPDATE call_logs SET created_at = ? WHERE id = ?",
                         (created_at, log_id))
    return log_id


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _put_pricing(client, models):
    return client.put("/api/admin/settings/model-pricing",
                      headers=_auth(), json={"models": models})


# ── 单价配置:admin 门 + 读写往返 + 校验 ─────────────────────

def test_pricing_requires_admin_token(client):
    """admin 门:无口令/错口令读写单价 → 401(与 stats 同门)。"""
    assert client.get("/api/admin/settings/model-pricing").status_code == 401
    assert client.get("/api/admin/settings/model-pricing",
                      headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.put("/api/admin/settings/model-pricing",
                      json={"models": {}}).status_code == 401


def test_pricing_roundtrip(client):
    """读写往返:默认空表;PUT 落库 → GET 读回一致;再次 PUT 整体替换。"""
    # 默认:无任何单价
    resp = client.get("/api/admin/settings/model-pricing", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"models": {}}

    # 写入两个模型的单价(元/千 token)
    resp = _put_pricing(client, {
        "qwen-max": {"input_per_1k": 0.002, "output_per_1k": 0.006},
        "kimi": {"input_per_1k": 0.01, "output_per_1k": 0.02},
    })
    assert resp.status_code == 200
    body = client.get("/api/admin/settings/model-pricing", headers=_auth()).json()
    assert body["models"]["qwen-max"] == {"input_per_1k": 0.002, "output_per_1k": 0.006}
    assert body["models"]["kimi"] == {"input_per_1k": 0.01, "output_per_1k": 0.02}

    # PUT = 整体替换:只提交 kimi → qwen-max 被移除
    resp = _put_pricing(client, {"kimi": {"input_per_1k": 0.03, "output_per_1k": 0.04}})
    assert resp.status_code == 200
    body = client.get("/api/admin/settings/model-pricing", headers=_auth()).json()
    assert set(body["models"]) == {"kimi"}
    assert body["models"]["kimi"] == {"input_per_1k": 0.03, "output_per_1k": 0.04}


def test_pricing_validation(client):
    """校验:非数字/负数/缺键/多余键/结构不对 → 422 整体拒绝(不落半截)。"""
    bad_payloads = [
        {"models": {"m": {"input_per_1k": "cheap", "output_per_1k": 0.1}}},   # 非数字
        {"models": {"m": {"input_per_1k": -1, "output_per_1k": 0.1}}},         # 负数
        {"models": {"m": {"input_per_1k": 0.1}}},                              # 缺 output 键
        {"models": {"m": {"input_per_1k": 0.1, "output_per_1k": 0.2,
                           "extra": 1}}},                                      # 多余键
        {"models": {"": {"input_per_1k": 0.1, "output_per_1k": 0.2}}},         # 空模型名
        {"models": ["not-a-dict"]},                                            # 条目非对象
        {"not_models": {}},                                                    # 缺 models 键
    ]
    for payload in bad_payloads:
        resp = client.put("/api/admin/settings/model-pricing",
                          headers=_auth(), json=payload)
        assert resp.status_code == 422, f"应 422 拒绝: {payload}"
    # 拒绝后配置未被污染
    assert client.get("/api/admin/settings/model-pricing",
                      headers=_auth()).json() == {"models": {}}


# ── 费用聚合:正确性 / 无单价 null / 时间窗口 ─────────────────

def test_cost_summary_aggregation(client):
    """聚合正确性:同 pack+stage+天 的调用合并成一条,费用 = Σ(token/1000×单价)。"""
    store = client.app.state.conversation_store
    _put_pricing(client, {"qwen-max": {"input_per_1k": 0.01, "output_per_1k": 0.02}})

    # 同组两条:1000/500 + 500/500 → tokens 1500/1000
    _llm_log(store, "qwen-max", stage="route_pack", pack="njmind_form",
             tokens_in=1000, tokens_out=500)
    _llm_log(store, "qwen-max", stage="route_pack", pack="njmind_form",
             tokens_in=500, tokens_out=500)
    # 不同 stage → 独立分组
    _llm_log(store, "qwen-max", stage="generate", pack="njmind_form",
             tokens_in=2000, tokens_out=1000)
    # 不同 pack → 独立分组(pack_name 为空 = 平台级调用)
    _llm_log(store, "qwen-max", stage="route_pack", pack=None,
             tokens_in=100, tokens_out=100)

    resp = client.get("/api/admin/costs/summary", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    items = {(i["pack"], i["stage"]): i for i in body["items"]}

    # route_pack + njmind_form:1500 in × 0.01/1k + 1000 out × 0.02/1k
    a = items[("njmind_form", "route_pack")]
    assert a["day"] == _today()
    assert a["calls"] == 2
    assert a["tokens_in"] == 1500 and a["tokens_out"] == 1000
    assert abs(a["cost"] - (1500 / 1000 * 0.01 + 1000 / 1000 * 0.02)) < 1e-9

    # generate + njmind_form:2000 in + 1000 out
    b = items[("njmind_form", "generate")]
    assert b["calls"] == 1
    assert abs(b["cost"] - (2000 / 1000 * 0.01 + 1000 / 1000 * 0.02)) < 1e-9

    # 无归属 pack(pack_name NULL → 空串)
    c = items[("", "route_pack")]
    assert c["calls"] == 1 and c["tokens_in"] == 100

    # total = 三组之和
    expected_total = (a["cost"] + b["cost"] + c["cost"])
    assert abs(body["total_cost"] - expected_total) < 1e-9


def test_cost_summary_unpriced_model_null(client):
    """无单价模型:聚合行仍返回但 cost=null,不计入 total_cost;
    混合组(有价+无价模型)cost 只含有价部分。"""
    store = client.app.state.conversation_store
    _put_pricing(client, {"qwen-max": {"input_per_1k": 0.01, "output_per_1k": 0.02}})

    # 完全无单价的模型
    _llm_log(store, "unknown-model", stage="route_pack", pack="njmind_form",
             tokens_in=9999, tokens_out=9999)
    # 同组混入有价模型(同 pack/stage/天)
    _llm_log(store, "qwen-max", stage="route_pack", pack="njmind_form",
             tokens_in=1000, tokens_out=1000)
    # 另一组:只有有价模型
    _llm_log(store, "qwen-max", stage="generate", pack="chatbi",
             tokens_in=1000, tokens_out=1000)

    body = client.get("/api/admin/costs/summary", headers=_auth()).json()
    items = {(i["pack"], i["stage"]): i for i in body["items"]}

    mixed = items[("njmind_form", "route_pack")]
    # 混合组:无价模型的 token 仍计入观测,cost 只含有价部分(1000/1000×0.01 + 1000/1000×0.02)
    assert mixed["calls"] == 2
    assert mixed["tokens_in"] == 9999 + 1000
    assert abs(mixed["cost"] - (1000 / 1000 * 0.01 + 1000 / 1000 * 0.02)) < 1e-9

    priced = items[("chatbi", "generate")]
    assert abs(priced["cost"] - 0.03) < 1e-9

    # total 只含有价费用
    assert abs(body["total_cost"] - (mixed["cost"] + priced["cost"])) < 1e-9

    # 全无单价时:所有 cost=null,total=0,不报错
    _put_pricing(client, {})
    _llm_log(store, "unknown-model", stage="solo", pack="njmind_form")
    body = client.get("/api/admin/costs/summary", headers=_auth()).json()
    assert body["total_cost"] == 0
    assert all(i["cost"] is None for i in body["items"])


def test_cost_summary_window_and_types(client):
    """默认 7 天窗口:8 天前的调用不在默认聚合内;days=30 放行;
    非 llm 调用(upstream)与无 usage 的调用不参与计费口径。"""
    store = client.app.state.conversation_store
    _put_pricing(client, {"qwen-max": {"input_per_1k": 0.01, "output_per_1k": 0.02}})

    old_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    _llm_log(store, "qwen-max", stage="route_pack", pack="njmind_form",
             tokens_in=1000, tokens_out=1000, created_at=old_at)
    # 近期调用
    _llm_log(store, "qwen-max", stage="route_pack", pack="njmind_form",
             tokens_in=500, tokens_out=500)
    # 非 llm 类型:不进聚合
    store.save_call_log("upstream", "/api/validate", {}, {"ok": True},
                        200, 35, pack_name="njmind_form")
    # llm 但无 usage(部分网关不回 token):行仍聚合,tokens 计 0
    store.save_call_log("llm", "http://llm/chat/completions",
                        {"model": "qwen-max", "stage": "route_pack"},
                        {"content": "no usage"}, 200, 20, pack_name="njmind_form")

    # 默认(7 天):只有近期两条(一条有 usage 一条无)
    body = client.get("/api/admin/costs/summary", headers=_auth()).json()
    item = {(i["pack"], i["stage"]): i for i in body["items"]}[("njmind_form", "route_pack")]
    assert item["calls"] == 2
    assert item["tokens_in"] == 500 and item["tokens_out"] == 500
    assert abs(item["cost"] - (500 / 1000 * 0.01 + 500 / 1000 * 0.02)) < 1e-9

    # days=30:8 天前的调用进入窗口(按天分组 → 老调用在独立的 day 行)
    body = client.get("/api/admin/costs/summary", params={"days": 30},
                      headers=_auth()).json()
    group = [i for i in body["items"]
             if (i["pack"], i["stage"]) == ("njmind_form", "route_pack")]
    assert len(group) == 2  # 今天 2 条 + 8 天前 1 条
    assert sum(i["calls"] for i in group) == 3
    assert sum(i["tokens_in"] for i in group) == 1500
    assert sum(i["tokens_out"] for i in group) == 1500
    old_days = {i["day"] for i in group if i["calls"] == 1}
    assert old_days and old_days != {_today()}


def test_cost_summary_requires_admin_token(client):
    assert client.get("/api/admin/costs/summary").status_code == 401
    assert client.get("/api/admin/costs/summary",
                      headers={"X-Admin-Token": "wrong"}).status_code == 401
