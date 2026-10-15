"""知识库层级树（用户 10-15 需求:左侧需要是有层级的树）——目录+子库+树查询。

- kind 列: 'kb'(默认,存量兼容) | 'folder'(目录,不存文档只做分组)
- parent_id: NULL=顶层;指向 folder id=子项
- 循环引用防护:parent 必须是 folder 且不得形成环
"""
import pytest
from types import SimpleNamespace

from domains.knowledge_graph.store import KGStore


class TestKbHierarchy:

    def test_create_folder(self):
        s = KGStore()
        f = s.create_kb("法务目录", kind="folder")
        assert f["kind"] == "folder"
        assert f["parentId"] is None

    def test_create_kb_under_folder(self):
        s = KGStore()
        f = s.create_kb("目录甲", kind="folder")
        kb = s.create_kb("劳动法库", parent_id=f["id"])
        assert kb["parentId"] == f["id"]
        assert kb["kind"] == "kb"

    def test_kb_tree_returns_nested(self):
        s = KGStore()
        f = s.create_kb("树目录", kind="folder")
        s.create_kb("子库一", parent_id=f["id"])
        s.create_kb("子库二", parent_id=f["id"])
        s.create_kb("顶层散库")
        tree = s.list_kb_tree()
        # 顶层含目录与散库;目录下挂两个子库
        top_names = {t["name"] for t in tree}
        assert "树目录" in top_names and "顶层散库" in top_names
        folder = next(t for t in tree if t["name"] == "树目录")
        assert {c["name"] for c in folder["children"]} == {"子库一", "子库二"}

    def test_parent_must_be_folder(self):
        s = KGStore()
        kb = s.create_kb("普通库")
        with pytest.raises(ValueError, match="folder"):
            s.create_kb("挂错爹", parent_id=kb["id"])

    def test_cycle_rejected(self):
        s = KGStore()
        f1 = s.create_kb("目录一", kind="folder")
        f2 = s.create_kb("目录二", kind="folder", parent_id=f1["id"])
        # 把 f1 挪到 f2 下面 → 环
        with pytest.raises(ValueError, match="环|cycle"):
            s.move_kb(f1["id"], parent_id=f2["id"])

    def test_move_kb_between_folders(self):
        s = KGStore()
        fa = s.create_kb("目录A", kind="folder")
        fb = s.create_kb("目录B", kind="folder")
        kb = s.create_kb("搬家库", parent_id=fa["id"])
        s.move_kb(kb["id"], parent_id=fb["id"])
        assert s.get_kb(kb["id"])["parentId"] == fb["id"]

    def test_legacy_kb_defaults(self):
        """存量库无 kind/parent_id → kind='kb', parentId=None(兼容)。"""
        s = KGStore()
        kb = s.create_kb("存量风格库")   # 不传新参
        assert kb["kind"] == "kb" and kb["parentId"] is None


# ── API 层(TestClient;admin 门走真实链) ──────────────────

from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE = "/api/packs/knowledge_graph"


@pytest.fixture()
def tree_client(tmp_path, monkeypatch):
    """最小应用 + 真实 KGStore(测试 PG 库,conftest 逐测试清表)。

    admin 门确定性:delenv ADMIN_TOKEN = 开放模式(无口令放行);
    门控行为(401)留给专属用例 setenv。树端点不触图/向量,无需 fake。
    """
    monkeypatch.setenv("KG_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from domains.knowledge_graph import runtime
    runtime.reset_runtime_cache()
    from domains.knowledge_graph.api import router
    app = FastAPI()
    app.include_router(router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = None
    app.state.settings_store = None
    yield TestClient(app)
    runtime.reset_runtime_cache()


class TestKbTreeApi:

    def test_tree_endpoint_returns_nested(self, tree_client):
        """GET /kbs/tree:目录带 children 递归,kb 叶子带统计字段。"""
        c = tree_client
        folder = c.post(f"{BASE}/kbs", json={"name": "目录A", "kind": "folder"}).json()
        c.post(f"{BASE}/kbs", json={"name": "子库甲", "parent_id": folder["id"]})
        c.post(f"{BASE}/kbs", json={"name": "顶层散库"})
        r = c.get(f"{BASE}/kbs/tree")
        assert r.status_code == 200, r.text
        tree = r.json()["items"]
        folder_node = next(t for t in tree if t["name"] == "目录A")
        assert folder_node["kind"] == "folder"
        assert {x["name"] for x in folder_node["children"]} == {"子库甲"}
        top = next(t for t in tree if t["name"] == "顶层散库")
        assert top["kind"] == "kb" and top["children"] == []
        assert "docCount" in top and "entityTotal" in top

    def test_tree_endpoint_admin_gated(self, tree_client, monkeypatch):
        """ADMIN_TOKEN 配置时无口令 401——树结构(库名清单)不裸奔。"""
        c = tree_client
        monkeypatch.setenv("ADMIN_TOKEN", "t")
        assert c.get(f"{BASE}/kbs/tree").status_code == 401
        ok = c.get(f"{BASE}/kbs/tree", headers={"X-Admin-Token": "t"})
        assert ok.status_code == 200

    def test_create_folder_via_api(self, tree_client):
        c = tree_client
        r = c.post(f"{BASE}/kbs", json={"name": "法务目录", "kind": "folder"})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "folder" and r.json()["parentId"] is None

    def test_create_kb_under_folder_via_api(self, tree_client):
        c = tree_client
        folder = c.post(f"{BASE}/kbs", json={"name": "目录", "kind": "folder"}).json()
        r = c.post(f"{BASE}/kbs",
                   json={"name": "劳动法库", "parent_id": folder["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["parentId"] == folder["id"]
        assert r.json()["kind"] == "kb"

    def test_create_default_is_plain_kb(self, tree_client):
        """不传 kind/parent_id:默认 kb/顶层(存量契约不变)。"""
        c = tree_client
        r = c.post(f"{BASE}/kbs", json={"name": "普通库"})
        assert r.status_code == 200
        assert r.json()["kind"] == "kb" and r.json()["parentId"] is None

    def test_create_parent_must_be_folder_422(self, tree_client):
        c = tree_client
        kb = c.post(f"{BASE}/kbs", json={"name": "普通库"}).json()
        r = c.post(f"{BASE}/kbs",
                   json={"name": "挂错爹", "parent_id": kb["id"]})
        assert r.status_code == 422

    def test_create_unknown_parent_422(self, tree_client):
        c = tree_client
        r = c.post(f"{BASE}/kbs", json={"name": "幽灵子", "parent_id": "nope"})
        assert r.status_code == 422

    def test_create_invalid_kind_422(self, tree_client):
        c = tree_client
        r = c.post(f"{BASE}/kbs", json={"name": "怪东西", "kind": "tag"})
        assert r.status_code == 422

    def test_move_kb_between_folders_via_api(self, tree_client):
        c = tree_client
        fa = c.post(f"{BASE}/kbs", json={"name": "目录A", "kind": "folder"}).json()
        fb = c.post(f"{BASE}/kbs", json={"name": "目录B", "kind": "folder"}).json()
        kb = c.post(f"{BASE}/kbs",
                    json={"name": "搬家库", "parent_id": fa["id"]}).json()
        r = c.put(f"{BASE}/kbs/{kb['id']}/move", json={"parent_id": fb["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["parentId"] == fb["id"]
        # 提顶层:parent_id=null
        r2 = c.put(f"{BASE}/kbs/{kb['id']}/move", json={"parent_id": None})
        assert r2.status_code == 200 and r2.json()["parentId"] is None

    def test_move_kb_cycle_422(self, tree_client):
        c = tree_client
        f1 = c.post(f"{BASE}/kbs", json={"name": "目录一", "kind": "folder"}).json()
        f2 = c.post(f"{BASE}/kbs",
                    json={"name": "目录二", "kind": "folder",
                          "parent_id": f1["id"]}).json()
        r = c.put(f"{BASE}/kbs/{f1['id']}/move", json={"parent_id": f2["id"]})
        assert r.status_code == 422

    def test_move_kb_to_non_folder_422(self, tree_client):
        c = tree_client
        kb = c.post(f"{BASE}/kbs", json={"name": "普通库"}).json()
        other = c.post(f"{BASE}/kbs", json={"name": "另一个库"}).json()
        r = c.put(f"{BASE}/kbs/{kb['id']}/move",
                  json={"parent_id": other["id"]})
        assert r.status_code == 422

    def test_move_kb_unknown_404(self, tree_client):
        c = tree_client
        r = c.put(f"{BASE}/kbs/不存在/move", json={"parent_id": None})
        assert r.status_code == 404

    def test_move_kb_admin_gated(self, tree_client, monkeypatch):
        c = tree_client
        monkeypatch.setenv("ADMIN_TOKEN", "t")
        r = c.put(f"{BASE}/kbs/任意/move", json={"parent_id": None})
        assert r.status_code == 401

    def test_delete_folder_with_children_422(self, tree_client):
        """目录删除守卫:有子节点 422(删了子节点 parentId 悬空成孤儿)。"""
        c = tree_client
        folder = c.post(f"{BASE}/kbs", json={"name": "目录", "kind": "folder"}).json()
        c.post(f"{BASE}/kbs", json={"name": "子库", "parent_id": folder["id"]})
        r = c.delete(f"{BASE}/kbs/{folder['id']}")
        assert r.status_code == 422
        assert "子节点" in r.json()["detail"]

    def test_delete_empty_folder_ok(self, tree_client):
        c = tree_client
        folder = c.post(f"{BASE}/kbs", json={"name": "空目录", "kind": "folder"}).json()
        r = c.delete(f"{BASE}/kbs/{folder['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        assert c.get(f"{BASE}/kbs/{folder['id']}").status_code == 404


class TestDefaultKb:
    def test_start_creates_default_kb_when_empty(self, monkeypatch):
        """零库启动 → 自动建「默认知识库」(用户 10-15 建议:开箱即用)。"""
        from domains.knowledge_graph import pack as kg_pack, runtime
        kg_pack._RECOVERY_DONE = False
        store = KGStore()
        assert not store.list_kbs()   # 前置:空
        app_state = SimpleNamespace()
        monkeypatch.setattr(runtime, "get_kg_store", lambda s: store)
        kg_pack.start(app_state, task_manager=object())
        names = [k["name"] for k in store.list_kbs()]
        assert "默认知识库" in names

    def test_start_skips_when_kbs_exist(self, monkeypatch):
        """已有库(含 folder) → 不建默认库(不干扰用户结构)。"""
        from domains.knowledge_graph import pack as kg_pack, runtime
        kg_pack._RECOVERY_DONE = False
        store = KGStore()
        store.create_kb("用户已有库")
        app_state = SimpleNamespace()
        monkeypatch.setattr(runtime, "get_kg_store", lambda s: store)
        kg_pack.start(app_state, task_manager=object())
        names = [k["name"] for k in store.list_kbs()]
        assert "默认知识库" not in names and names == ["用户已有库"]

    def test_start_recreates_default_when_all_deleted(self, monkeypatch):
        """删光所有库后重启 → 默认库再现(零库即建,不区分"从未建过"与
        "删光了"——语义简化:想保留空态就至少留一个库)。"""
        from domains.knowledge_graph import pack as kg_pack, runtime
        kg_pack._RECOVERY_DONE = False
        store = KGStore()
        app_state = SimpleNamespace()
        monkeypatch.setattr(runtime, "get_kg_store", lambda s: store)
        kg_pack.start(app_state, task_manager=object())
        default = next(k for k in store.list_kbs() if k["name"] == "默认知识库")
        store.delete_kb(default["id"])
        assert store.list_kbs() == []          # 删光
        kg_pack._RECOVERY_DONE = False
        kg_pack.start(app_state, task_manager=object())
        names = [k["name"] for k in store.list_kbs()]
        assert names == ["默认知识库"]          # 零库即建
