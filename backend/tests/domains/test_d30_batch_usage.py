"""D30 批量删除 + 使用检查(块命中聚合)。

批量:逐条走单删同实现(导入中守卫 409 如实进结果不中断整批)、
空参 422、上限 50。使用检查:按文档聚合 D29 hit_count,零命中文档
也列出(治理信号),按命中总量降序。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router

from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    monkeypatch.setattr(runtime, "get_graph", lambda state: env.graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: env.vector)
    return TestClient(app), env


def _mk_docs(env, n=3):
    kb = env.store.create_kb("D30测试库", "")
    ids = []
    for i in range(n):
        doc = env.store.create_document(kb["id"], f"d{i}.txt", "text/plain", 5,
                                        "/dev/null", f"h-d30-{i}")
        env.store.replace_chunks(doc["id"], kb["id"],
                                 [{"seq": 0, "text": f"文档{i}内容"}])
        ids.append(doc["id"])
    return kb, ids


class TestBatchDelete:

    def test_批量删除成功(self, client):
        c, env = client
        kb, ids = _mk_docs(env, 3)
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/batch-delete",
                   json={"doc_ids": ids})
        assert r.status_code == 200
        d = r.json()
        assert d["deleted"] == 3 and d["failed"] == 0
        assert all(x["ok"] for x in d["results"])

    def test_部分失败不中断(self, client):
        c, env = client
        kb, ids = _mk_docs(env, 2)
        # 混入一个不存在的 id:404 进结果,其余照删
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/batch-delete",
                   json={"doc_ids": [ids[0], "no-such-id", ids[1]]})
        d = r.json()
        assert d["deleted"] == 2 and d["failed"] == 1
        assert not d["results"][1]["ok"]

    def test_空参422(self, client):
        c, env = client
        kb, _ = _mk_docs(env, 1)
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/batch-delete",
                   json={"doc_ids": []})
        assert r.status_code == 422

    def test_超上限422(self, client):
        c, env = client
        kb, _ = _mk_docs(env, 1)
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/batch-delete",
                   json={"doc_ids": [f"x{i}" for i in range(51)]})
        assert r.status_code == 422


class TestUsage:

    def test_命中聚合排序(self, client):
        c, env = client
        kb, ids = _mk_docs(env, 2)
        # 文档0 命中 3 次(三次独立调用;同一 UPDATE 里重复 id 只 +1 是
        # SQL 正确语义),文档1 零命中
        cid = env.store.list_chunks(ids[0])[0]["id"]
        for _ in range(3):
            env.store.bump_chunk_hits([cid])
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/usage")
        assert r.status_code == 200
        items = r.json()["items"]
        assert items[0]["docId"] == ids[0]
        assert items[0]["hitTotal"] == 3 and items[0]["hitChunks"] == 1
        # 零命中文档也在列(治理信号)
        assert any(x["docId"] == ids[1] and x["hitTotal"] == 0 for x in items)
