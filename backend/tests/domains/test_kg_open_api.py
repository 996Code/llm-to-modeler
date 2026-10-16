"""D25 知识库开放 API —— 宿主推数据(带 API Key 鉴权/幂等/更新重导/删除)。

覆盖:Key 签发/清单/吊销(管理门);开放端点鉴权(有效 Key 通过/无效
401/吊销后 401/缺 Key 401);推送幂等(同内容跳过);更新强制重导;
删除联动清理;AuthMiddleware 对 /api/open/ 前缀放行(ADMIN_TOKEN 模式
下无 Bearer 也可达,由 API Key 独自设门)。
"""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router
from domains.knowledge_graph.open_api import router as open_router

# 复用 m3 的导入流水线环境(真实 store + TaskManager + fake 图/向量)
from tests.domains.test_knowledge_graph_m3 import _wait, env  # noqa: F401


@pytest.fixture()
def client(env, monkeypatch):
    """管理端 + 开放 API 双路由的测试客户端。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.include_router(open_router, prefix="/api/open")
    app.state.llm_client = env.llm
    app.state.settings_store = env.settings
    app.state.task_manager = env.manager
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    monkeypatch.setattr(runtime, "get_graph", lambda state: env.graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: env.vector)
    return TestClient(app), env


def _issue_key(client, name="宿主A") -> str:
    c, _ = client
    r = c.post("/api/packs/knowledge_graph/kbs/open-api-keys", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _md(name: str, content: str):
    return {"file": (name, io.BytesIO(content.encode("utf-8")), "text/markdown")}


def _make_kb(env, name="开放库") -> str:
    kb = env.store.create_kb(name, schema_json={"relation_types": []})
    return {"id": kb["id"], "name": kb["name"], "description": "",
            "schema": {"relation_types": []}, "vectorEnabled": True, "vectorDim": 3}


class TestApiKeyManagement:

    def test_issue_list_revoke(self, client):
        """签发(明文仅一次)→ 清单(无哈希/明文)→ 吊销生效。"""
        c, _ = client
        key = _issue_key(client)
        assert key.startswith("kgk_") and len(key) > 40

        items = c.get("/api/packs/knowledge_graph/kbs/open-api-keys").json()["items"]
        assert len(items) == 1
        assert "key" not in items[0] and "keyHash" not in items[0]
        assert items[0]["name"] == "宿主A"

        assert c.delete(f"/api/packs/knowledge_graph/kbs/open-api-keys/"
                        f"{items[0]['id']}").status_code == 200
        # 重复吊销 → 404
        assert c.delete(f"/api/packs/knowledge_graph/kbs/open-api-keys/"
                        f"{items[0]['id']}").status_code == 404

    def test_key_routes_not_shadowed_by_kb_id(self, client):
        """/kbs/open-api-keys 固定段不被 /kbs/{kb_id} 路径参数吞掉。"""
        c, _ = client
        r = c.get("/api/packs/knowledge_graph/kbs/open-api-keys")
        assert r.status_code == 200 and "items" in r.json()


class TestOpenPush:

    def test_push_with_valid_key_triggers_import(self, client):
        """有效 Key 推送:文档落库 + 导入任务发起(全流程)。"""
        c, env = client
        kb = _make_kb(env)
        key = _issue_key(client)
        r = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files=_md("host.md", "[E:甲] [E:乙]"),
                   headers={"X-Api-Key": key})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skipped"] is False
        assert body["document"]["filename"] == "host.md"
        task_id = body["task"]["id"]
        t = _wait(env.manager, task_id)
        assert t["status"] == "succeeded"
        assert env.store.list_chunks(body["document"]["id"])

    def test_push_idempotent_same_content(self, client):
        """幂等:同库同内容再推 → skipped,不建新文档不重导。"""
        c, env = client
        kb = _make_kb(env, "幂等库")
        key = _issue_key(client)
        content = "[E:甲] 内容固定"
        first = c.post(f"/api/open/kb/{kb['id']}/documents",
                       files=_md("a.md", content), headers={"X-Api-Key": key}).json()
        _wait(env.manager, first["task"]["id"])

        again = c.post(f"/api/open/kb/{kb['id']}/documents",
                       files=_md("b.md", content), headers={"X-Api-Key": key})
        assert again.status_code == 200
        body = again.json()
        assert body["skipped"] is True
        assert body["document"]["id"] == first["document"]["id"]
        assert "task" not in body          # 不重复触发导入
        assert len(env.store.list_documents(kb["id"])) == 1

    def test_push_rejects_bad_key(self, client):
        """鉴权:缺 Key/无效 Key/吊销 Key 均 401。"""
        c, env = client
        kb = _make_kb(env, "鉴权库")
        r = c.post(f"/api/open/kb/{kb['id']}/documents", files=_md("x.md", "内容"))
        assert r.status_code == 401

        r = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files=_md("x.md", "内容"), headers={"X-Api-Key": "kgk_bogus"})
        assert r.status_code == 401

        key = _issue_key(client)
        key_id = c.get("/api/packs/knowledge_graph/kbs/open-api-keys"
                       ).json()["items"][0]["id"]
        c.delete(f"/api/packs/knowledge_graph/kbs/open-api-keys/{key_id}")
        r = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files=_md("x.md", "内容"), headers={"X-Api-Key": key})
        assert r.status_code == 401 and "吊销" in r.json()["detail"]

    def test_push_rejects_bad_file(self, client):
        """坏文件(扩展名/解析)当场 422,不进导入队列。"""
        c, env = client
        kb = _make_kb(env, "坏文件库")
        key = _issue_key(client)
        r = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
                   headers={"X-Api-Key": key})
        assert r.status_code == 422

    def test_push_unknown_kb_404(self, client):
        c, _ = client
        key = _issue_key(client)
        r = c.post("/api/open/kb/nope/documents", files=_md("x.md", "内容"),
                   headers={"X-Api-Key": key})
        assert r.status_code == 404


class TestOpenUpdateAndDelete:

    def test_update_forces_reimport(self, client):
        """PUT 更新内容 → 强制重导(新内容切块重建)。"""
        c, env = client
        kb = _make_kb(env, "更新库")
        key = _issue_key(client)
        first = c.post(f"/api/open/kb/{kb['id']}/documents",
                       files=_md("doc.md", "[E:甲] 旧内容"),
                       headers={"X-Api-Key": key}).json()
        _wait(env.manager, first["task"]["id"])
        doc_id = first["document"]["id"]

        upd = c.put(f"/api/open/kb/{kb['id']}/documents/{doc_id}",
                    files=_md("doc.md", "[E:丙] [E:丁] 新内容完全不同"),
                    headers={"X-Api-Key": key})
        assert upd.status_code == 200, upd.text
        t = _wait(env.manager, upd.json()["task"]["id"])
        assert t["status"] == "succeeded"
        chunks = env.store.list_chunks(doc_id)
        assert any("新内容" in c_["text"] for c_ in chunks)

    def test_update_same_content_as_other_doc_409(self, client):
        """更新成与另一文档相同内容 → 409(防内容撞车)。"""
        c, env = client
        kb = _make_kb(env, "撞车库")
        key = _issue_key(client)
        a = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files=_md("a.md", "内容A"), headers={"X-Api-Key": key}).json()
        b = c.post(f"/api/open/kb/{kb['id']}/documents",
                   files=_md("b.md", "内容B"), headers={"X-Api-Key": key}).json()
        r = c.put(f"/api/open/kb/{kb['id']}/documents/{b['document']['id']}",
                  files=_md("b.md", "内容A"), headers={"X-Api-Key": key})
        assert r.status_code == 409

    def test_delete_cleans_up(self, client):
        """DELETE:文档+块删除(与管理端同一条清理实现)。"""
        c, env = client
        kb = _make_kb(env, "删除库")
        key = _issue_key(client)
        first = c.post(f"/api/open/kb/{kb['id']}/documents",
                       files=_md("gone.md", "[E:甲] 待删"),
                       headers={"X-Api-Key": key}).json()
        doc_id = first["document"]["id"]
        _wait(env.manager, first["task"]["id"])

        r = c.delete(f"/api/open/kb/{kb['id']}/documents/{doc_id}",
                     headers={"X-Api-Key": key})
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert not env.store.get_document(doc_id)
        assert not env.store.list_chunks(doc_id)


class TestAuthMiddlewareOpenPrefix:
    """ADMIN_TOKEN 模式下 /api/open/ 前缀放行(API Key 独自设门)。"""

    def test_open_prefix_bypasses_bearer(self, client, monkeypatch):
        c, env = client
        monkeypatch.setenv("ADMIN_TOKEN", "t")
        kb = _make_kb(env, "白名单库")
        # 不带 Bearer:中间件放行,由 API Key 依赖接管(此处无 Key → 401
        # 而非中间件的 invalid_token)
        r = c.post(f"/api/open/kb/{kb['id']}/documents", files=_md("x.md", "内容"))
        assert r.status_code == 401
        assert "API Key" in r.json()["detail"]      # 是 Key 门的 401,不是 token 门
