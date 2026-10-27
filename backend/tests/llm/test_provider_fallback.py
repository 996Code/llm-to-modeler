"""C8 模型供应商主备切换(设计 8.6)——存储语义+切换语义+留痕。

覆盖:
  - llm_providers 存储:revision CAS/校验/留空 key 沿用旧值/启停解析
  - LLMClient 端点链:DB 配置覆盖 env;连接类失败同参数切备用;
    身份(401)/限流(429)/未知错误不切;provider+成本进调用日志
  - 管理 API:GET 掩码(has_key)/PUT 409/400/probe

openai 异常构造:httpx.Request/Response 最小替身(openai SDK 的
APIStatusError 需要 response.request)。
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError

from llm.client import LLMClient
from services import llm_providers as store


class _FakeStore:
    def __init__(self):
        self.calls = []

    def save_call_log(self, **kw):
        self.calls.append(kw)
        return "id"


def _resp(status: int) -> httpx.Response:
    req = httpx.Request("POST", "http://x/v1/chat/completions")
    return httpx.Response(status_code=status, request=req)


def _status_err(code: int, msg: str = "err") -> APIStatusError:
    return APIStatusError(msg, response=_resp(code), body=None)


@pytest.fixture(autouse=True)
def _clean():
    store.reset_runtime_cache_for_tests()
    yield
    store.reset_runtime_cache_for_tests()


def _save_default(probe_calls=None):
    """写一份主备配置;probe_calls 收集 create 调用(按端点)。"""
    store.save_providers({
        "primary": {"base_url": "http://primary.test/v1", "api_key": "sk-p",
                    "model": "m-primary", "price_in": 0.01, "price_out": 0.02},
        "fallback": {"base_url": "http://fallback.test/v1", "api_key": "sk-f",
                     "model": "m-fallback"},
    }, 0, "test")


def _client_with_creates(monkeypatch, results_by_url):
    """LLMClient + 按 base_url 注入 create 结果。

    results_by_url: {base_url: 返回值 | 异常}(每端点第一次的结果;
    传 list 则按序消耗)。
    """
    client = LLMClient(config=None, conversation_store=_FakeStore())

    def make_create(base_url):
        queue = results_by_url[base_url]
        if not isinstance(queue, list):
            queue = [queue]

        def _create(**kwargs):
            item = queue.pop(0) if queue else queue
            if isinstance(item, Exception):
                raise item
            return item
        return _create

    def fake_client_for(cfg):
        class _C:
            def __init__(self):
                self.chat = SimpleNamespace()
                self.chat.completions = SimpleNamespace()
                self.chat.completions.create = make_create(cfg.base_url)
        return _C()

    monkeypatch.setattr(client, "_client_for", fake_client_for)
    return client


from types import SimpleNamespace  # noqa: E402


def _ok_response(content="hi", pt=10, ct=5):
    """最小 chat completion 响应替身(只带 chat() 读到的字段)。"""
    msg = SimpleNamespace(content=content, reasoning_content=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct)
    usage.model_dump = lambda: {"prompt_tokens": pt, "completion_tokens": ct}
    return SimpleNamespace(choices=[choice], usage=usage)


# ── 存储 ─────────────────────────────────────────────────────

def test_store_cas_and_keep_key():
    _save_default()
    assert store.current_revision() == 1
    # CAS 冲突
    with pytest.raises(store.ProviderConflictError):
        store.save_providers({"primary": {"base_url": "http://a/v1",
                                          "model": "m"}}, 0, "x")
    # 留空 key 沿用旧值
    store.save_providers({
        "primary": {"base_url": "http://primary2/v1", "api_key": "",
                    "model": "m2", "_existing_key": "sk-p"},
        "fallback": {"base_url": "http://f2/v1", "api_key": "",
                     "model": "mf", "_existing_key": "sk-f"}}, 1, "t")
    cfg = store.load_providers()
    assert cfg["primary"]["api_key"] == "sk-p"
    assert cfg["fallback"]["api_key"] == "sk-f"


def test_store_validation_and_disabled():
    with pytest.raises(store.ProviderConfigError):
        store.save_providers({"primary": {"model": "m"}}, 0, "x")
    with pytest.raises(store.ProviderConfigError):
        store.save_providers({"primary": {"base_url": "http://a/v1"}}, 0, "x")
    store.save_providers({
        "primary": {"base_url": "http://a/v1", "model": "m", "enabled": True},
        "fallback": {"base_url": "http://b/v1", "model": "m",
                     "enabled": False}}, 0, "t")
    active = store.resolve_active()
    assert list(active) == ["primary"]  # 停用的 fallback 不参与


# ── 切换语义 ─────────────────────────────────────────────────

def test_switch_on_connection_error(monkeypatch):
    """主端点连接类失败 → 同参数切备用成功;日志 provider=fallback。"""
    _save_default()
    client = _client_with_creates(monkeypatch, {
        "http://primary.test/v1": APIConnectionError(
            message="conn refused",
            request=httpx.Request("POST", "http://x")),
        "http://fallback.test/v1": _ok_response("from-fallback"),
    })
    out = client.chat([{"role": "user", "content": "q"}])
    assert out == "from-fallback"
    providers = [c["request_data"].get("provider")
                 for c in client._conversation_store.calls]
    assert "primary" in providers and "fallback" in providers


def test_no_switch_on_auth_error(monkeypatch):
    """主端点 401(身份错误) → 不切备用,原样上抛。"""
    _save_default()
    client = _client_with_creates(monkeypatch, {
        "http://primary.test/v1": _status_err(401, "bad key"),
    })
    with pytest.raises(APIStatusError):
        client.chat([{"role": "user", "content": "q"}])
    providers = [c["request_data"].get("provider")
                 for c in client._conversation_store.calls]
    assert providers == ["primary"]  # 只尝试了主端点


def test_no_switch_on_rate_limit(monkeypatch):
    """429 限流不切(换端点不解决问题,走全局退避)。"""
    _save_default()
    client = _client_with_creates(monkeypatch, {
        "http://primary.test/v1": _status_err(429, "rate"),
    })
    with pytest.raises(APIStatusError):
        client.chat([{"role": "user", "content": "q"}])
    providers = [c["request_data"].get("provider")
                 for c in client._conversation_store.calls]
    assert providers == ["primary"]


def test_switch_on_5xx(monkeypatch):
    """主端点 5xx(服务端故障) → 切备用。"""
    _save_default()
    client = _client_with_creates(monkeypatch, {
        "http://primary.test/v1": _status_err(502, "bad gw"),
        "http://fallback.test/v1": _ok_response("ok"),
    })
    assert client.chat([{"role": "user", "content": "q"}]) == "ok"


def test_cost_recorded_from_price(monkeypatch):
    """单价配置时成功日志记成本(usage×单价/1000)。"""
    _save_default()  # primary price_in=0.01 price_out=0.02
    client = _client_with_creates(monkeypatch, {
        "http://primary.test/v1": _ok_response(pt=1000, ct=500),
    })
    client.chat([{"role": "user", "content": "q"}])
    ok_call = [c for c in client._conversation_store.calls
               if c["status_code"] == 200][0]
    assert ok_call["request_data"]["provider"] == "primary"
    # 1000*0.01/1000 + 500*0.02/1000 = 0.01+0.01 = 0.02
    assert abs(ok_call["request_data"]["costYuan"] - 0.02) < 1e-9


def test_unconfigured_chain_empty_and_gate(monkeypatch):
    """未配置供应商 → 端点链为空(不回退 env);chat 前置闸给可行动报错。"""
    monkeypatch.setenv("LLM_BASE_URL", "http://env-default/v1")
    client = LLMClient(config=None, conversation_store=_FakeStore())
    assert client._resolve_endpoints() == []
    with pytest.raises(RuntimeError, match="模型供应商"):
        client.chat([{"role": "user", "content": "q"}])


# ── 管理 API ─────────────────────────────────────────────────

@pytest.fixture()
def api(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from api.llm_providers import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_get_masks_key(api):
    _save_default()
    resp = api.get("/api/admin/llm-providers").json()
    assert resp["configured"] is True
    assert "api_key" not in str(resp["primary"])
    assert resp["primary"]["has_key"] is True


def test_api_put_conflict_and_keep_key(api):
    _save_default()
    body = {
        "primary": {"base_url": "http://new/v1", "api_key": "",
                    "model": "m", "enabled": True},
        "expected_revision": 0,  # 库里已是 1 → 409
    }
    assert api.put("/api/admin/llm-providers", json=body).status_code == 409
    # 正确 revision + 留空 key → 沿用旧值(GET 仍显示 has_key)
    body["expected_revision"] = 1
    ok = api.put("/api/admin/llm-providers", json=body)
    assert ok.status_code == 200
    cfg = store.load_providers()
    assert cfg["primary"]["api_key"] == "sk-p"
    assert cfg["primary"]["base_url"] == "http://new/v1"


# ── PII 脱敏前置(设计 7.3:先于 call_logs 记录)────────────────

def test_redact_pii_high_confidence():
    from llm.client import redact_pii
    out = redact_pii("手机 13812345678 身份证 11010519491231002X 信用码 91330100MA27X8YT4K")
    assert "13812345678" not in out and "138******78" in out
    assert "11010519491231002X" not in out
    assert "91330100MA27X8YT4K" not in out


def test_redact_pii_no_false_positive():
    from llm.client import redact_pii
    # 18位纯数字+X 尾(单号)不脱;普通 11 位起始非 1[3-9] 不脱
    assert redact_pii("单号 12345678901234567X") == "单号 12345678901234567X"
    assert redact_pii("编号 23812345678") == "编号 23812345678"
    # 校验位不合法的 18 位数字串不脱
    assert "98765432100123456X" in redact_pii("号 98765432100123456X")


def test_messages_log_persists_redacted(monkeypatch):
    """落库点:messages 与响应 content 均过脱敏(设计 7.3 落库侧)。"""
    from llm import client as llm_client

    msgs = [{"role": "user", "content": "我的手机 13812345678"}]
    out = llm_client._sanitize_messages_for_log(msgs)
    assert "13812345678" not in out[0]["content"]
    monkeypatch.delenv("LLM_LOG_PII_REDACT", raising=False)
    # 开关关 = 原文(调试)
    monkeypatch.setenv("LLM_LOG_PII_REDACT", "0")
    out2 = llm_client._sanitize_messages_for_log(msgs)
    assert "13812345678" in out2[0]["content"]
