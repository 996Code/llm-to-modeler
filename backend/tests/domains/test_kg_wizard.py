"""D33 建库向导 —— 库级分段参数 + 分段实时预览(与导入同代码路径)。

覆盖:chunk-preview 端点(预览=导入逐块一致/参数生效/422 拒非法)、
库级分段参数 CRUD(建库带参/更新重配/校验)、导入按库级参数切块
(库级 > 插件设置 > 默认 的解析链)。
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime, tasks
from sdk.doc_parser import chunk_text, parse_to_text

# 复用 m3 的导入流水线环境(MockLLM/FakeGraph/FakeVector/TaskManager)
from tests.domains.test_knowledge_graph_m3 import _wait, env  # noqa: F401


@pytest.fixture()
def kb_client(env, monkeypatch):
    """管理端 API 客户端(store 指向 env 的真实 store;开放模式)。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from domains.knowledge_graph.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = env.settings
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    return TestClient(app), env


def _md_file(content: str, name: str = "doc.md"):
    return {"file": (name, io.BytesIO(content.encode("utf-8")), "text/markdown")}


# ── 分段实时预览端点 ─────────────────────────────────────────

class TestChunkPreview:

    def test_preview_matches_import_chunking(self, kb_client):
        """预览=导入逐块一致:同一文件+参数,预览的块与 chunk_text 直调
        完全同构(验收硬口径——预览骗人等于没预览)。"""
        client, _ = kb_client
        content = "# 第一章 起源\n" + "张三在山谷中修行。" * 60 + \
                  "\n# 第二章 历练\n" + "李四下山历练去了。" * 60
        params = {"target": 800, "overlap": 50, "max_chars": 2000,
                  "structural_max": 5000, "vector_sub": 500}
        r = client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                        files=_md_file(content), data=params)
        assert r.status_code == 200, r.text
        body = r.json()
        expected = chunk_text(
            parse_to_text("doc.md", content.encode("utf-8")),
            target_chars=800, overlap_chars=50, max_chars=2000,
            structural_max_chars=5000)
        assert body["textChars"] == len(parse_to_text("doc.md", content.encode()))
        assert len(body["chunks"]) == len(expected)
        for got, exp in zip(body["chunks"], expected):
            assert got["seq"] == exp["seq"] and got["charCount"] == exp["char_count"]
            assert got["preview"] == exp["text"][:200]

    def test_preview_params_take_effect(self, kb_client):
        """参数生效:目标块更小 → 块更多(调参即时看效果的依据)。"""
        client, _ = kb_client
        # 多段落文本(target 是按块边界累积的软目标,单段落不触发)
        content = "\n\n".join(
            "。".join(f"第{i}句内容" for i in range(20)) + "。" for _ in range(30))
        big = client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                          files=_md_file(content),
                          data={"target": 2000, "overlap": 0, "max_chars": 3000,
                                "structural_max": 5000, "vector_sub": 500}).json()
        small = client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                            files=_md_file(content),
                            data={"target": 300, "overlap": 0, "max_chars": 3000,
                                  "structural_max": 5000, "vector_sub": 500}).json()
        assert small["stats"]["chunkCount"] > big["stats"]["chunkCount"]

    def test_preview_rejects_bad_requests(self, kb_client):
        """422 拒非法:坏扩展名/越界参数/空文件。"""
        client, _ = kb_client
        ok = {"target": 1200, "overlap": 100, "max_chars": 3000,
              "structural_max": 10000, "vector_sub": 500}
        assert client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                           files=_md_file("x", "doc.exe"), data=ok).status_code == 422
        bad = dict(ok, target=10)   # 低于下限 200
        assert client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                           files=_md_file("内容"), data=bad).status_code == 422
        empty = {"file": ("e.md", io.BytesIO(b""), "text/markdown")}
        assert client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                           files=empty, data=ok).status_code == 422

    def test_preview_sub_chunks_counted(self, kb_client):
        """向量子切段数进预览(父子回带口径的直观呈现:块大→子段多)。"""
        client, _ = kb_client
        content = "。".join(f"第{i}句内容" for i in range(400)) + "。"
        body = client.post("/api/packs/knowledge_graph/kbs/chunk-preview",
                           files=_md_file(content),
                           data={"target": 2000, "overlap": 0, "max_chars": 3000,
                                 "structural_max": 5000, "vector_sub": 300}).json()
        assert body["stats"]["subChunkTotal"] >= body["stats"]["chunkCount"]
        assert all(c["subChunks"] >= 1 for c in body["chunks"])


# ── 库级分段参数 CRUD ───────────────────────────────────────

class TestKbChunkParams:

    def test_create_kb_with_chunk_params(self, kb_client):
        client, _ = kb_client
        r = client.post("/api/packs/knowledge_graph/kbs", json={
            "name": "向导库", "template": "general",
            "chunk_params": {"chunk_target_chars": 600, "vector_subchunk_chars": 400},
        })
        assert r.status_code == 200, r.text
        assert r.json()["chunkParams"] == {
            "chunk_target_chars": 600, "vector_subchunk_chars": 400}

    def test_update_kb_chunk_params(self, kb_client):
        """设置页重配:PUT 更新分段参数,返回体反映新值。"""
        client, _ = kb_client
        kb = client.post("/api/packs/knowledge_graph/kbs", json={
            "name": "重配库", "template": "general"}).json()
        r = client.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}",
                       json={"chunk_params": {"chunk_target_chars": 800}})
        assert r.status_code == 200
        assert r.json()["chunkParams"] == {"chunk_target_chars": 800}

    def test_chunk_params_validation(self, kb_client):
        """422 拒非法:非对象/非整数/越界/未知键。"""
        client, _ = kb_client
        base = {"name": "校验库", "template": "general"}
        assert client.post("/api/packs/knowledge_graph/kbs",
                           json={**base, "chunk_params": "x"}).status_code == 422
        assert client.post("/api/packs/knowledge_graph/kbs",
                           json={**base, "chunk_params": {"chunk_target_chars": "abc"}}).status_code == 422
        assert client.post("/api/packs/knowledge_graph/kbs",
                           json={**base, "chunk_params": {"chunk_target_chars": 99999}}).status_code == 422
        assert client.post("/api/packs/knowledge_graph/kbs",
                           json={**base, "chunk_params": {"unknown_key": 1}}).status_code == 422


# ── 导入按库级参数切块(解析链:库级 > 插件设置 > 默认) ───────

class TestImportUsesKbParams:

    def test_kb_params_override_pack_settings(self, env):
        """库级参数覆盖插件设置:pack 设 target=100,库级 600——
        导入切块按 600 落(不覆盖则按 pack 的 100)。"""
        kb = env.store.create_kb("库级参数导入库", schema_json={"relation_types": []},
                                 chunk_params={"chunk_target_chars": 600,
                                               "chunk_overlap_chars": 0})
        text = "。".join(f"第{i}句内容文字" for i in range(300)) + "。"
        doc = env.store.create_document(
            kb["id"], "params.md", "text/markdown", len(text.encode()), "", "hp1")
        p = env.tmp_path / "files" / kb["id"]
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"{doc['id']}.md"
        f.write_text(text, encoding="utf-8")
        env.store.update_document(doc["id"], file_path=str(f))

        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        chunks = env.store.list_chunks(doc["id"])
        assert len(chunks) >= 2
        # 库级 600 生效(pack 是 100):块大小显著大于 pack 口径
        assert max(c["charCount"] for c in chunks) >= 400

    def test_create_kb_prefills_local_dim(self, kb_client):
        """本地 bge-m3 定死能力:建库即写 dim=1024,不再"待导入探测"。
        (m3 替身注入了 embed_model 属性,走同源口径不会误触一致性降级)"""
        c, env = kb_client
        r = c.post("/api/packs/knowledge_graph/kbs",
                   json={"name": "dim预填库", "template": "general"})
        assert r.status_code == 200, r.text
        kb = r.json()
        assert kb["vectorDim"] == 1024
        assert kb["embeddingModel"] in ("bge-m3", "mock-embed")   # 同源口径

    def test_kb_without_params_falls_back_to_pack(self, env):
        """未配置库级参数回退插件设置(pack=100,块小)。"""
        kb = env.store.create_kb("回退库", schema_json={"relation_types": []})
        text = "。".join(f"第{i}句内容文字" for i in range(300)) + "。"
        doc = env.store.create_document(
            kb["id"], "fallback.md", "text/markdown", len(text.encode()), "", "hp2")
        p = env.tmp_path / "files" / kb["id"]
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{doc['id']}.md").write_text(text, encoding="utf-8")
        env.store.update_document(doc["id"], file_path=str(p / f"{doc['id']}.md"))

        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        chunks = env.store.list_chunks(doc["id"])
        # pack target=100:块贴着小口径(与上一用例的 600 形成对照)
        assert max(c["charCount"] for c in chunks) <= 300
