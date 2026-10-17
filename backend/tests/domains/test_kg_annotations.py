"""D32 KG 标注回复—— 标注 CRUD/向量短路/阈值语义/降级/两生效点。

覆盖:upsert 重打向量、命中短路(kb_search 工具 reply+artifact 标注
标记)、REST /search 短路、阈值语义(相似但低于 0.90 不短路)、
启停(禁用的标注不参与匹配)、删除、跨库逐库试、短路层故障降级
常规检索。
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.knowledge_graph import runtime
from domains.knowledge_graph.api import router as kg_router
from domains.knowledge_graph.tools.kb_search import KbSearchTool
from sdk.tool import ToolContext

# 复用 m4 检索 fake 环境(MockLLM embeddings → 固定向量)
from tests.domains.test_knowledge_graph_m4 import _ctx, env  # noqa: F401


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


class _Embedder:
    """可脚本化的 embeddings:按问题前缀返回固定向量(相似度可控)。"""

    def __init__(self, base):
        self._base = base
        self.mapping = {}          # question → vector

    def __call__(self, texts, conv_id=None, stage=None, mode=None):
        out = []
        for t in texts:
            v = self.mapping.get(t)
            if v is None:
                v = self._base.embeddings([t], stage=stage)[0]
            out.append(v)
        return out


@pytest.fixture()
def scripted_embed(env, monkeypatch):
    """把 env.llm.embeddings 包成可映射版;返回映射表由用例填。"""
    se = _Embedder(env.llm)
    monkeypatch.setattr(env.llm, "embeddings", se)
    return se.mapping



def _mk_kb(env, name):
    kb = env.store.create_kb(name)
    env.store.set_kb_vector_info(kb["id"], "test-embed", 3, True)
    return kb

class TestAnnotationCrud:

    def test_create_list_update_delete(self, client):
        c, env = client
        kb = _mk_kb(env, "标注库")
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations",
                   json={"question": "未成年工的年龄?", "answer": "未满16周岁(法规原文)",
                         "source": "点踩转化"})
        assert r.status_code == 200, r.text
        aid = r.json()["id"]
        items = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations").json()["items"]
        assert len(items) == 1 and items[0]["question"].startswith("未成年工")
        assert items[0]["enabled"] is True

        # 更新答案
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}",
                  json={"answer": "修订后的答案"})
        assert r.status_code == 200
        # 纯启停(不重打向量)
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}",
                  json={"enabled": False})
        assert r.status_code == 200
        items = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations").json()["items"]
        assert items[0]["enabled"] is False

        # 删除
        assert c.delete(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}").status_code == 200
        assert c.delete(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}").status_code == 404

    def test_validation(self, client):
        c, env = client
        kb = _mk_kb(env, "标注校验库")
        assert c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations",
                      json={"question": "", "answer": "x"}).status_code == 422
        assert c.get("/api/packs/knowledge_graph/kbs/nope/annotations").status_code == 404


class TestShortCircuit:

    def test_kb_search_exact_hit_short_circuits(self, env, scripted_embed):
        """标注命中:kb_search 直接返回标准答案(reply+artifact 带 annotationHit),
        不走检索(fake graph 种子不会被动用)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "短路库")
        q, a = "未成年工的年龄界定", "未满十六周岁(标准答案口径)"
        scripted_embed[q] = [0.1] * 3
        scripted_embed[q] = [1.0, 0.0, 0.0]
        kg_annotations.upsert_annotation(env.app_state, kb["id"], q, a)
        # 查询与标注同文 → 向量相同 → cosine=1 ≥ 0.90
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": q}, _ctx(env))
        assert result.reply == a
        assert result.artifact["annotationHit"] is True
        assert result.artifact["kb"]["name"] == "短路库"

    def test_kb_search_below_threshold_falls_through(self, env, scripted_embed):
        """相似但低于阈值:不短路,走常规检索(种子命中返回模拟回答)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "阈值库")
        scripted_embed["标注问题原文"] = [1.0, 0.0, 0.0]
        kg_annotations.upsert_annotation(env.app_state, kb["id"], "标注问题原文", "标准答案")
        # 查询向量与标注正交 → cosine=0 < 0.90
        scripted_embed["完全不同的另一个问题?"] = [0.0, 1.0, 0.0]
        # MockLLM 的 embeddings 已被 scripted_embed 包装:未映射的文本走原实现
        # (m4 固定 [0.1,0.2,0.3])。这里直接映射查询向量保证正交
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "完全不同的另一个问题?"}, _ctx(env))
        assert result.reply is None                  # 走了常规链路(reply 为空)
        assert result.artifact_type == "data"
        assert not (result.artifact or {}).get("annotationHit")

    def test_disabled_annotation_not_matched(self, env, scripted_embed):
        """禁用的标注不参与匹配。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "停用库")
        q = "被停用的问题"
        scripted_embed[q] = [1.0, 0.0, 0.0]
        item = kg_annotations.upsert_annotation(env.app_state, kb["id"], q, "答案")
        kg_annotations.set_enabled(env.app_state, kb["id"], item["id"], False)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": q}, _ctx(env))
        assert result.reply is None                 # 不短路

    def test_rest_search_short_circuits(self, client, scripted_embed):
        """REST /search 同层短路:annotationHit + 标准答案。"""
        from domains.knowledge_graph import annotations as kg_annotations
        c, env = client
        kb = _mk_kb(env, "REST短路库")
        q = "rest 精确问题"
        scripted_embed[q] = [1.0, 0.0, 0.0]
        kg_annotations.upsert_annotation(env.app_state, kb["id"], q, "REST 标准答案")
        r = c.post("/api/packs/knowledge_graph/search", json={"query": q, "kb": kb["name"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["annotationHit"] is True
        assert body["answer"] == "REST 标准答案"

    def test_cross_kb_tries_each(self, env, scripted_embed):
        """跨库:逐库试,第二个库命中即返回。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb1 = _mk_kb(env, "跨库1")
        kb2 = _mk_kb(env, "跨库2")
        q = "跨库命中问题"
        scripted_embed[q] = [0.9, 0.1, 0.0]
        kg_annotations.upsert_annotation(env.app_state, kb2["id"], q, "跨库标准答案")
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": q}, _ctx(env))
        assert result.reply == "跨库标准答案"
        assert result.artifact["kb"]["name"] == "跨库2"

    def test_match_failure_degrades(self, env, monkeypatch):
        """短路层故障( embeddings 抛错):降级常规检索不炸。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "降级库")
        kg_annotations.upsert_annotation(env.app_state, kb["id"], "q", "a")

        def boom(texts, conv_id=None, stage=None, mode=None):
            raise RuntimeError("embed down")
        monkeypatch.setattr(env.llm, "embeddings", boom)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "任何问题"}, _ctx(env))
        assert result.artifact_type == "data"       # 常规检索照常
        assert not (result.artifact or {}).get("annotationHit")
