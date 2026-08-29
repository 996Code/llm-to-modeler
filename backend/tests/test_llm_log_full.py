"""LLM 调用日志完整入库(LLM_LOG_FULL)的单元测试。

不依赖真实 LLM:mock OpenAI SDK 的 chat.completions.create,
验证 call_logs 里记的是完整 messages(图片转占位符)还是摘要,
以及开关关闭时退回旧截断行为。
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.llm.client import LLMClient, LLMConfig, _sanitize_messages_for_log
from src.services.conversation_store import ConversationStore


# ── 消息清洗 ──────────────────────────────────────────────

def test_sanitize_text_passthrough():
    msgs = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "你好"}]
    assert _sanitize_messages_for_log(msgs) == msgs


def test_sanitize_image_to_placeholder():
    """多模态消息:图片 data URL → 占位符(保留长度);文本部分保留。"""
    data_url = "data:image/png;base64," + "A" * 5000
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "分析这张图"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    out = _sanitize_messages_for_log(msgs)
    url = out[0]["content"][1]["image_url"]["url"]
    # 占位符记整个 data URL 的长度(含 "data:image/png;base64," 前缀)
    assert url == f"<image data omitted: {len(data_url)} chars>"
    assert out[0]["content"][0]["text"] == "分析这张图"
    # 原始消息不被修改(防御性拷贝)
    assert msgs[0]["content"][1]["image_url"]["url"] == data_url


# ── 完整入库开关(mock OpenAI)──────────────────────────────

def _fake_response(content: str):
    """构造最小 OpenAI 响应对象(SimpleNamespace 鸭子类型)。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, reasoning_content=None),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 42}),
    )


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(str(tmp_path / "llm_log.db"))


def _make_client(store, monkeypatch, log_full):
    monkeypatch.setenv("LLM_LOG_FULL", log_full)
    client = LLMClient(config=LLMConfig(base_url="http://x/v1", api_key="k"), conversation_store=store)
    return client


def test_chat_logs_full_prompt_when_enabled(store, monkeypatch):
    client = _make_client(store, monkeypatch, "1")
    messages = [{"role": "user", "content": "做一个请假表单"}]
    with patch.object(client.client.chat.completions, "create", return_value=_fake_response("好的,这是配置...")):
        result = client.chat(messages, conv_id="c1", stage="chat.reply")

    assert result.startswith("好的")
    logs = store.get_call_logs(conv_id="c1")
    assert len(logs) == 1
    req = logs[0]["request_data"]
    # 完整 prompt 入库
    assert req["messages"] == messages
    assert req["stage"] == "chat.reply"
    # 完整响应入库(不截断)
    assert logs[0]["response_data"]["content"] == "好的,这是配置..."


def test_chat_logs_summary_when_disabled(store, monkeypatch):
    client = _make_client(store, monkeypatch, "0")
    messages = [{"role": "user", "content": "x" * 800}]
    long_reply = "r" * 800
    with patch.object(client.client.chat.completions, "create", return_value=_fake_response(long_reply)):
        client.chat(messages, conv_id="c2")

    logs = store.get_call_logs(conv_id="c2")
    req = logs[0]["request_data"]
    # 关闭时:无 messages 字段,只有体积指标;响应截断 500
    assert "messages" not in req
    assert req["messages_chars"] == 800
    assert len(logs[0]["response_data"]["content"]) == 500


def test_chat_json_logs_full_and_failure_carries_prompt(store, monkeypatch):
    client = _make_client(store, monkeypatch, "1")
    messages = [{"role": "user", "content": "输出JSON"}]

    # 成功路径:chat_json 的 guided_messages 全量入库
    with patch.object(client.client.chat.completions, "create",
                      return_value=_fake_response('{"pack": "njmind_form"}')):
        parsed = client.chat_json(messages, conv_id="c3", stage="route_pack")
    assert parsed == {"pack": "njmind_form"}
    req = store.get_call_logs(conv_id="c3")[0]["request_data"]
    assert req["messages"][0]["content"].startswith("输出JSON")
    assert req["stage"] == "route_pack"

    # 失败路径:prompt 也入库(排查失败最需要当时发了什么)
    with patch.object(client.client.chat.completions, "create", side_effect=RuntimeError("boom")):
        with pytest.raises(Exception):
            client.chat(messages, conv_id="c4", stage="route_pack")
    fail = store.get_call_logs(conv_id="c4")[0]
    assert fail["error_message"] == "boom"
    assert fail["request_data"]["messages"] == messages


# ── 会话上下文 thread-local 兜底(call_context)──────────────

def test_call_context_binds_upstream_logs(store):
    """上游调用未显式传 conv_id 时,从线程绑定的会话上下文兜底入链。"""
    from services.call_context import bind_conversation, clear_conversation
    from src.services.upstream_client import UpstreamClient

    up = UpstreamClient(conversation_store=store)
    bind_conversation("conv-upstream")
    try:
        # 模拟插件经 asset_client 触发的上游调用(方法内部即调 _log_call,无 conv_id 参数)
        up._log_call("endpoint=/api/form/get", request_data={"code": "F1"},
                     status_code=200, duration_ms=12)
    finally:
        clear_conversation()

    logs = store.get_call_logs(conv_id="conv-upstream")
    assert len(logs) == 1
    assert logs[0]["call_type"] == "upstream" and logs[0]["endpoint"] == "endpoint=/api/form/get"


def test_call_context_fallback_llm_logs(store, monkeypatch):
    """LLM 调用忘传 conv_id 时同样兜底;清理后不再关联。"""
    from services.call_context import bind_conversation, clear_conversation

    monkeypatch.setenv("LLM_LOG_FULL", "1")
    client = LLMClient(config=LLMConfig(base_url="http://x/v1", api_key="k"), conversation_store=store)

    bind_conversation("conv-llm")
    try:
        client._log_call("http://x/chat", request_data={"messages_count": 1},
                         status_code=200, duration_ms=5)
    finally:
        clear_conversation()

    assert len(store.get_call_logs(conv_id="conv-llm")) == 1

    # 清理后再落一条:无会话归属(conv_id 为 NULL,不再误关联)
    client._log_call("http://x/chat", request_data={"messages_count": 1},
                     status_code=200, duration_ms=5)
    assert len(store.get_call_logs(conv_id="conv-llm")) == 1  # 仍是 1 条
