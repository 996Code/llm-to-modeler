"""标题守卫(P0-1 移植)——标题是会话级产物,首轮定稿,后续轮不再重算。

实测背景(2026-11-14,标准问答移植后浏览器实测):标题 LLM 调用 2670ms
> _take_title_future 窗口 2000ms,快轮工具先把 future 挤丢,标题永不
落库 → 守卫每轮读到空标题再生成一次(call_logs 实证 conv 每轮 1 条
conversation_title)。守卫三半:
  - 读侧(nodes._conversation_has_title):已有非空标题 → 跳过生成;
    读库异常 fail-open 回到生成行为
  - 写侧·线程(nodes._submit_title_generation):标题后台线程生成完
    直接 set_title_if_empty 落库——不再依赖收尾窗口
  - 写侧·库(store.set_title_if_empty / update_conversation_config):
    仅空时写,首轮标题/用户创建标题不被后续轮覆盖

裸标题 vs 展示口径陷阱:列表/详情的 title 空时兜底"新对话",守卫必须
读裸标题(store.get_title)——用展示口径会把每个新会话误判成已有标题。
"""
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from engine import nodes
from engine.conversation import ConversationManager
from services.conversation_store import ConversationStore


def _configure(llm, conversation):
    """单 pack 装配:packs 声明单链路直通(零路由 LLM),标题是唯一 LLM 调用。"""
    nodes.configure(
        registry=MagicMock(),
        llm_client=llm,
        asset_client=None,
        conversation=conversation,
        prompt_loader=MagicMock(),
        pack_routers={"knowledge_graph": MagicMock()},
        pack_configs={"knowledge_graph": {"domain": {"description": "知识库", "fallback": True}}},
    )


def _state(**over):
    state = {
        "user_input": "怎么下载证书",
        "compressed_history": "",
        "conversation_id": "conv-1",
        "packs": ["knowledge_graph"],
    }
    state.update(over)
    return state


def _llm():
    llm = MagicMock()
    llm.chat_json.return_value = {"title": "LLM新标题"}
    return llm


class _InlineExecutor:
    """同步执行器:submit 即执行(future 已完成)——标题线程内联化,
    测试不跨线程竞争(PG 测试库逐用例清表与后台线程不兼容)。"""

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as e:  # pragma: no cover - _bg 不抛,防御
            f.set_exception(e)
        return f


@pytest.fixture()
def inline_title_executor(monkeypatch):
    """把标题线程池换成内联执行器,并清在途 future 表。"""
    monkeypatch.setattr(nodes, "_TITLE_EXECUTOR", _InlineExecutor())
    nodes._TITLE_FUTURES.clear()
    yield
    nodes._TITLE_FUTURES.clear()


class TestTitleGuardRead:
    """读侧:nodes._conversation_has_title(空串=无标题/不存在/读失败)。"""

    def test_existing_title_skips_generation(self, inline_title_executor):
        """会话已有非空标题:不再提交标题生成(零 LLM 调用)。"""
        llm = _llm()
        conv = MagicMock()
        conv.get_title.return_value = "已有标题"
        _configure(llm, conv)
        result = nodes.classify_intent_node(_state())
        llm.chat_json.assert_not_called()
        assert nodes._TITLE_FUTURES == {}

    def test_empty_title_generates_and_persists(self, inline_title_executor):
        """标题为空(首轮/上次生成失败):生成且线程内直接落库。"""
        llm = _llm()
        conv = MagicMock()
        conv.get_title.return_value = ""
        _configure(llm, conv)
        nodes.classify_intent_node(_state())
        llm.chat_json.assert_called_once()
        assert llm.chat_json.call_args.kwargs.get("stage") == "conversation_title"
        # 后台线程自落库(不依赖收尾窗口)
        conv.set_title_if_empty.assert_called_once_with("conv-1", "LLM新标题")
        # 收尾合并仍能拿到标题(内联执行器下 future 已完成)
        assert nodes._take_title_future("conv-1") == "LLM新标题"

    def test_read_failure_fails_open(self, inline_title_executor):
        """守卫读异常:fail-open 回到生成行为(守卫绝不阻塞主流程)。"""
        llm = _llm()
        conv = MagicMock()
        conv.get_title.side_effect = RuntimeError("db down")
        _configure(llm, conv)
        nodes.classify_intent_node(_state())
        llm.chat_json.assert_called_once()

    def test_no_conversation_store_generates(self, inline_title_executor):
        """未装配会话存储(独立模式):守卫不可用,维持生成行为;
        线程内落库对 None 门面静默跳过。"""
        llm = _llm()
        _configure(llm, None)
        nodes.classify_intent_node(_state())
        llm.chat_json.assert_called_once()

    def test_slow_title_still_persists_after_take_timeout(self, monkeypatch):
        """实测回归:标题 LLM 慢于收尾窗口 → future 被丢弃,但线程内
        落库不丢标题(次轮守卫读到标题,不再重复生成)。"""
        llm = _llm()

        def slow_generate(user_input, conversation_id):
            import time
            time.sleep(0.15)
            return "慢标题"
        monkeypatch.setattr(nodes, "_generate_conversation_title", slow_generate)
        monkeypatch.setattr(nodes, "_TITLE_EXECUTOR", ThreadPoolExecutor(max_workers=1))
        nodes._TITLE_FUTURES.clear()
        conv = MagicMock()
        conv.get_title.return_value = ""
        _configure(llm, conv)
        nodes.classify_intent_node(_state())
        # 收尾窗口极短:future 未就绪被丢弃(旧实现此处标题永久丢失)
        assert nodes._take_title_future("conv-1", timeout=0.01) == ""
        # 后台线程自行落库——等它完成并断言
        import time as _t
        deadline = _t.monotonic() + 2
        while _t.monotonic() < deadline:
            if conv.set_title_if_empty.called:
                break
            _t.sleep(0.02)
        conv.set_title_if_empty.assert_called_once_with("conv-1", "慢标题")
        nodes._TITLE_EXECUTOR.shutdown(wait=True)
        nodes._TITLE_FUTURES.clear()


class TestConversationManagerTitleFacade:
    """门面:get_title 走裸标题/fail-open;set_title_if_empty 透传。"""

    def test_get_title_delegates_and_masks_failure(self):
        store = MagicMock()
        store.get_title.return_value = "标题A"
        cm = ConversationManager(store=store)
        assert cm.get_title("c1") == "标题A"
        store.get_title.assert_called_once_with("c1")

        store.get_title.side_effect = RuntimeError("db down")
        assert cm.get_title("c1") == ""  # 读失败按无标题(不抛)

        assert ConversationManager(store=None).get_title("c1") == ""

    def test_set_title_if_empty_passthrough(self):
        store = MagicMock()
        store.set_title_if_empty.return_value = True
        cm = ConversationManager(store=store)
        assert cm.set_title_if_empty("c1", "标题") is True
        store.set_title_if_empty.assert_called_once_with("c1", "标题")
        # 无 store(独立模式):False,不抛
        assert ConversationManager(store=None).set_title_if_empty("c1", "t") is False


class TestTitleWriteIfEmpty:
    """写侧(PG 测试库,conftest 每测试清表):仅空时写,原子条件。"""

    def test_set_title_if_empty_writes_then_rejects(self):
        store = ConversationStore()
        conv_id = store.create_conversation("user1", "")["id"]
        assert store.set_title_if_empty(conv_id, "首轮标题") is True
        # 二次写(后续轮/工具钩子)不覆盖
        assert store.set_title_if_empty(conv_id, "后续轮标题") is False
        assert store.get_title(conv_id) == "首轮标题"

    def test_set_title_if_empty_ignores_empty_title(self):
        store = ConversationStore()
        conv_id = store.create_conversation("user1", "")["id"]
        assert store.set_title_if_empty(conv_id, "") is False
        assert store.get_title(conv_id) == ""

    def test_update_conversation_config_keeps_existing_title(self):
        """config 更新附带 title:已有标题不被覆盖(用户创建标题守住);
        空标题会话正常写入。"""
        store = ConversationStore()
        conv1 = store.create_conversation("user1", "用户起的标题")["id"]
        store.update_conversation_config(conv1, {"formCode": "c1"}, title="引擎标题")
        conv = store.get_conversation(conv1, "user1")
        assert conv["title"] == "用户起的标题"
        assert conv["currentConfig"]["formCode"] == "c1"

        conv2 = store.create_conversation("user2", "")["id"]
        store.update_conversation_config(conv2, {"formCode": "c2"}, title="引擎标题2")
        assert store.get_conversation(conv2, "user2")["title"] == "引擎标题2"

    def test_raw_title_not_display_fallback(self):
        """裸标题≠展示口径:新会话展示 title 兜底"新对话",守卫读的
        裸标题必须是空串——否则守卫把每个新会话误判成已有标题。"""
        store = ConversationStore()
        conv_id = store.create_conversation("user1", "")["id"]
        assert store.list_conversations("user1")[0]["title"] == "新对话"  # 展示口径
        assert store.get_title(conv_id) == ""                            # 守卫口径

        store.set_title_if_empty(conv_id, "真实标题")
        assert store.get_title(conv_id) == "真实标题"
