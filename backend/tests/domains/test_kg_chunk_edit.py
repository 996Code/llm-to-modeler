"""D27 块编辑+增量重索引 —— 改文本仅重建该块向量,未编辑块零重算。

覆盖:编辑落库(文本/字数/全文索引生成列随 UPDATE 重算)、向量增量
重建(新子段 upsert+残留旧子段清理)、未编辑块向量零重算、向量失败
不回滚文本(诚实 warning)、图谱不动(第一版语义边界)、422 校验。
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router

# 复用 m3 的导入流水线环境(真实 store + TaskManager + fake 图/向量)
from tests.domains.test_knowledge_graph_m3 import _make_doc, _wait, env  # noqa: F401


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = env.settings
    app.state.task_manager = env.manager
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    monkeypatch.setattr(runtime, "get_graph", lambda state: env.graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: env.vector)
    return TestClient(app), env


def _imported_doc(env, name: str, paragraphs: list):
    """建库+文档+跑完导入,返回 (kb dict, doc, chunks)。"""
    from domains.knowledge_graph.schema_templates import get_template_schema
    kb = env.store.create_kb(f"库-{name}", schema_json=get_template_schema("general"),
                             schema_template="general")
    content = "\n\n".join(paragraphs).encode("utf-8")
    doc = env.store.create_document(
        kb["id"], f"{name}.md", "text/markdown", len(content),
        "", __import__("hashlib").sha256(content).hexdigest())
    p = env.tmp_path / "files" / kb["id"]
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{doc['id']}.md").write_bytes(content)
    env.store.update_document(doc["id"], file_path=str(p / f"{doc['id']}.md"))
    t = _wait(env.manager, __import__(
        "domains.knowledge_graph.tasks", fromlist=["submit_import"]).submit_import(
        env.app_state, kb["id"], doc["id"])["id"])
    assert t["status"] == "succeeded"
    kb_dict = {"id": kb["id"], "name": kb["name"], "description": "",
               "schema": {"relation_types": []},
               "vectorEnabled": True, "vectorDim": 3}
    return kb_dict, doc, env.store.list_chunks(doc["id"])


class TestChunkEdit:

    def test_edit_updates_text_and_reembeds(self, client):
        """编辑:文本落库 + 该块子段向量重建(新文本可召回)。"""
        c, env = client
        # 章节标题强制分块(章内容须 ≥40 字,否则短缓冲被目录合并逻辑并块)
        pad1 = "第一章的背景铺垫内容。" * 4
        pad2 = "第二章的背景铺垫内容。" * 4
        kb, doc, chunks = _imported_doc(env, "编辑库", [
            f"# 第一章\n[E:甲] [E:乙] 原始内容第一段。{pad1}",
            f"# 第二章\n[E:丙] 原始内容第二段。{pad2}"])
        assert len(chunks) == 2
        target = chunks[0]
        old_rows = dict(env.vector.rows[kb["id"]])
        embed_calls_before = env.llm.embed_calls
        new_text = "[E:甲] 编辑后的全新内容,包含独特关键词蓝鲸量子。"

        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/"
                  f"{doc['id']}/chunks/{target['id']}",
                  json={"text": new_text})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vectorUpdated"] is True
        assert body["chunk"]["charCount"] == len(new_text)

        # 块文本已更新
        fresh = env.store.list_chunks(doc["id"])
        assert "蓝鲸量子" in fresh[0]["text"]
        # 该块的子段向量已重建(文本换新)
        sub_ids = [k for k in env.vector.rows[kb["id"]] if k.startswith(f"{target['id']}#")]
        assert sub_ids and all(
            "蓝鲸量子" in env.vector.rows[kb["id"]][k]["text"] for k in sub_ids)
        # 未编辑块零重算:其它块的向量行原样(对象同一性)
        other = chunks[1]["id"]
        other_ids = [k for k in old_rows if k.startswith(f"{other}#")]
        for k in other_ids:
            assert env.vector.rows[kb["id"]][k] is old_rows[k]
        # 只重嵌了该块(embed 调用次数增量 = 该块子段数,不是全文档)
        sub_count = len(sub_ids)
        assert env.llm.embed_calls - embed_calls_before == sub_count

    def test_edit_cleans_stale_sub_chunks(self, client):
        """新文本更短(子段数变少)→ 残留旧子段被清,不参与召回。"""
        c, env = client
        # 章节标题走结构块路径:整章一块(不受 pack 的小 target 切碎),
        # 长章子切成多个向量子段
        long_text = ("# 巨章\n" + "[E:甲] 很长的内容。" * 120)
        kb, doc, chunks = _imported_doc(env, "残留库", [long_text, "[E:乙] 短段。"])
        target = chunks[0]
        old_sub_ids = {k for k in env.vector.rows[kb["id"]]
                       if k.startswith(f"{target['id']}#")}
        assert len(old_sub_ids) >= 2   # 前置:原文本确有多子段

        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/"
                  f"{doc['id']}/chunks/{target['id']}",
                  json={"text": "[E:甲] 短了。"})
        assert r.status_code == 200
        new_sub_ids = {k for k in env.vector.rows[kb["id"]]
                       if k.startswith(f"{target['id']}#")}
        assert len(new_sub_ids) == 1
        # 残留清零:被替换掉的旧子段 id 不再在向量库里(集合差本身
        # 恒非空——断言的是"差集与现存行无交集")
        stale = old_sub_ids - new_sub_ids
        current = set(env.vector.rows[kb["id"]].keys())
        assert not (stale & current)

    def test_vector_failure_keeps_text_with_warning(self, client, monkeypatch):
        """向量重建失败:文本不回滚,warning 如实返回(不静默假同步)。"""
        c, env = client
        kb, doc, chunks = _imported_doc(env, "失败库", ["[E:甲] 内容。"])

        def boom(texts, conv_id=None, stage=None, mode=None):
            raise RuntimeError("embed down")
        monkeypatch.setattr(env.llm, "embeddings", boom)

        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/"
                  f"{doc['id']}/chunks/{chunks[0]['id']}",
                  json={"text": "[E:甲] 新文本。"})
        assert r.status_code == 200
        body = r.json()
        assert body["vectorUpdated"] is False
        assert body["warning"] and "向量重建失败" in body["warning"]
        # 文本已生效
        assert "新文本" in env.store.list_chunks(doc["id"])[0]["text"]

    def test_edit_validations(self, client):
        """422:空文本/超长;404:块不属于该文档。"""
        c, env = client
        kb, doc, chunks = _imported_doc(env, "校验库", ["[E:甲] 内容。"])
        base = f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/chunks"
        assert c.put(f"{base}/{chunks[0]['id']}", json={"text": "  "}).status_code == 422
        assert c.put(f"{base}/{chunks[0]['id']}",
                     json={"text": "x" * 50001}).status_code == 422
        assert c.put(f"{base}/nope", json={"text": "y"}).status_code == 404

    def test_edit_rejected_while_importing(self, client, monkeypatch):
        """并发守卫:导入进行中编辑 → 409(防旧文本向量被任务回写漂移)。"""
        c, env = client
        kb, doc, chunks = _imported_doc(env, "并发库", ["[E:甲] 内容。"])
        from domains.knowledge_graph import tasks as kg_tasks
        monkeypatch.setattr(kg_tasks, "_inflight_task_id",
                            lambda state, doc_id: "task-1234")
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/"
                  f"{doc['id']}/chunks/{chunks[0]['id']}",
                  json={"text": "新文本。"})
        assert r.status_code == 409 and "导入中" in r.json()["detail"]

    def test_graph_not_touched(self, client):
        """第一版语义边界:编辑不动图谱(实体/关系仍是旧文本的)。"""
        c, env = client
        kb, doc, chunks = _imported_doc(env, "图谱边界库", ["[E:甲] [E:乙] 内容。"])
        nodes_before = dict(env.graph.nodes)
        edges_before = list(env.graph.edges)

        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/"
                  f"{doc['id']}/chunks/{chunks[0]['id']}",
                  json={"text": "[E:丙] 完全不同的新内容。"})
        assert r.status_code == 200
        assert env.graph.nodes == nodes_before
        assert env.graph.edges == edges_before
