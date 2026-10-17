"""KG 导入块日志的文档级进度口径(total/done)。

背景(2026-10-17 用户报):前端"逐块抽取"徽章分母用组内行数,批跑完
分子恒等于分母——永远 1/1、2/2 无信息量。修复口径:后端块日志
(started/done/failed)自带文档级 total/done,前端徽章优先消费。
本文件锁死:done 单调推进、终值=total、失败块也推进 done。
"""
import pytest
from types import SimpleNamespace

from domains.knowledge_graph import runtime, tasks as kg_tasks

# 复用 m4 fake 环境(MockLLM/graph/vector)
from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401
from tests.domains.test_knowledge_graph_m4 import (
    RetrievalFakeGraph, RetrievalFakeVector,
)


class _ImportFakeVector(RetrievalFakeVector):
    """补导入路径的向量方法(建库/按文档删/upsert 内存态即可)。"""

    def ensure_collection(self, kb_id, dim):
        pass

    def delete_by_doc(self, kb_id, doc_id):
        pass

    def upsert(self, kb_id, items):
        pass


class _ImportFakeGraph(RetrievalFakeGraph):
    """在 m4 检索 fake 图上补导入路径所需的方法(内存态即可)。"""

    def delete_document(self, kb, doc_id):
        return {"edges": 0, "orphanEntities": 0}

    def list_entity_aliases(self, kb):
        return {}

    def list_entity_names(self, kb):
        return []

    def upsert_batch(self, kb, doc_id, entities, relations):
        self._last_batch = (entities, relations)

    def document_counts(self, kb, doc_id):
        return {"entities": 0, "relations": 0}


class _RecordingHandle:
    """记录型任务 handle:捕获 log 调用的 data 载荷。"""

    def __init__(self, payload):
        self.payload = payload
        self.task_id = "task-test"
        self.entries: list[dict] = []

    def log(self, message, level="info", **data):
        self.entries.append({"message": message, "level": level, "data": data})

    def set_progress(self, percent, message=""):
        pass

    def check_cancel(self):
        pass


class TestChunkLogProgress:

    @pytest.fixture(autouse=True)
    def _inject_app_state(self, env, monkeypatch):
        """pack.py 装配时把 app_state 注入 tasks 全局——测试同款注入。
        graph 换成补齐导入方法的三倍体(m4 检索 fake 图缺导入侧接口)。"""
        monkeypatch.setattr(kg_tasks, "_app_state", env.app_state)
        fake = _ImportFakeGraph()
        fake_vector = _ImportFakeVector()
        from domains.knowledge_graph import runtime as _rt
        monkeypatch.setattr(_rt, "get_graph", lambda state: fake)
        monkeypatch.setattr(_rt, "get_vector", lambda state: fake_vector)

    def test_total_done_monotonic_and_final_equals_total(self, env):
        """3 块导入:块日志 total 恒为 3;done 1→2→3 单调,终值=total。"""
        store = env.store
        kb = store.create_kb("进度口径测试库", "")
        doc = store.create_document(kb["id"], "t.txt", "text/plain", 30, "/dev/null", "h1")
        chunks = [{"seq": s, "text": f"第{s}段内容,实体甲。"}
                  for s in range(3)]
        store.replace_chunks(doc["id"], kb["id"], chunks)

        handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"],
                                   "force": False, "chunk_ids": []})
        result = kg_tasks.run_import_document(handle)
        assert result["chunks"] == 3

        # 抓块级终态日志(抽取完成/失败)与 started 行
        chunk_logs = [e for e in handle.entries
                      if e["data"].get("chunk_state") in ("done", "failed")]
        started_logs = [e for e in handle.entries
                        if e["data"].get("chunk_state") == "started"]
        assert len(chunk_logs) == 3

        # total 恒定=3(所有块日志)
        for e in chunk_logs + started_logs:
            assert e["data"].get("total") == 3, f"total 口径失真: {e['data']}"

        # done 单调推进且终值=total
        dones = [e["data"]["done"] for e in chunk_logs]
        assert dones == sorted(dones), f"done 非单调: {dones}"
        assert dones[-1] == 3
        assert len(set(dones)) == 3  # 每块+1,无重复无跳号

    def test_failed_chunk_also_advances_done(self, env, monkeypatch):
        """失败块也算推进:3 块中 1 块失败,done 终值仍是 3。"""
        store = env.store
        kb = store.create_kb("失败口径测试库", "")
        doc = store.create_document(kb["id"], "t.txt", "text/plain", 30, "/dev/null", "h2")
        store.replace_chunks(doc["id"], kb["id"],
                             [{"seq": s, "text": f"第{s}段内容。"} for s in range(3)])

        # 让第 1 块的 LLM 调用炸:kg.extract 阶段抛异常,重试耗尽按块失败
        from tests.domains.test_knowledge_graph_m4 import MockLLM

        class _BoomExtract(MockLLM):
            def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
                if stage == "kg.extract":
                    raise RuntimeError("模拟抽取失败")
                return super().chat_json(messages, temperature=temperature,
                                         conv_id=conv_id, stage=stage, model=model)

        monkeypatch.setattr(env, "llm", _BoomExtract())
        # run_import_document 内部经 runtime 拿 llm——patch env.app_state 上的引用
        env.app_state.llm_client = env.llm

        handle = _RecordingHandle({"kb_id": kb["id"], "doc_id": doc["id"],
                                   "force": False, "chunk_ids": []})
        result = kg_tasks.run_import_document(handle)
        chunk_logs = [e for e in handle.entries
                      if e["data"].get("chunk_state") in ("done", "failed")]
        assert len(chunk_logs) == 3
        for e in chunk_logs:
            assert e["data"].get("total") == 3
        dones = sorted(e["data"]["done"] for e in chunk_logs)
        assert dones[-1] == 3, f"失败块未推进 done: {dones}"
        assert result["failedChunks"] >= 1
