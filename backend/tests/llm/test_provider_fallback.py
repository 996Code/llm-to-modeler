"""C8 模型供应商主备切换(设计 8.6)——存储语义+切换语义+留痕。

覆盖:
  - llm_providers 存储:直存(无 CAS)/校验/留空 key 沿用旧值/启停解析
  - LLMClient 端点链:DB 配置覆盖 env;连接类失败同参数切备用;
    身份(401)/限流(429)/未知错误不切;provider+成本进调用日志
  - 管理 API:GET 掩码(has_key)/PUT 直存与校验

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
    }, "test")


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

def test_store_direct_save_and_keep_key():
    """直存(无 CAS):revision 自增;留空 key 沿用旧值。"""
    _save_default()
    assert store.current_revision() == 1
    assert store.resolve_active()["primary"]["model"] == "m-primary"
    store.save_providers({"primary": {"base_url": "http://a/v1",
                                      "model": "m"}}, "x")
    assert store.current_revision() == 2
    assert store.resolve_active()["primary"]["model"] == "m"
    with store._conn() as conn:
        row = conn.execute(
            "SELECT updated_by FROM llm_providers WHERE scope = ?",
            (store.SCOPE,)).fetchone()
    assert row["updated_by"] == "x"
    # 留空 key 沿用旧值
    store.save_providers({
        "primary": {"base_url": "http://primary2/v1", "api_key": "",
                    "model": "m2", "_existing_key": "sk-p"},
        "fallback": {"base_url": "http://f2/v1", "api_key": "",
                     "model": "mf", "_existing_key": "sk-f"}}, "t")
    cfg = store.load_providers()
    assert cfg["primary"]["api_key"] == "sk-p"
    assert cfg["fallback"]["api_key"] == "sk-f"
    assert cfg["_revision"] == 3


def test_store_rejects_revision_argument():
    content = {"primary": {"base_url": "http://a/v1", "model": "m"}}
    with pytest.raises(TypeError):
        store.save_providers(content, "test", 0)
    assert store.current_revision() == 0


def test_store_validation_and_disabled():
    with pytest.raises(store.ProviderConfigError):
        store.save_providers({"primary": {"model": "m"}}, "x")
    with pytest.raises(store.ProviderConfigError):
        store.save_providers({"primary": {"base_url": "http://a/v1"}}, "x")
    store.save_providers({
        "primary": {"base_url": "http://a/v1", "model": "m", "enabled": True},
        "fallback": {"base_url": "http://b/v1", "model": "m",
                     "enabled": False}}, "t")
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


def test_default_client_ignores_endpoint_env_and_builds_nothing(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://env-default/v1")
    monkeypatch.setenv("LLM_API_KEY", "env-secret")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setattr("llm.client.OpenAI",
                        lambda **kw: pytest.fail("eager SDK construction"))
    client = LLMClient()
    assert client.config.base_url == ""
    assert client.config.api_key == ""
    assert client.config.model == ""
    assert client.client is None
    with pytest.raises(RuntimeError, match="模型供应商"):
        client.chat([{"role": "user", "content": "q"}])


@pytest.mark.parametrize("role", ["primary", "fallback"])
def test_missing_provider_model_never_uses_explicit_config(monkeypatch, role):
    from llm.client import LLMConfig
    active = {"primary": {"base_url": "http://primary/v1", "model": "primary"}}
    active[role] = {"base_url": f"http://{role}/v1", "model": ""}
    monkeypatch.setattr(store, "resolve_active", lambda: active)
    client = LLMClient(LLMConfig(base_url="http://explicit/v1", model="explicit"))
    def client_for(cfg):
        if not cfg.model:
            pytest.fail("invalid provider reached SDK")
        def create(**kw):
            raise _status_err(502)
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(client, "_client_for", client_for)
    with pytest.raises(RuntimeError, match="model"):
        client.chat([{"role": "user", "content": "q"}])


def test_explicit_client_contract_and_endpoint_reuse():
    from llm.client import LLMConfig
    cfg = LLMConfig(base_url="http://explicit/v1", api_key="key", model="explicit")
    client = LLMClient(cfg)
    try:
        assert client.config is cfg
        assert str(client.client.base_url) == "http://explicit/v1/"
        assert client._client_for(cfg) is client.client
    finally:
        client.close()


def test_fallback_uses_its_own_model(monkeypatch):
    _save_default()
    seen = []
    client = LLMClient()

    def client_for(cfg):
        def create(**kw):
            seen.append(dict(kw))
            if cfg.model == "m-primary":
                raise _status_err(502)
            return _ok_response()
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(client, "_client_for", client_for)
    assert client.chat([{"role": "user", "content": "q"}]) == "hi"
    assert [kw["model"] for kw in seen] == ["m-primary", "m-fallback"]


def test_chat_model_tracks_active_provider_without_sdk(monkeypatch):
    monkeypatch.setattr("llm.client.OpenAI",
                        lambda **kw: pytest.fail("model inspection created SDK"))
    client = LLMClient()
    assert client.chat_model == ""
    _save_default()
    assert client.chat_model == "m-primary"
    store.save_providers({"primary": {
        "base_url": "http://changed/v1", "model": "changed-model"}}, "test")
    assert client.chat_model == "changed-model"
    store.save_providers({"primary": {
        "base_url": "http://changed/v1", "model": "changed-model",
        "enabled": False}}, "test")
    assert client.chat_model == ""


def test_cached_chat_follows_db_updates_and_disable(monkeypatch):
    monkeypatch.setenv("LLM_CACHE_ENABLED", "1")
    _save_default()
    client = LLMClient()
    seen = []

    def client_for(cfg):
        def create(**kw):
            seen.append((cfg.base_url, kw["model"]))
            return _ok_response(f"{cfg.base_url}:{kw['model']}")
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(client, "_client_for", client_for)
    messages = [{"role": "user", "content": "q"}]
    first = client.chat(messages)
    assert client.chat(messages) == first
    assert len(seen) == 1
    store.save_providers({"primary": {
        "base_url": "http://primary.test/v1", "model": "changed-model"}}, "test")
    assert client.chat(messages) != first
    store.save_providers({"primary": {
        "base_url": "http://changed.test/v1", "model": "changed-model"}}, "test")
    assert client.chat(messages) == "http://changed.test/v1:changed-model"
    store.save_providers({"primary": {
        "base_url": "http://changed.test/v1", "model": "changed-model",
        "enabled": False}}, "test")
    with pytest.raises(RuntimeError, match="模型供应商"):
        client.chat(messages)
    assert len(seen) == 3


def test_lazy_endpoint_clients_reused_and_closed(monkeypatch):
    _save_default()
    client = LLMClient()
    assert client.client is None
    chain = client._resolve_endpoints()
    primary = client._client_for(chain[0][1])
    fallback = client._client_for(chain[1][1])
    assert client._client_for(chain[0][1]) is primary
    assert client._client_for(chain[1][1]) is fallback
    client.close()
    assert primary.is_closed()
    assert fallback.is_closed()


def test_fallback_does_not_inherit_primary_thinking(monkeypatch):
    store.save_providers({
        "primary": {"base_url": "http://primary/v1", "model": "qwen3-primary"},
        "fallback": {"base_url": "http://fallback/v1", "model": "deepseek-chat"},
    }, "test")
    client = LLMClient()
    seen = []

    def client_for(cfg):
        def create(**kw):
            seen.append(dict(kw))
            if cfg.model == "qwen3-primary":
                raise _status_err(502)
            return _ok_response()
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(client, "_client_for", client_for)
    assert client.chat([{"role": "user", "content": "q"}]) == "hi"
    assert seen[0]["extra_body"] == {"enable_thinking": False}
    assert "extra_body" not in seen[1]


def test_max_tokens_clamped_on_gateway_400(monkeypatch):
    """400 且文案带 max_tokens 合法区间 → 截到上限同端点重试成功。

    背景(3.110 实证):构造器默认 max_tokens=200000,996code/qwen3.7
    上限 131072——首个真实调用 400,此前直接冒泡成 dry-run 500。
    """
    _save_default()
    seen: list = []

    def _err_400(**kw):
        seen.append(kw.get("max_tokens"))
        raise _status_err(
            400, "Error code: 400 - Range of max_tokens should be [1, 131072]")

    def _ok(**kw):
        seen.append(kw.get("max_tokens"))
        return _ok_response()

    queue = [_err_400, _ok]
    # 显式 max_tokens=200000:测试环境 .env 可能设 LLM_MAX_TOKENS,
    # 受污染时首请求即未超限,clamp 分支走不到
    from llm.client import LLMConfig
    client = LLMClient(
        config=LLMConfig(base_url="http://x/v1", api_key="k",
                         max_tokens=200000),
        conversation_store=_FakeStore())

    def fake_client_for(cfg):
        class _C:
            def __init__(self):
                self.chat = SimpleNamespace()
                self.chat.completions = SimpleNamespace()
                self.chat.completions.create = lambda **kw: queue.pop(0)(**kw)
        return _C()

    monkeypatch.setattr(client, "_client_for", fake_client_for)
    assert client.chat([{"role": "user", "content": "q"}]) == "hi"
    assert len(seen) == 2 and seen[0] > 131072 and seen[1] == 131072


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


def test_api_put_direct_and_keep_key(api):
    """PUT 直存(无 CAS);留空 key 沿用旧值(GET 仍显示 has_key)。"""
    _save_default()
    body = {
        "primary": {"base_url": "http://new/v1", "api_key": "",
                    "model": "m", "enabled": True},
    }
    ok = api.put("/api/admin/llm-providers", json=body)
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2
    cfg = store.load_providers()
    assert cfg["primary"]["api_key"] == "sk-p"
    assert cfg["primary"]["base_url"] == "http://new/v1"
    body["primary"]["model"] = "new-model"
    again = api.put("/api/admin/llm-providers", json=body)
    assert again.status_code == 200
    assert again.json()["revision"] == 3
    assert store.resolve_active()["primary"]["model"] == "new-model"


def _provider_http(monkeypatch, primary_fails=False):
    """Use the real OpenAI SDK; replace only its HTTP transport."""
    from openai import OpenAI
    seen = []

    def handle(request):
        import json
        seen.append((request.url.host, request.url.path))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        assert json.loads(request.content)["model"] == "m-primary"
        if primary_fails:
            return httpx.Response(502, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "m-primary", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "primary-ok"},
                "finish_reason": "stop"}]})

    def build(**kw):
        kw["http_client"].close()
        kw["http_client"] = httpx.Client(transport=httpx.MockTransport(handle))
        kw["max_retries"] = 0
        return OpenAI(**kw)

    monkeypatch.setattr("llm.client.OpenAI", build)
    return seen


def test_put_rejects_enabled_empty_fallback_preserving_working_chat(api, monkeypatch):
    _save_default()
    before = store.load_providers()
    response = api.put("/api/admin/llm-providers", json={
        "primary": {"base_url": "http://primary.test/v1", "model": "m-primary"},
        "fallback": {"base_url": "http://fallback.test/v1", "model": "  ",
                     "enabled": True}})
    assert response.status_code == 400
    assert "fallback" in response.json()["detail"]
    assert store.load_providers() == before
    seen = _provider_http(monkeypatch)
    client = LLMClient()
    try:
        assert client.chat([{"role": "user", "content": "q"}]) == "primary-ok"
        assert [host for host, _ in seen] == ["primary.test"]
    finally:
        client.close()


@pytest.mark.parametrize("legacy,primary_fails", [(False, False), (True, False), (True, True)])
def test_saved_empty_fallback_does_not_block_primary(api, monkeypatch, legacy, primary_fails):
    import asyncio
    import json
    payload = {
        "primary": {"base_url": "http://primary.test/v1", "model": "m-primary"},
        "fallback": {"base_url": "http://fallback.test/v1", "model": "",
                     "enabled": False}}
    response = api.put("/api/admin/llm-providers", json=payload)
    assert response.status_code == 200
    saved = store.load_providers()
    assert saved["fallback"]["model"] == ""
    if legacy:
        # Simulate a row accepted by the old PUT validation, not a resolver mock.
        payload["fallback"]["enabled"] = True
        with store._conn() as conn:
            conn.execute("UPDATE llm_providers SET content=?, revision=revision+1 WHERE scope=?",
                         (json.dumps(payload), store.SCOPE))
        store.invalidate_cache()
    seen = _provider_http(monkeypatch, primary_fails)
    client = LLMClient()
    try:
        assert client.chat_model == "m-primary"
        if primary_fails:
            with pytest.raises(RuntimeError, match="fallback.*model"):
                client.chat([{"role": "user", "content": "q"}])
        else:
            assert client.chat([{"role": "user", "content": "q"}]) == "primary-ok"
        assert [host for host, _ in seen] == ["primary.test"]
        async def probe_http(self, url, **kw):
            assert url == "http://primary.test/v1/models"
            return httpx.Response(200)
        monkeypatch.setattr(httpx.AsyncClient, "get", probe_http)
        assert asyncio.run(client.probe()) == ("ok", "model=m-primary · /models HTTP 200")
        if legacy:
            result = api.post("/api/admin/llm-providers/probe")
            assert result.status_code == 200
            primary, fallback = result.json()["results"]
            assert primary["role"] == "primary"
            assert primary["ok"] is not primary_fails
            assert fallback["role"] == "fallback" and fallback["ok"] is False
            assert "model" in fallback["error"]
            assert all(host == "primary.test" for host, _ in seen)
    finally:
        client.close()


def test_api_invalid_save_preserves_config(api):
    _save_default()
    before = store.load_providers()
    resp = api.put("/api/admin/llm-providers", json={
        "primary": {"base_url": "http://new/v1", "model": ""}})
    assert resp.status_code == 400
    assert store.load_providers() == before


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
