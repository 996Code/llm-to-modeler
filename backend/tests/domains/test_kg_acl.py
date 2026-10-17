"""知识库检索数据权限(嵌入模式)——ACL 表/判定/管理端点/两处检索生效点。

覆盖:身份归一(anonymous/未传/list/逗号串)、判定语义(未传不过滤/
无 ACL 开放/user 命中/role 命中/双 miss 拒)、store 全量替换、
kb_search 工具(显式拒绝/跨库过滤/记忆失效/无身份回归)、
/search REST(roles 体参/显式 403/数组静默跳过/默认跨库过滤)。
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import acl, runtime
from domains.knowledge_graph.api import router as kg_router
from domains.knowledge_graph.tools.kb_search import KbSearchTool
from sdk.tool import ToolContext

# 复用 m4 的检索 fake 环境(MockLLM/RetrievalFakeGraph/RetrievalFakeVector)
from tests.domains.test_knowledge_graph_m4 import _ctx, env  # noqa: F401


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    return TestClient(app), env


def _ctx_with_user(env, user="anonymous", pack_params=None):
    """带用户身份与 pack_params 的工具上下文。"""
    c = _ctx(env)
    c = ToolContext(llm_client=c.llm_client, asset_client=None, conversation=None,
                    emit=lambda *a, **k: None, conv_id="conv-acl",
                    session_state=None, user_id=user)
    return c


# ── 判定纯函数 ─────────────────────────────────────────────

class TestRequestIdentity:

    def test_anonymous_and_empty_mean_no_identity(self):
        assert acl.request_identity("", None) == ("", [])
        assert acl.request_identity("anonymous", None) == ("", [])
        assert acl.request_identity(None, []) == ("", [])
        assert not acl.has_identity(*acl.request_identity("anonymous", []))

    def test_roles_normalization(self):
        assert acl.request_identity("u1", ["法务", " 管理员 ", ""]) == ("u1", ["法务", "管理员"])
        assert acl.request_identity("u1", "法务,管理员") == ("u1", ["法务", "管理员"])
        assert acl.request_identity("u1", "  ") == ("u1", [])
        assert acl.has_identity("", ["法务"])       # 只有角色也算有身份
        assert acl.has_identity("u1", [])


# ── store ACL ─────────────────────────────────────────────

class TestStoreAcl:

    def test_set_get_full_replace(self, env):
        kb = env.store.create_kb("ACL库")
        env.store.set_kb_acl(kb["id"], ["u1", "u2"], ["法务"])
        assert env.store.get_kb_acl(kb["id"]) == {"users": ["u1", "u2"], "roles": ["法务"]}
        # 全量替换(不是追加)
        env.store.set_kb_acl(kb["id"], ["u3"], [])
        assert env.store.get_kb_acl(kb["id"]) == {"users": ["u3"], "roles": []}
        # 清空 = 回到开放
        env.store.set_kb_acl(kb["id"], [], [])
        assert env.store.get_kb_acl(kb["id"]) == {"users": [], "roles": []}

    def test_get_all_kb_acl(self, env):
        k1 = env.store.create_kb("全量1")
        k2 = env.store.create_kb("全量2")
        env.store.set_kb_acl(k1["id"], ["u1"], ["法务"])
        all_acl = env.store.get_all_kb_acl()
        assert all_acl[k1["id"]] == [
            {"subjectType": "user", "subjectId": "u1"},
            {"subjectType": "role", "subjectId": "法务"}]
        assert k2["id"] not in all_acl          # 未配置的库不在 map


# ── 判定语义 ─────────────────────────────────────────────

class TestFilterSemantics:

    def _kbs(self, env):
        return [env.store.create_kb(f"权限库{i}") for i in range(3)]

    def test_no_identity_no_filter(self, env):
        """未传身份:配了 ACL 也不过滤(独立模式行为不变)。"""
        kbs = self._kbs(env)
        env.store.set_kb_acl(kbs[0]["id"], ["别人"], [])
        out, denied = acl.filter_kbs(env.store, kbs, "", [])
        assert len(out) == 3 and not denied

    def test_no_acl_open(self, env):
        """库未配置 ACL:全放行(默认开放)。"""
        kbs = self._kbs(env)
        out, denied = acl.filter_kbs(env.store, kbs, "u1", ["角色A"])
        assert len(out) == 3 and not denied

    def test_user_hit_and_role_hit(self, env):
        kbs = self._kbs(env)
        env.store.set_kb_acl(kbs[0]["id"], ["u1"], [])          # 用户命中
        env.store.set_kb_acl(kbs[1]["id"], [], ["法务"])          # 角色命中
        env.store.set_kb_acl(kbs[2]["id"], ["别人"], ["其它"])    # 双 miss
        out, denied = acl.filter_kbs(env.store, kbs, "u1", ["法务", "无关"])
        assert [k["name"] for k in out] == [kbs[0]["name"], kbs[1]["name"]]
        assert denied == [kbs[2]["name"]]

    def test_kb_allowed_single(self, env):
        kb = env.store.create_kb("单判库")
        assert acl.kb_allowed(env.store, kb["id"], "u1", []) is True   # 无 ACL
        env.store.set_kb_acl(kb["id"], [], ["法务"])
        assert acl.kb_allowed(env.store, kb["id"], "u1", []) is False
        assert acl.kb_allowed(env.store, kb["id"], "", ["法务"]) is True
        assert acl.kb_allowed(env.store, kb["id"], "", []) is True     # 无身份短路


# ── 管理端点 ─────────────────────────────────────────────

class TestAclEndpoints:

    def test_get_put_acl(self, client):
        c, env = client
        kb = env.store.create_kb("端点库")
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl",
                  json={"users": ["u1", "u1", " u2 "], "roles": ["法务"]})
        assert r.status_code == 200, r.text
        assert r.json() == {"users": ["u1", "u2"], "roles": ["法务"]}  # 去重去空
        body = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl").json()
        assert (body["users"], body["roles"]) == (["u1", "u2"], ["法务"])
        assert body["options"] == {"users": [], "roles": []}   # 未同步字典=空选项
        # 清空回到开放
        assert c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl",
                     json={"users": [], "roles": []}).status_code == 200
        body = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl").json()
        assert (body["users"], body["roles"]) == ([], [])

    def test_subjects_sync_and_options(self, client):
        """宿主同步字典:全量替换+GET /acl 的 options 带出候选。"""
        c, env = client
        kb = env.store.create_kb("字典库")
        r = c.post("/api/packs/knowledge_graph/subjects/user",
                   json={"items": [{"id": "u1", "name": "张三"}, {"id": "u2"}]})
        assert r.status_code == 200 and r.json()["count"] == 2
        c.post("/api/packs/knowledge_graph/subjects/role",
               json={"items": [{"id": "法务"}, {"id": "管理员", "name": "系统管理员"}]})
        body = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl").json()
        assert body["options"]["users"] == [
            {"id": "u1", "name": "张三"}, {"id": "u2", "name": "u2"}]
        assert body["options"]["roles"] == [
            {"id": "法务", "name": "法务"}, {"id": "管理员", "name": "系统管理员"}]
        # 再同步(全量替换):u2 消失、u3 出现——宿主删人字典跟着走
        c.post("/api/packs/knowledge_graph/subjects/user",
               json={"items": [{"id": "u1", "name": "张三"}, {"id": "u3"}]})
        body = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl").json()
        assert [u["id"] for u in body["options"]["users"]] == ["u1", "u3"]
        assert c.post("/api/packs/knowledge_graph/subjects/bogus",
                      json={"items": []}).status_code == 422

    def test_put_validation(self, client):
        c, env = client
        kb = env.store.create_kb("校验ACL库")
        assert c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/acl",
                     json={"users": "u1"}).status_code == 422
        assert c.get("/api/packs/knowledge_graph/kbs/nope/acl").status_code == 404


# ── kb_search 工具生效点 ───────────────────────────────────

class TestKbSearchAcl:

    def _mk_kbs(self, env, n=3):
        return [env.store.create_kb(f"检索权限库{i}") for i in range(n)]

    def test_explicit_denied_honest_reply(self, env):
        """显式指定但无权:诚实拒绝(不静默空结果)。"""
        kbs = self._mk_kbs(env, 1)
        env.store.set_kb_acl(kbs[0]["id"], ["别人"], [])
        tool = KbSearchTool(env.app_state)
        result = tool.execute(
            {"user_input": "甲在哪?", "kb": kbs[0]["name"]},
            _ctx_with_user(env, user="u1"))
        assert result.reply and "没有访问知识库" in result.reply
        assert result.artifact is None

    def test_cross_search_filtered_by_roles(self, env):
        """跨库默认:角色过滤后只检索有权的库。"""
        kbs = self._mk_kbs(env, 3)
        env.store.set_kb_acl(kbs[0]["id"], [], ["法务"])       # 角色命中
        # 库1 无配置=开放;库2 拒
        env.store.set_kb_acl(kbs[2]["id"], ["别人"], ["其它"])
        tool = KbSearchTool(env.app_state)
        result = tool.execute(
            {"user_input": "甲在哪?",
             "pack_params": {"knowledge_graph": {"user_roles": ["法务"]}}},
            _ctx_with_user(env, user="u9"))
        assert result.artifact and result.artifact["type"] == "kg_search_result"
        assert {k["name"] for k in result.artifact["kbs"]} == \
            {kbs[0]["name"], kbs[1]["name"]}

    def test_all_denied_friendly_reply(self, env):
        kbs = self._mk_kbs(env, 2)
        for kb in kbs:
            env.store.set_kb_acl(kb["id"], ["别人"], [])
        tool = KbSearchTool(env.app_state)
        result = tool.execute(
            {"user_input": "甲在哪?", "pack_params": {"knowledge_graph": {"user_roles": ["法务"]}}},
            _ctx_with_user(env, user="u1"))
        assert result.reply and "没有您可访问的知识库" in result.reply

    def test_no_identity_unaffected(self, env):
        """无身份(独立模式):配了 ACL 也照常检索(回归锚)。"""
        kbs = self._mk_kbs(env, 2)
        env.store.set_kb_acl(kbs[0]["id"], ["别人"], [])
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "甲在哪?"}, _ctx(env))
        assert result.artifact and len(result.artifact["kbs"]) == 2


# ── /search REST 生效点 ────────────────────────────────────

class TestSearchRestAcl:

    def test_roles_filter_default_cross(self, client):
        """默认跨库 + roles 体参:只检索有权的库。"""
        c, env = client
        k1 = env.store.create_kb("REST开放库")
        k2 = env.store.create_kb("REST受限库")
        env.store.set_kb_acl(k2["id"], ["别人"], [])
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "甲在哪?", "roles": ["法务"]},
                   headers={"X-User-Id": "u1"})
        assert r.status_code == 200, r.text
        assert {k["name"] for k in r.json()["kbs"]} == {k1["name"]}

    def test_explicit_denied_403(self, client):
        c, env = client
        kb = env.store.create_kb("REST拒绝库")
        env.store.set_kb_acl(kb["id"], ["别人"], [])
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "甲在哪?", "kb": kb["name"], "roles": ["法务"]},
                   headers={"X-User-Id": "u1"})
        assert r.status_code == 403

    def test_array_silently_skips_denied(self, client):
        """数组指定:无权的库与不存在的名字同款静默跳过(防枚举一致)。"""
        c, env = client
        k_ok = env.store.create_kb("REST数组OK")
        k_no = env.store.create_kb("REST数组拒")
        env.store.set_kb_acl(k_no["id"], ["别人"], [])
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "甲在哪?", "kb": [k_no["name"], k_ok["name"], "不存在"],
                         "roles": ["法务"]},
                   headers={"X-User-Id": "u1"})
        assert r.status_code == 200
        assert {k["name"] for k in r.json()["kbs"]} == {k_ok["name"]}

    def test_no_identity_open(self, client):
        """无身份:配了 ACL 也照常(REST 回归锚)。"""
        c, env = client
        kb = env.store.create_kb("REST无身份库")
        env.store.set_kb_acl(kb["id"], ["别人"], [])
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "甲在哪?", "kb": kb["name"]})
        assert r.status_code == 200
