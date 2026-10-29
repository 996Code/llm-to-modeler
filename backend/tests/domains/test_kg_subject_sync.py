"""知识库用户/角色目录同步接口——全量/增量/CRUD/同步状态。

覆盖:全量替换的 diff 计数与幂等(unchanged 行 updated_at 不假跳)、
增量的合并语义(upsert 缺省字段保现值/删除如实报 missing)、单条
upsert(建/改合一)、列表搜索/过滤/分页、同步状态总览、批量校验
(重复 id/超限/非法字段)。对外契约文档:doc/知识库用户角色同步API.md。
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router
from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401

BASE = "/api/packs/knowledge_graph"


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(kg_router, prefix="/api/packs/knowledge_graph")
    app.state.llm_client = env.llm
    app.state.settings_store = None
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: env.store)
    # MCP 密钥缓存按 id(app.state) 键控,清掉防地址复用读到上一测试的密钥
    from services import mcp_server
    mcp_server._key_cache.clear()
    yield TestClient(app), env
    mcp_server._key_cache.clear()


def _set_mcp_key(key: str):
    from services.platform_settings import (
        PlatformSettingsStore, save_mcp_settings)
    save_mcp_settings(PlatformSettingsStore(), {"api_key": key})


def _full(c, subject_type, items):
    r = c.post(f"{BASE}/subjects/{subject_type}", json={"items": items})
    assert r.status_code == 200, r.text
    return r.json()


# ── 全量同步 ───────────────────────────────────────────────

class TestFullSync:

    def test_first_sync_counts_and_acl_options(self, client):
        c, env = client
        body = _full(c, "user", [
            {"id": "u1", "name": "张三"},
            {"id": "u2"},
        ])
        assert body == {"subjectType": "user", "count": 2, "created": 2,
                        "updated": 0, "unchanged": 0, "removed": 0}
        # ACL 编辑页下拉候选 = 同步进来的活跃目录(向后兼容形状 {id,name})
        kb = env.store.create_kb("全量库")
        options = c.get(f"{BASE}/kbs/{kb['id']}/acl").json()["options"]
        assert options["users"] == [
            {"id": "u1", "name": "张三"}, {"id": "u2", "name": "u2"}]

    def test_full_replace_diff_counts(self, client):
        c, env = client
        _full(c, "user", [
            {"id": "u1", "name": "张三"}, {"id": "u2"}, {"id": "u3"}])
        body = _full(c, "user", [
            {"id": "u1", "name": "张三"},   # 值未变 → unchanged
            {"id": "u2", "name": "李四"},   # 改名 → updated
            {"id": "u4"},                   # 新增 → created
        ])                                  # u3 不在清单 → removed
        assert (body["created"], body["updated"],
                body["unchanged"], body["removed"]) == (1, 1, 1, 1)
        assert c.get(f"{BASE}/subjects/user/u3").status_code == 404
        assert c.get(f"{BASE}/subjects/user/u2").json()["name"] == "李四"

    def test_idempotent_resync_keeps_updated_at(self, client):
        c, env = client
        _full(c, "user", [{"id": "u1", "name": "张三"}])
        t1 = c.get(f"{BASE}/subjects/user/u1").json()["updatedAt"]
        time.sleep(0.02)
        body = _full(c, "user", [{"id": "u1", "name": "张三"}])
        assert body["unchanged"] == 1
        t2 = c.get(f"{BASE}/subjects/user/u1").json()["updatedAt"]
        assert t2 == t1                      # 未变的行不动,时间戳不假跳
        _full(c, "user", [{"id": "u1", "name": "张三丰"}])
        assert c.get(f"{BASE}/subjects/user/u1").json()["updatedAt"] != t1

    def test_full_sync_replaces_status(self, client):
        c, env = client
        _full(c, "user", [{"id": "u1", "name": "张三", "status": "disabled"}])
        # 全量是权威快照:清单里没带的字段回到默认
        _full(c, "user", [{"id": "u1", "name": "张三"}])
        rec = c.get(f"{BASE}/subjects/user/u1").json()
        assert rec["status"] == "active"

    def test_full_sync_validation(self, client, monkeypatch):
        c, env = client
        from domains.knowledge_graph import api as kg_api
        assert c.post(f"{BASE}/subjects/bogus",
                      json={"items": []}).status_code == 422
        assert c.post(f"{BASE}/subjects/user",
                      json={"items": "x"}).status_code == 422
        assert c.post(f"{BASE}/subjects/user",
                      json={"items": [{"name": "无id"}]}).status_code == 422
        assert c.post(f"{BASE}/subjects/user", json={"items": [
            {"id": "u1"}, {"id": "u1"}]}).status_code == 422    # id 重复
        assert c.post(f"{BASE}/subjects/user", json={"items": [
            {"id": "u1", "status": "frozen"}]}).status_code == 422
        # 超限:把上限压到 2 验证 422(不真造 2 万条)
        monkeypatch.setattr(kg_api, "_FULL_SYNC_MAX_ITEMS", 2)
        assert c.post(f"{BASE}/subjects/user", json={
            "items": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        ).status_code == 422
        # 整批校验前置:非法批不落任何行(事务性)
        assert c.get(f"{BASE}/subjects/user/a").status_code == 404


# ── 增量同步 ───────────────────────────────────────────────

class TestDeltaSync:

    def test_delta_merge_and_delete(self, client):
        c, env = client
        _full(c, "user", [
            {"id": "u1", "name": "张三", "status": "disabled"},
            {"id": "u2"}, {"id": "u3"}])
        r = c.post(f"{BASE}/subjects/user/delta", json={
            "upserts": [
                {"id": "u1", "name": "张三丰"},   # 只改名 → status 保留
                {"id": "u4", "name": "新人"},
            ],
            "deletes": ["u3", "u99"],             # u99 不存在 → missing
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["inserted"], body["updated"],
                body["deleted"]) == (1, 1, 1)
        assert body["missing"] == ["u99"]
        rec = c.get(f"{BASE}/subjects/user/u1").json()
        assert rec["name"] == "张三丰"
        assert rec["status"] == "disabled"         # 未提供的字段保持现值
        assert c.get(f"{BASE}/subjects/user/u3").status_code == 404
        assert c.get(f"{BASE}/subjects/user/u4").json()["name"] == "新人"

    def test_delta_disable_exits_acl_options(self, client):
        c, env = client
        _full(c, "user", [{"id": "u1"}, {"id": "u2"}])
        r = c.post(f"{BASE}/subjects/user/delta", json={
            "upserts": [{"id": "u2", "status": "disabled"}]})
        assert r.json()["updated"] == 1
        kb = env.store.create_kb("停用库")
        options = c.get(f"{BASE}/kbs/{kb['id']}/acl").json()["options"]
        assert [u["id"] for u in options["users"]] == ["u1"]  # 停用不进候选

    def test_delta_replay_is_safe(self, client):
        """重放安全:删除已删的 id 进 missing 不报错;upsert 值未变不动行。"""
        c, env = client
        _full(c, "user", [{"id": "u1", "name": "张三"}, {"id": "u2", "name": "李四"}])
        # 删除重放:第二次同删 u1 → 如实 missing,不 500
        first = c.post(f"{BASE}/subjects/user/delta",
                       json={"deletes": ["u1"]}).json()
        assert first["deleted"] == 1 and first["missing"] == []
        second = c.post(f"{BASE}/subjects/user/delta",
                        json={"deletes": ["u1"]}).json()
        assert second["deleted"] == 0 and second["missing"] == ["u1"]
        # upsert 值未变重放:unchanged 计数,不动行
        r1 = c.post(f"{BASE}/subjects/user/delta", json={
            "upserts": [{"id": "u2", "name": "李四"}]}).json()
        assert r1["unchanged"] == 1 and r1["updated"] == 0
        # 已删条目的 upsert 重放:重新插入(增量是快照语义,不丢人)
        r2 = c.post(f"{BASE}/subjects/user/delta", json={
            "upserts": [{"id": "u1", "name": "张三"}]}).json()
        assert r2["inserted"] == 1

    def test_delta_empty_noop(self, client):
        c, env = client
        r = c.post(f"{BASE}/subjects/user/delta", json={})
        assert r.status_code == 200
        body = r.json()
        assert (body["inserted"], body["updated"], body["deleted"]) == (0, 0, 0)
        assert body["missing"] == []

    def test_delta_validation(self, client, monkeypatch):
        c, env = client
        from domains.knowledge_graph import api as kg_api
        assert c.post(f"{BASE}/subjects/role/delta", json={
            "upserts": "x"}).status_code == 422
        assert c.post(f"{BASE}/subjects/role/delta", json={
            "upserts": [{"id": "r1"}, {"id": "r1"}]}).status_code == 422
        assert c.post(f"{BASE}/subjects/role/delta", json={
            "deletes": "r1"}).status_code == 422
        assert c.post(f"{BASE}/subjects/role/delta", json={
            "deletes": ["r1", "r1"]}).status_code == 422
        monkeypatch.setattr(kg_api, "_DELTA_MAX_UPSERTS", 1)
        assert c.post(f"{BASE}/subjects/role/delta", json={
            "upserts": [{"id": "r1"}, {"id": "r2"}]}).status_code == 422


# ── 单条 CRUD ─────────────────────────────────────────────

class TestCrud:

    def test_put_create_then_merge_update(self, client):
        c, env = client
        r = c.put(f"{BASE}/subjects/user/u10",
                  json={"name": "王五", "status": "disabled"})
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True
        assert r.json()["status"] == "disabled"   # 提供的字段生效
        # 再 PUT 只带 name:status 保留现值,name 更新
        r2 = c.put(f"{BASE}/subjects/user/u10", json={"name": "王五丰"})
        body = r2.json()
        assert body["created"] is False
        assert body["name"] == "王五丰" and body["status"] == "disabled"

    def test_get_and_delete_lifecycle(self, client):
        c, env = client
        assert c.get(f"{BASE}/subjects/role/r9").status_code == 404
        assert c.delete(f"{BASE}/subjects/role/r9").status_code == 404
        c.put(f"{BASE}/subjects/role/r9", json={"name": "法务"})
        rec = c.get(f"{BASE}/subjects/role/r9").json()
        assert rec["name"] == "法务" and rec["createdAt"]
        assert c.delete(f"{BASE}/subjects/role/r9").json() == {"success": True}
        assert c.get(f"{BASE}/subjects/role/r9").status_code == 404

    def test_put_validation(self, client):
        c, env = client
        assert c.put(f"{BASE}/subjects/user/u1",
                     json={"status": "frozen"}).status_code == 422
        assert c.put(f"{BASE}/subjects/user/u1",
                     json={"name": "x" * 500}).status_code == 422
        assert c.put(f"{BASE}/subjects/bogus/x",
                     json={}).status_code == 422


# ── 列表:搜索/过滤/分页 ───────────────────────────────────

class TestList:

    def _seed(self, c):
        _full(c, "user", [
            {"id": "u1", "name": "张三"},
            {"id": "u2", "name": "李四", "status": "disabled"},
            {"id": "u3", "name": "王五"},
        ])

    def test_default_returns_all_with_full_shape(self, client):
        c, env = client
        self._seed(c)
        body = c.get(f"{BASE}/subjects/user").json()
        assert body["total"] == 3 and body["page"] is None
        assert [u["id"] for u in body["items"]] == ["u1", "u2", "u3"]
        item = body["items"][0]
        assert set(item) == {"id", "name", "status",
                             "createdAt", "updatedAt"}

    def test_q_matches_id_or_name(self, client):
        c, env = client
        self._seed(c)
        assert [u["id"] for u in c.get(
            f"{BASE}/subjects/user", params={"q": "张"}).json()["items"]] == ["u1"]
        assert [u["id"] for u in c.get(
            f"{BASE}/subjects/user", params={"q": "u2"}).json()["items"]] == ["u2"]
        assert c.get(f"{BASE}/subjects/user",
                     params={"q": "不存在"}).json()["items"] == []

    def test_status_filter_and_pagination(self, client):
        c, env = client
        self._seed(c)
        body = c.get(f"{BASE}/subjects/user",
                     params={"status": "disabled"}).json()
        assert [u["id"] for u in body["items"]] == ["u2"]
        page1 = c.get(f"{BASE}/subjects/user",
                      params={"page": 1, "pageSize": 2}).json()
        assert (page1["total"], len(page1["items"]),
                page1["page"], page1["pageSize"]) == (3, 2, 1, 2)
        page2 = c.get(f"{BASE}/subjects/user",
                      params={"page": 2, "pageSize": 2}).json()
        assert [u["id"] for u in page2["items"]] == ["u3"]
        assert c.get(f"{BASE}/subjects/user",
                     params={"page": 0}).status_code == 422
        assert c.get(f"{BASE}/subjects/user",
                     params={"page": 1, "pageSize": 500}).status_code == 422
        assert c.get(f"{BASE}/subjects/user",
                     params={"status": "frozen"}).status_code == 422
        assert c.get(f"{BASE}/subjects/bogus").status_code == 422


# ── 同步状态 ───────────────────────────────────────────────

class TestSyncStatus:

    def test_status_after_ops(self, client):
        c, env = client
        _full(c, "user", [{"id": "u1"}, {"id": "u2", "status": "disabled"}])
        c.post(f"{BASE}/subjects/user/delta", json={"deletes": ["u2"]})
        c.put(f"{BASE}/subjects/role/r1", json={"name": "法务"})
        body = c.get(f"{BASE}/sync/status").json()
        assert body["user"]["total"] == 1 and body["user"]["active"] == 1
        assert body["user"]["lastMode"] == "delta"
        assert body["user"]["lastSyncAt"]
        assert body["role"]["total"] == 1
        assert body["role"]["lastMode"] == "upsert"
        recent = body["recent"]
        assert recent and set(recent[0]) == {"subjectType", "mode",
                                             "summary", "syncedAt"}
        # 流水按时间倒序:最近一次是 role 的 upsert
        assert recent[0]["subjectType"] == "role"

    def test_status_empty(self, client):
        c, env = client
        body = c.get(f"{BASE}/sync/status").json()
        assert body["user"] == {"total": 0, "active": 0, "disabled": 0}
        assert body["role"] == {"total": 0, "active": 0, "disabled": 0}
        assert body["recent"] == []


# ── X-Api-Key(平台 MCP 密钥)接受分支 ──────────────────────

class TestMcpKeyAuth:

    def test_mcp_key_authorizes_sync_endpoints(self, client):
        """配置了 MCP 密钥后,X-Api-Key 可调全部同步面端点。"""
        c, env = client
        _set_mcp_key("sk-sync-1")
        headers = {"X-Api-Key": "sk-sync-1"}
        assert c.post(f"{BASE}/subjects/user", headers=headers,
                      json={"items": [{"id": "u1"}]}).status_code == 200
        assert c.get(f"{BASE}/subjects/user", headers=headers).status_code == 200
        assert c.put(f"{BASE}/subjects/role/r1", headers=headers,
                     json={"name": "法务"}).status_code == 200
        assert c.get(f"{BASE}/sync/status", headers=headers).status_code == 200

    def test_wrong_mcp_key_401_no_write(self, client):
        """错钥匙 401 且不落库(不静默漏进管理鉴权分支)。"""
        c, env = client
        _set_mcp_key("sk-sync-2")
        r = c.post(f"{BASE}/subjects/user",
                   headers={"X-Api-Key": "sk-bad"},
                   json={"items": [{"id": "u1"}]})
        assert r.status_code == 401
        assert "MCP API Key 无效" in r.json()["detail"]
        assert c.get(f"{BASE}/subjects/user/u1").status_code == 404

    def test_mcp_key_not_configured_503(self, client):
        """未配置 MCP 密钥时带 X-Api-Key → 503(与 /api/mcp 同文案)。"""
        c, env = client
        r = c.post(f"{BASE}/subjects/user",
                   headers={"X-Api-Key": "sk-any"}, json={"items": []})
        assert r.status_code == 503

    def test_mcp_key_does_not_open_other_admin_endpoints(self, client,
                                                         monkeypatch):
        """MCP 密钥只开同步面:其余管理端点(如建库)不认 X-Api-Key。"""
        c, env = client
        _set_mcp_key("sk-sync-3")
        monkeypatch.setenv("ADMIN_TOKEN", "adm-tok")  # 关开放模式设真门
        headers = {"X-Api-Key": "sk-sync-3"}
        r = c.post(f"{BASE}/kbs", headers=headers, json={"name": "x"})
        assert r.status_code == 401, \
            f"MCP 密钥不应打开管理端点: {r.status_code} {r.text[:200]}"
        assert c.post(f"{BASE}/subjects/user", headers=headers,
                      json={"items": [{"id": "u1"}]}).status_code == 200
        # 不带 X-Api-Key 时同步面回落管理鉴权:口令模式无凭证 → 401
        assert c.post(f"{BASE}/subjects/user",
                      json={"items": []}).status_code == 401
