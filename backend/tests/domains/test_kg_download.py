"""知识库文档下载端点 —— 成功/404 三态/路径越界防御。"""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router

# 复用 m4 检索 fake 环境(KG_FILES_DIR 已指 tmp)
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


def _mk_doc_with_file(env, filename="规程.pdf", content=b"PDF-bytes-123"):
    kb = env.store.create_kb("下载测试库")
    doc = env.store.create_document(
        kb["id"], filename, "application/pdf", len(content), "",
        hashlib.sha256(content).hexdigest())
    doc_dir = runtime.files_root() / kb["id"]
    doc_dir.mkdir(parents=True, exist_ok=True)
    path = doc_dir / f"{doc['id']}.pdf"
    path.write_bytes(content)
    env.store.update_document(doc["id"], file_path=str(path))
    return kb, doc, content


class TestDownloadDocument:

    def test_download_ok(self, client):
        c, env = client
        kb, doc, content = _mk_doc_with_file(env, "JJG 563-2004 检定规程.pdf")
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/download")
        assert r.status_code == 200
        assert r.content == content
        assert r.headers["content-type"].startswith("application/pdf")
        # 中文文件名走 RFC 5987(filename*),原始名不丢
        assert "JJG" in r.headers.get("content-disposition", "")

    def test_doc_not_found(self, client):
        c, env = client
        kb = env.store.create_kb("空库")
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/nope/download")
        assert r.status_code == 404

    def test_file_missing_on_disk(self, client):
        c, env = client
        kb, doc, _ = _mk_doc_with_file(env)
        # 模拟文件被清理:磁盘删除但元数据还在
        import os
        os.remove(env.store.document_file_path(doc["id"]))
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/download")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_path_outside_files_root_rejected(self, client, tmp_path):
        c, env = client
        kb, doc, _ = _mk_doc_with_file(env)
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"evil")
        env.store.update_document(doc["id"], file_path=str(outside))
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/download")
        assert r.status_code == 404

    def test_no_file_path(self, client):
        c, env = client
        kb = env.store.create_kb("无文件库")
        doc = env.store.create_document(
            kb["id"], "ghost.md", "text/markdown", 0, "", hashlib.sha256(b"x").hexdigest())
        r = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/download")
        assert r.status_code == 404

    def test_token_mode_requires_login_not_admin(self, env, monkeypatch, tmp_path):
        """token 模式:全局中间件要求 Authorization(登录即可下载);
        端点本身不再要求管理员头——对话用户引用下载走同一门。"""
        monkeypatch.setenv("ADMIN_TOKEN", "secret")
        from api.auth import AuthMiddleware, router as auth_router

        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        app.include_router(auth_router)
        app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
        app.state.llm_client = env.llm
        app.state.settings_store = None
        monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
        monkeypatch.setattr(runtime, "get_graph", lambda state: env.graph)
        monkeypatch.setattr(runtime, "get_vector", lambda state: env.vector)
        c = TestClient(app)

        kb, doc, content = _mk_doc_with_file(env, "引用来源.pdf")
        url = f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents/{doc['id']}/download"
        assert c.get(url).status_code == 401          # 无 token:全局中间件拦
        token = c.post("/api/auth/token", json={"secret": "secret"}).json()["token"]
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.content == content  # 普通登录 token 即可
