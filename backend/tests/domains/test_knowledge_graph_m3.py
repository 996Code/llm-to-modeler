"""knowledge_graph M3 导入流水线单测 —— mock LLM + 内存图/向量存储。

覆盖:成功全流程 / 幂等跳过 / force 重导清理 / chunk 级断点续跑 /
连续失败熔断 / 并发导入守卫 / 向量模式启用与降级 / 本体归纳任务。
真实 Neo4j/Milvus/LLM 的端到端由脚本化验收完成(不走 pytest)。
"""
import time
from types import SimpleNamespace

import pytest

from domains.knowledge_graph import runtime, tasks
from domains.knowledge_graph.store import KGStore
from services.pack_settings import PackSettingsStore
from services.task_manager import TaskManager
from services.task_store import TaskStore


# ── 测试替身 ─────────────────────────────────────────────────

class MockLLM:
    """按 chunk 文本内容脚本化的 LLM:提取出文中 [E:名] 标记的实体。

    fail_markers:prompt 含这些子串时抛错(制造单块失败);
    induce_result:本体归纳的固定返回。
    """

    def __init__(self):
        self.fail_markers: list = []
        self.extract_calls = 0
        self.embed_calls = 0
        self.induce_result = {
            "entity_types": [{"key": "widget", "label": "部件", "description": "d",
                              "examples": ["w1"]}],
            "relation_types": [{"key": "part_of", "label": "属于", "description": "d",
                                "domain": ["widget"], "range": ["widget"]}],
        }

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None):
        content = messages[0]["content"]
        if stage == "kg.extract":
            for marker in self.fail_markers:
                if marker in content:
                    raise RuntimeError(f"mock 抽取失败(命中标记 {marker})")
            self.extract_calls += 1
            import re
            names = re.findall(r"\[E:([^\]]+)\]", content)
            entities = [{"name": n, "type": "person", "description": "", "aliases": []}
                        for n in names]
            relations = []
            if len(names) >= 2:
                relations.append({"source": names[0], "target": names[1],
                                  "type": "任职于", "description": "", "evidence": ""})
            return {"entities": entities, "relations": relations}
        if stage == "kg.induce_schema":
            return self.induce_result
        return {}

    def embeddings(self, texts, conv_id=None, stage=None):
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeGraph:
    """内存图存储(覆盖 tasks.py 用到的全部方法)。"""

    def __init__(self):
        self.nodes = {}        # (kb, normalized) -> node
        self.edges = []        # dicts with kb/doc_id/source/target/type
        self.delete_calls = []

    def upsert_batch(self, kb_id, doc_id, entities, relations):
        for e in entities:
            key = (kb_id, e["normalized_name"])
            node = self.nodes.setdefault(key, {
                "id": f"{kb_id}:{e['normalized_name']}", "name": e["name"],
                "normalized_name": e["normalized_name"], "type": e.get("type"),
                "source_docs": [], "type_status": e.get("type_status", "approved"),
            })
            if doc_id not in node["source_docs"]:
                node["source_docs"].append(doc_id)
        for r in relations:
            self.edges.append({**r, "kb": kb_id, "doc_id": doc_id})
        return {"entities": len(entities), "relations": len(relations)}

    def delete_document(self, kb_id, doc_id):
        self.delete_calls.append(doc_id)
        before_e = len(self.nodes)
        self.edges = [e for e in self.edges if not (e["kb"] == kb_id and e["doc_id"] == doc_id)]
        for key in list(self.nodes):
            node = self.nodes[key]
            if key[0] == kb_id and doc_id in node["source_docs"]:
                node["source_docs"].remove(doc_id)
                if not node["source_docs"]:
                    del self.nodes[key]
        return {"edges": 0, "orphanEntities": before_e - len(self.nodes)}

    def delete_kb(self, kb_id):
        self.nodes = {k: v for k, v in self.nodes.items() if k[0] != kb_id}
        self.edges = [e for e in self.edges if e["kb"] != kb_id]
        return {}

    def counts(self, kb_id):
        return {"entities": sum(1 for k in self.nodes if k[0] == kb_id),
                "relations": sum(1 for e in self.edges if e["kb"] == kb_id)}

    def document_counts(self, kb_id, doc_id):
        return {
            "entities": sum(1 for n in self.nodes.values()
                            if n["id"].startswith(kb_id) and doc_id in n["source_docs"]),
            "relations": sum(1 for e in self.edges
                             if e["kb"] == kb_id and e["doc_id"] == doc_id),
        }


class FakeVector:
    def __init__(self):
        self.collections = {}   # kb -> dim
        self.rows = {}          # kb -> {chunk_id: row}
        self.deleted_docs = []

    def ensure_collection(self, kb_id, dim):
        self.collections.setdefault(kb_id, dim)

    def drop_collection(self, kb_id):
        self.collections.pop(kb_id, None); self.rows.pop(kb_id, None)

    def upsert_chunks(self, kb_id, items):
        self.rows.setdefault(kb_id, {}).update({i["chunk_id"]: i for i in items})
        return len(items)

    def delete_by_doc(self, kb_id, doc_id):
        self.deleted_docs.append(doc_id)
        self.rows[kb_id] = {k: v for k, v in self.rows.get(kb_id, {}).items()
                            if v["doc_id"] != doc_id}

    def count(self, kb_id):
        return len(self.rows.get(kb_id, {}))


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时环境:独立 SQLite + 文件目录 + 小参数配置 + 纯图谱模式。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "kg.db"))
    monkeypatch.setenv("KG_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
    runtime.reset_runtime_cache()
    import services.task_manager as tm_mod
    _ = tm_mod  # noqa

    settings = PackSettingsStore(str(tmp_path / "settings.db"))
    settings.save_values("knowledge_graph", {
        "chunk_target_chars": 100, "chunk_overlap_chars": 0, "chunk_max_chars": 300,
        "llm_batch_size": 2, "llm_concurrency": 2, "llm_max_retries": 0,
        "failure_threshold": 5, "glossary_top_k": 100,
    })

    fake_graph, fake_vector, llm = FakeGraph(), FakeVector(), MockLLM()
    monkeypatch.setattr(runtime, "get_graph", lambda state: fake_graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: fake_vector)

    task_store = TaskStore(str(tmp_path / "tasks.db"))
    manager = TaskManager(task_store, max_workers=2)
    app_state = SimpleNamespace(
        llm_client=llm, settings_store=settings, task_manager=manager,
    )
    tasks.register_tasks(manager, app_state)

    yield SimpleNamespace(
        store=runtime.get_kg_store(app_state), settings=settings, graph=fake_graph,
        vector=fake_vector, llm=llm, manager=manager, app_state=app_state,
        tmp_path=tmp_path,
    )
    manager.close()
    runtime.reset_runtime_cache()
    tasks._app_state = None


def _wait(manager, task_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = manager.store.get_task(task_id)
        if t["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return t
        time.sleep(0.02)
    raise AssertionError("任务未在超时内完成")


def _make_doc(env, name: str, paragraphs: list):
    from domains.knowledge_graph.schema_templates import get_template_schema
    kb = env.store.create_kb(f"库-{name}", schema_json=get_template_schema("general"),
                             schema_template="general")
    content = "\n\n".join(paragraphs).encode("utf-8")
    doc = env.store.create_document(
        kb["id"], f"{name}.md", "text/markdown", len(content),
        "", __import__("hashlib").sha256(content).hexdigest())
    p = env.tmp_path / "files" / kb["id"]
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{doc['id']}.md"
    f.write_bytes(content)
    env.store.update_document(doc["id"], file_path=str(f))
    return kb, doc


# ── 用例 ─────────────────────────────────────────────────────

class TestImportPipeline:

    def test_success_flow(self, env):
        kb, doc = _make_doc(env, "成功", [
            "# 章节\n[E:甲] 和 [E:乙] 是同事。",
            "后来 [E:丙] 也加入了。",
            "再后来 [E:丁] 离职了。",
        ])
        task = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        final = _wait(env.manager, task["id"])
        assert final["status"] == "succeeded", final["error"]
        assert final["result"]["status"] == "succeeded"
        assert final["result"]["entities"] == 4      # 甲乙丙丁
        assert final["result"]["relations"] >= 1     # 至少一章的任职于
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "succeeded" and d["entityCount"] == 4
        assert all(c["status"] == "done" for c in env.store.list_chunks(doc["id"]))
        # 幂等清理先跑了一次(首次导入 = 空操作清理)
        assert env.graph.delete_calls == [doc["id"]]
        # 词表生效:后续批次的 prompt 应含前批实体名(通过抽取调用数间接验证)
        assert env.llm.extract_calls == len(env.store.list_chunks(doc["id"]))

    def test_idempotent_skip(self, env):
        kb, doc = _make_doc(env, "跳过", ["[E:甲] [E:乙]"])
        t1 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t1["status"] == "succeeded"
        calls_before = env.llm.extract_calls
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["result"].get("skipped") is True
        assert env.llm.extract_calls == calls_before  # 没有重复烧 LLM

    def test_force_reimport_cleans_and_redoes(self, env):
        kb, doc = _make_doc(env, "重导", ["[E:甲] [E:乙]", "[E:丙]"])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert env.store.get_document(doc["id"])["importStatus"] == "succeeded"
        t = _wait(env.manager, tasks.submit_import(
            env.app_state, kb["id"], doc["id"], force=True)["id"])
        assert t["status"] == "succeeded" and not t["result"].get("skipped")
        assert env.graph.delete_calls == [doc["id"], doc["id"]]  # 重导前清理
        assert env.store.get_document(doc["id"])["importStatus"] == "succeeded"

    def test_checkpoint_resume_only_failed_chunks(self, env):
        """单块失败 → partial;修复后重跑只重抽失败块(断点续跑)。"""
        pad = "背景铺垫文字。" * 12   # ~84 字,确保每段独立成块
        kb, doc = _make_doc(env, "续跑", [
            f"{pad}[E:甲] [E:乙]",
            f"{pad}坏块标记XYZ 出现在这一段 [E:丙]",
            f"{pad}[E:丁] 结尾",
        ])
        env.llm.fail_markers = ["坏块标记XYZ"]
        t1 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t1["status"] == "succeeded"
        assert t1["result"]["status"] == "partial" and t1["result"]["failedChunks"] >= 1
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "partial" and "失败" in d["error"]
        statuses = [c["status"] for c in env.store.list_chunks(doc["id"])]
        assert "failed" in statuses and "done" in statuses
        calls_after_first = env.llm.extract_calls

        # 修复"上游"后重跑:不 force → 复用 chunk,只重抽失败块
        env.llm.fail_markers = []
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["status"] == "succeeded" and t2["result"]["status"] == "succeeded"
        re_extracted = env.llm.extract_calls - calls_after_first
        assert re_extracted == statuses.count("failed")
        assert all(c["status"] == "done" for c in env.store.list_chunks(doc["id"]))

    def test_circuit_breaker(self, env):
        env.settings.save_values("knowledge_graph", {"failure_threshold": 2})
        pad = "背景铺垫文字。" * 12
        kb, doc = _make_doc(env, "熔断", [
            f"{pad}[E:甲]", f"{pad}坏块标记A", f"{pad}坏块标记B", f"{pad}[E:乙]",
        ])
        env.llm.fail_markers = ["坏块标记A", "坏块标记B"]
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "failed"
        assert "熔断" in t["error"]
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "failed" and "熔断" in d["error"]

    def test_concurrent_same_doc_rejected(self, env):
        env.llm.fail_markers = []  # 无失败
        # 用大文档 + 人为放慢抽取,让任务跑起来
        kb, doc = _make_doc(env, "并发", [f"[E:甲{i}] [E:乙{i}]" for i in range(30)])
        orig = env.llm.chat_json
        def slow(*a, **kw):
            time.sleep(0.05)
            return orig(*a, **kw)
        env.llm.chat_json = slow
        t1 = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        with pytest.raises(ValueError, match="进行中"):
            tasks.submit_import(env.app_state, kb["id"], doc["id"])
        _wait(env.manager, t1["id"])
        # 完成后可再次提交
        t2 = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        assert _wait(env.manager, t2["id"])["status"] == "succeeded"

    def test_vector_mode_enabled_and_upserted(self, env, monkeypatch):
        monkeypatch.setenv("LLM_EMBED_MODEL", "mock-embed")
        kb, doc = _make_doc(env, "向量", ["[E:甲] [E:乙]", "[E:丙]"])
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        assert t["result"]["vectorEnabled"] is True
        kb_after = env.store.get_kb(kb["id"])
        assert kb_after["vectorEnabled"] and kb_after["vectorDim"] == 3
        assert env.vector.collections.get(kb["id"]) == 3
        assert env.vector.count(kb["id"]) == len(env.store.list_chunks(doc["id"]))

    def test_vector_degraded_without_model(self, env):
        kb, doc = _make_doc(env, "降级", ["[E:甲]"])
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        assert t["result"]["vectorEnabled"] is False
        assert not env.store.get_kb(kb["id"])["vectorEnabled"]


class TestInduceSchema:

    def test_induce_stores_pending_proposal(self, env):
        kb, doc = _make_doc(env, "归纳", ["[E:甲] [E:乙]", "[E:丙]"])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        t = _wait(env.manager, tasks.submit_induce_schema(env.app_state, kb["id"])["id"])
        assert t["status"] == "succeeded", t["error"]
        assert t["result"]["entityTypes"] == 1
        schema = env.store.get_kb(kb["id"])["schema"]
        assert schema["pending_schema_induction"]["entity_types"][0]["key"] == "widget"
        # 原本体不被覆盖
        assert schema["entity_types"], "原实体类型应保留"
