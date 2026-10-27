"""qwen3 系默认关思考模式测试(线上导入卡死根治)。

根因(2026-10-17 99.22 实证):qwen3.8-max 开思考时 KG 抽取类任务
avg 224s / max 889s,非流式 LLM_TIMEOUT=300s 是总时长上限,长生成
全量超时(SDK 重试 2 次共 901s,call_logs 实锤),导入卡死 1h+;
关思考后同款抽取请求 6.0s 完成、输出完好。

口径:对模型名含 "qwen3" 的出站注入 extra_body.enable_thinking=False;
非 qwen3 模型不注入(dashscope 方言,发给其他提供商 400)。
本文件锁死 chat / chat_json 两条直连路径的注入行为。
"""
from unittest.mock import MagicMock

import pytest

from llm.client import LLMClient


@pytest.fixture(autouse=True)
def _providers_state():
    """DB 供应商链状态隔离(本文件种入的行不泄漏到其他测试)。"""
    from services import llm_providers as _providers
    _providers.reset_runtime_cache_for_tests()
    yield
    _providers.reset_runtime_cache_for_tests()


def _bare_client() -> LLMClient:
    """绕过 __init__ 的网络配置,只测参数注入。"""

    class _Cfg:
        base_url = "http://test"
        model = "qwen3.8-max"
        api_key = "k"
        temperature = 0.1
        max_tokens = 64

    client = LLMClient.__new__(LLMClient)
    client.config = _Cfg()
    client._log_full = False
    client._conversation_store = None
    # 端点链只认 DB 供应商:先清再种(同一测试内多次 _bare_client 也幂等),
    # 链解析命中 _client_for 复用的 self.client(即测试 patch 的 MagicMock)
    from services import llm_providers as _providers
    _providers.reset_runtime_cache_for_tests()
    _providers.save_providers({
        "primary": {"base_url": "http://test", "api_key": "k",
                    "model": "qwen3.8-max"}}, 0, "test")
    return client


# ── _thinking_off_kwargs:模型名判定 ───────────────────────────


@pytest.mark.parametrize("name", ["qwen3.8-max", "Qwen3-Max", "qwen3-235b", "qwen3.6-35b"])
def test_qwen3_模型注入关思考(name):
    kw = LLMClient._thinking_off_kwargs(name)
    assert kw == {"extra_body": {"enable_thinking": False}}


@pytest.mark.parametrize("name", ["deepseek-chat", "gpt-4o", "glm-4", "qwen2.5-72b", "", None])
def test_非qwen3_模型不注入(name):
    assert LLMClient._thinking_off_kwargs(name) == {}


# ── chat() 路径:注入透传到 create ─────────────────────────────


def test_chat_qwen3_注入extra_body():
    client = _bare_client()
    client.client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "ok"
    resp.choices[0].finish_reason = "stop"
    client.client.chat.completions.create.return_value = resp

    client.chat([{"role": "user", "content": "hi"}])

    kwargs = client.client.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_chat_非qwen3_无extra_body():
    client = _bare_client()
    client.client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "ok"
    client.client.chat.completions.create.return_value = resp

    # 显式传非 qwen3 模型(模型优先级:显式传参 > DB 角色 > 构造器默认,
    # 仅改 config.model 不再覆盖供应商页配置的端点模型)
    client.chat([{"role": "user", "content": "hi"}], model="deepseek-chat")

    assert "extra_body" not in client.client.chat.completions.create.call_args.kwargs


# ── chat_json() 路径:json_object 直连同注 ─────────────────────


def test_chat_json_qwen3_注入extra_body():
    client = _bare_client()
    client.client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = '{"k": 1}'
    resp.usage = None
    client.client.chat.completions.create.return_value = resp

    result = client.chat_json([{"role": "user", "content": "给JSON"}])

    assert result == {"k": 1}
    kwargs = client.client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {"enable_thinking": False}
