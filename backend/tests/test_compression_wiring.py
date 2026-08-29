"""上下文压缩闭环的单元测试。

覆盖接线后的四个环节:
  1. load() 重放提取压缩摘要(compacted 事件 → compressed_summary)
  2. build_compressed_history 的 [历史摘要] 前缀注入
  3. compress_async:立即返回 keep-recent,后台写 compacted/compact_trace
  4. 归属校验:conversation_exists 防越权(他人 conv_id 拿不到历史)
"""
import time

from src.engine.compression import CompressionSidechain, build_compressed_history, KEEP_RECENT_TURNS
from src.engine.conversation import ConversationManager
from src.services.conversation_store import ConversationStore


def _mk_msg(role, content):
    return {"role": role, "content": content}


def test_load_extracts_latest_compressed_summary(tmp_path):
    """load() 重放:compacted 事件的 summary 进入 compressed_summary(取最新)。"""
    store = ConversationStore(str(tmp_path / "c.db"))
    cm = ConversationManager(store=store)
    conv = store.create_conversation("u1", "t")
    cid = conv["id"]
    store.add_message(cid, "user", "第一轮")
    store.add_message(cid, "assistant", "回复一")
    store.append_event(cid, "compacted", {"summary": "旧摘要", "tokens_before": 100})
    store.add_message(cid, "user", "第二轮")
    store.append_event(cid, "compacted", {"summary": "新摘要", "tokens_before": 200})

    view = cm.load(cid)
    assert view["compressed_summary"] == "新摘要"  # 最新一条生效
    assert view["last_compacted_idx"] == 3  # 最后压缩点在 3 条消息之后(两轮对话+第二轮 user)


def test_build_compressed_history_with_summary_prefix():
    """摘要前缀注入:有 summary 时 prompt 文本以 [历史摘要] 开头。"""
    hist = [_mk_msg("user", "最近的问题"), _mk_msg("assistant", "最近的回答")]
    with_summary = build_compressed_history(hist, summary="用户在做表单")
    assert with_summary.startswith("[历史摘要] 用户在做表单")
    assert "用户: 最近的问题" in with_summary
    without = build_compressed_history(hist)
    assert not without.startswith("[历史摘要]")


def test_compress_async_writes_events_and_returns_recent(tmp_path):
    """compress_async:同步返回 keep-recent;后台完成压缩后 compacted/compact_trace 落库。"""
    store = ConversationStore(str(tmp_path / "c2.db"))
    cm = ConversationManager(store=store)
    conv = store.create_conversation("u1", "t")
    cid = conv["id"]
    for i in range(8):  # 8 条 > KEEP_RECENT_TURNS*2=6,有可压的旧历史
        store.add_message(cid, "user" if i % 2 == 0 else "assistant", f"消息{i}" * 30)

    full = cm.load(cid)["messages"]

    class _FakeLLM:
        def chat(self, messages, temperature=None, conv_id=None, stage=None):
            return "这是压缩摘要"

    side = CompressionSidechain(llm_client=_FakeLLM(), conversation=cm)
    recent = side.compress_async(cid, full)

    # 同步立即返回 keep-recent(不等待后台)
    assert len(recent) == KEEP_RECENT_TURNS * 2
    # 等后台线程完成(压缩 LLM 是假的,毫秒级;轮询兜底 2s)
    for _ in range(40):
        events = store.load_events(cid, kinds=["compacted", "compact_trace"])
        if len(events) >= 2:
            break
        time.sleep(0.05)
    kinds = [e["kind"] for e in events]
    assert "compacted" in kinds and "compact_trace" in kinds
    compacted = next(e for e in events if e["kind"] == "compacted")
    assert compacted["payload"]["summary"] == "这是压缩摘要"
    assert compacted["payload"]["tokens_before"] > 0
    side.close()


def test_conversation_exists_enforces_ownership(tmp_path):
    """归属校验:他人 conv_id 返回 False(chat 历史加载的越权防线)。"""
    store = ConversationStore(str(tmp_path / "c3.db"))
    conv = store.create_conversation("alice", "t")
    assert store.conversation_exists(conv["id"], "alice") is True
    assert store.conversation_exists(conv["id"], "bob") is False
    assert store.conversation_exists("no-such-id", "alice") is False
