"""D14 摘要块——抽象问法兜底(seq=-1 摘要块 + 向量化入库)。

"XX 文档主要讲什么"式问法无具体实体/关键词,三路检索可能零命中;
导入收尾生成 LLM 摘要块(seq=-1, status=done)进向量索引兜底。
锁死:成功生成入库、重导幂等(先删旧摘要)、LLM 失败降级不阻断、
无块空文档跳过。
"""
import pytest

from domains.knowledge_graph import runtime, tasks as kg_tasks
from domains.knowledge_graph.store import KGStore

from tests.domains.test_kg_chunk_progress import (
    _ImportFakeGraph, _ImportFakeVector, _RecordingHandle,
)
from tests.domains.test_knowledge_graph_m4 import MockLLM, env  # noqa: F401


class _SummaryLLM(MockLLM):
    """kg.summary 阶段返回可识别摘要文本。"""

    def __init__(self):
        super().__init__()
        self.summary_calls: list[str] = []

    def chat(self, messages, temperature=None, max_tokens=None,
             conv_id=None, stage=None):
        if stage == "kg.summary":
            self.summary_calls.append(messages[0]["content"][:40])
            return "本文档规定了劳动用工的基本制度与劳动者权益保护。"
        return super().chat(messages, temperature=temperature,
                            max_tokens=max_tokens, conv_id=conv_id, stage=stage)


@pytest.fixture()
def import_env(env, monkeypatch):
    monkeypatch.setattr(kg_tasks, "_app_state", env.app_state)
    fake_g, fake_v = _ImportFakeGraph(), _ImportFakeVector()
    monkeypatch.setattr(runtime, "get_graph", lambda state: fake_g)
    monkeypatch.setattr(runtime, "get_vector", lambda state: fake_v)
    llm = _SummaryLLM()
    env.app_state.llm_client = llm
    return env, fake_v, llm


def _mk_doc(store, kb_name="摘要测试库", n=3):
    kb = store.create_kb(kb_name, "")
    doc = store.create_document(kb["id"], "t.txt", "text/plain", 30,
                                "/dev/null", "h-" + kb_name)
    store.replace_chunks(doc["id"], kb["id"],
                         [{"seq": s, "text": f"第{s}章内容,实体甲。"} for s in range(n)])
    return kb, doc


class TestSummaryChunk:

    def test_成功生成摘要块(self, import_env):
        env, fake_v, llm = import_env
        kb, doc = _mk_doc(env.store)
        handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"],
                                   "force": False, "chunk_ids": []})
        result = kg_tasks.run_import_document(handle)
        assert result["status"] == "succeeded"

        # 摘要块落库:seq=-1, status=done, 文本可识别
        chunks = env.store.list_chunks(doc["id"], include_summary=True)
        summaries = [c for c in chunks if c["seq"] == -1]
        assert len(summaries) == 1
        assert summaries[0]["status"] == "done"
        assert "劳动用工" in summaries[0]["text"]
        # LLM 确实被调了摘要阶段
        assert len(llm.summary_calls) == 1

    def test_重导幂等_旧摘要先删(self, import_env):
        env, _, _ = import_env
        kb, doc = _mk_doc(env.store, "幂等测试库")
        for _ in range(2):  # 跑两遍导入(重导路径)
            handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"],
                                       "force": False, "chunk_ids": []})
            kg_tasks.run_import_document(handle)
        # 摘要块只有一条(不是累积)
        summaries = [c for c in env.store.list_chunks(doc["id"], include_summary=True)
                     if c["seq"] == -1]
        assert len(summaries) == 1

    def test_LLM失败降级不阻断(self, import_env, monkeypatch):
        env, _, _ = import_env

        def _boom(*a, **k):
            raise RuntimeError("模拟摘要 LLM 失败")

        monkeypatch.setattr(env.app_state.llm_client, "chat", _boom)
        # 抽取也用 chat_json 不受影响;但 chat 失败会让抽取后的回答链路炸
        # ——直接单测 _generate_summary_chunk 的降级路径
        kb, doc = _mk_doc(env.store, "降级测试库")
        handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"]})
        ok = kg_tasks._generate_summary_chunk(
            handle, env.app_state, env.store, kb,
            env.store.get_document(doc["id"]),
            env.store.list_chunks(doc["id"]), "conv-t")
        assert ok is False
        # 降级日志可见
        assert any("摘要块生成失败" in e["message"] for e in handle.entries)

    def test_无块空文档跳过(self, import_env):
        env, _, _ = import_env
        kb = env.store.create_kb("空文档测试库", "")
        doc = env.store.create_document(kb["id"], "e.txt", "text/plain", 0,
                                        "/dev/null", "h-empty")
        handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"]})
        ok = kg_tasks._generate_summary_chunk(
            handle, env.app_state, env.store, kb,
            env.store.get_document(doc["id"]),
            env.store.list_chunks(doc["id"]), "conv-t")
        assert ok is False

    def test_store层_upsert(self, env):
        """upsert_summary_chunk:插行+重跑替换(非累积)。"""
        kb = env.store.create_kb("store层测试库", "")
        doc = env.store.create_document(kb["id"], "s.txt", "text/plain", 5,
                                        "/dev/null", "h-store")
        id1 = env.store.upsert_summary_chunk(doc["id"], kb["id"], "第一版摘要")
        id2 = env.store.upsert_summary_chunk(doc["id"], kb["id"], "第二版摘要")
        assert id1 != id2
        rows = [c for c in env.store.list_chunks(doc["id"], include_summary=True)
                if c["seq"] == -1]
        assert len(rows) == 1 and rows[0]["text"] == "第二版摘要"
