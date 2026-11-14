"""D32 KG 标注回复—— 标注 CRUD/向量短路/阈值语义/降级/两生效点。

覆盖:upsert 重打向量、命中短路(kb_search 工具答案制品标注
标记)、REST /search 短路、阈值语义(相似但低于 0.90 不短路)、
启停(禁用的标注不参与匹配)、删除、跨库逐库试、短路层故障降级
常规检索;固定问答(触发问法)归一化精确命中(零模型调用)、
并集语义、triggers CRUD 校验与同库冲突 422、无表首访零 DDL。
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

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_first_access_without_annotation_table(self, client, monkeypatch, method):
        """全新安装首访:标注表未懒建时,读/启停/删除按空集合处理,
        不跑 DDL 也不得触发 embeddings。"""
        c, env = client
        kb = _mk_kb(env, "首次访问标注库")
        # 专用测试库逐用例清表,但 kg_annotations 是首用懒建——
        # 显式 DROP 复现"从未建过表"的真实全新安装
        with env.store._get_conn() as conn:
            conn.execute("DROP TABLE IF EXISTS kg_annotations")

        def unexpected_embedding(*args, **kwargs):
            pytest.fail("Reading an empty annotation collection must not invoke embeddings")

        monkeypatch.setattr(env.llm, "embeddings", unexpected_embedding)
        path = f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations"
        if method == "GET":
            response = c.get(path)
            assert response.status_code == 200
            assert response.json() == {"items": []}
            assert c.get(path).json() == {"items": []}
        elif method == "PUT":
            response = c.put(path + "/missing", json={"enabled": False})
            assert response.status_code == 404
        else:
            response = c.delete(path + "/missing")
            assert response.status_code == 404

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
        """标注命中:kb_search 直接返回标准答案制品(带 annotationHit),
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
        assert result.reply is None
        assert result.summary == a
        assert result.artifact["answer"] == a
        assert result.artifact["resultDisplay"] == "card"
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
        assert result.reply is None
        assert result.artifact["answer"] == "跨库标准答案"
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


class TestTriggerExactMatch:
    """固定问答(触发问法)——归一化纯文本精确命中,先于 embedding。

    并集语义:triggers 只增加命中面;未命中(含带 triggers 条目的
    paraphrase)仍走语义路,所有 enabled 条目照常参与。
    """

    @staticmethod
    def _forbid_embeddings(env, monkeypatch):
        """建标注(upsert 需向量化)完成后启用:此后任何 embeddings
        调用直接失败——证明精确命中路径零模型调用。"""

        def fail(*args, **kwargs):
            pytest.fail("触发问法精确命中不得调用 embeddings")

        monkeypatch.setattr(env.llm, "embeddings", fail)

    def test_trigger_hit_zero_embeddings(self, env, monkeypatch):
        """触发问法原样命中:零 embedding 调用,matchType=exact。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "固定问答库")
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "如何下载证书", "证书在「我的-证书」页下载",
            triggers=["怎么下载证书", "证书怎么下载"])
        self._forbid_embeddings(env, monkeypatch)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "怎么下载证书"}, _ctx(env))
        assert result.summary == "证书在「我的-证书」页下载"
        assert result.artifact["annotationHit"] is True
        assert result.artifact["matchType"] == "exact"
        assert result.artifact["answer"] == "证书在「我的-证书」页下载"

    def test_question_itself_exact_hits_without_triggers(self, env, monkeypatch):
        """未配 triggers 的条目:question 原样问出也走精确段(升级旧行为)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "问题本身精确库")
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "营业时间", "9:00-18:00")
        self._forbid_embeddings(env, monkeypatch)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "营业时间"}, _ctx(env))
        assert result.artifact["annotationHit"] is True
        assert result.artifact["matchType"] == "exact"

    @pytest.mark.parametrize(
        "query",
        [
            "怎么下载证书?",   # 半角问号
            "怎么下载证书？",   # 全角问号(NFKC 折半角)
            "怎么下载证书。",   # 句读结尾
            "怎么 下载 证书",   # 内部空白
            "  怎么下载证书  ",  # 首尾空白
        ],
    )
    def test_normalization_variants_hit(self, env, monkeypatch, query):
        """归一化变体全部命中:NFKC/casefold/去空白/去尾部问句标点。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "归一化库")
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "如何下载证书", "标准答案", triggers=["怎么下载证书"])
        self._forbid_embeddings(env, monkeypatch)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": query}, _ctx(env))
        assert result.artifact["annotationHit"] is True
        assert result.artifact["matchType"] == "exact"

    def test_casefold_hit_for_ascii(self, env, monkeypatch):
        """英文问法大小写不敏感。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "英文问法库")
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "How to export", "点右上角导出", triggers=[])
        self._forbid_embeddings(env, monkeypatch)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "HOW TO EXPORT"}, _ctx(env))
        assert result.artifact["matchType"] == "exact"

    def test_miss_falls_back_to_semantic_for_entry_with_triggers(self, env, scripted_embed):
        """并集回归:带 triggers 条目的其他问法仍可语义命中(≥0.90)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "并集库")
        q = "证书下载的标准入口"
        scripted_embed[q] = [1.0, 0.0, 0.0]        # 标注问题向量
        scripted_embed["证书从哪里下载"] = [0.98, 0.0, 0.0]  # paraphrase 高度相似
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], q, "语义标准答案",
            triggers=["完全无关的触发问法"])
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "证书从哪里下载"}, _ctx(env))
        assert result.artifact["annotationHit"] is True
        assert result.artifact["matchType"] == "semantic"
        assert result.artifact["answer"] == "语义标准答案"

    def test_disabled_trigger_not_matched(self, env):
        """禁用条目的触发问法不参与精确匹配(走常规检索)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "停用触发库")
        item = kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "q", "a", triggers=["触发问法"])
        kg_annotations.set_enabled(env.app_state, kb["id"], item["id"], False)
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "触发问法"}, _ctx(env))
        assert not (result.artifact or {}).get("annotationHit")

    def test_cross_kb_trigger_hit(self, env, scripted_embed):
        """跨库:逐库试,触发问法在第二个库命中(第一个库语义不命中)。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb1 = _mk_kb(env, "跨库触发1")
        kb2 = _mk_kb(env, "跨库触发2")
        # scripted_embed 的回退路径会递归:所有会走到 embeddings 的文本
        # (两条标注问题 + 查询)都必须显式映射
        scripted_embed["固定触发问法"] = [0.0, 1.0, 0.0]  # 与 kb1 标注正交,语义不命中
        scripted_embed["q1"] = [1.0, 0.0, 0.0]
        scripted_embed["q2"] = [1.0, 0.0, 0.0]
        kg_annotations.upsert_annotation(env.app_state, kb1["id"], "q1", "a1")
        kg_annotations.upsert_annotation(
            env.app_state, kb2["id"], "q2", "跨库固定答案", triggers=["固定触发问法"])
        tool = KbSearchTool(env.app_state)
        result = tool.execute({"user_input": "固定触发问法"}, _ctx(env))
        assert result.artifact["answer"] == "跨库固定答案"
        assert result.artifact["kb"]["name"] == "跨库触发2"
        assert result.artifact["matchType"] == "exact"

    def test_rest_search_trigger_hit(self, client, monkeypatch):
        """REST /search 同层:触发问法精确命中。"""
        from domains.knowledge_graph import annotations as kg_annotations
        c, env = client
        kb = _mk_kb(env, "REST触发库")
        kg_annotations.upsert_annotation(
            env.app_state, kb["id"], "如何联系客服", "电话 12345",
            triggers=["客服电话多少"])
        self._forbid_embeddings(env, monkeypatch)
        r = c.post("/api/packs/knowledge_graph/search",
                   json={"query": "客服电话多少", "kb": kb["name"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["annotationHit"] is True
        assert body["matchType"] == "exact"
        assert body["answer"] == "电话 12345"

    def test_match_annotation_no_table_no_model(self, env, monkeypatch):
        """表未懒建:match 直接 None,不建表不调 embeddings。"""
        from domains.knowledge_graph import annotations as kg_annotations
        kb = _mk_kb(env, "无表短路库")
        with env.store._get_conn() as conn:
            conn.execute("DROP TABLE IF EXISTS kg_annotations")
        self._forbid_embeddings(env, monkeypatch)
        assert kg_annotations.match_annotation(env.app_state, kb["id"], "任何问法") is None


class TestTriggerCrud:
    """触发问法 CRUD:落库/替换/清空/校验/同库冲突。"""

    def test_create_with_triggers_and_list(self, client):
        c, env = client
        kb = _mk_kb(env, "触发CRUD库")
        r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations",
                   json={"question": "如何下载证书", "answer": "标准答案",
                         "triggers": ["怎么下载证书", "  怎么下载证书?  ", "",
                                      "怎么下载证书"]})
        assert r.status_code == 200, r.text
        # 空/归一化重复项被剔除(与 question 重复的也会被剔)
        assert r.json()["triggers"] == ["怎么下载证书"]
        items = c.get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations").json()["items"]
        assert items[0]["triggers"] == ["怎么下载证书"]

    def test_update_replaces_and_clears_triggers(self, client):
        c, env = client
        kb = _mk_kb(env, "触发更新库")
        aid = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations",
                     json={"question": "q", "answer": "a",
                           "triggers": ["旧问法"]}).json()["id"]
        # 缺省不改动
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}",
                  json={"answer": "a2"})
        assert r.json()["triggers"] == ["旧问法"]
        # 显式数组替换
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}",
                  json={"triggers": ["新问法"]})
        assert r.json()["triggers"] == ["新问法"]
        # 空数组清空
        r = c.put(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations/{aid}",
                  json={"triggers": []})
        assert r.json()["triggers"] == []

    def test_triggers_validation(self, client):
        c, env = client
        kb = _mk_kb(env, "触发校验库")
        base = f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations"
        # 非数组 422
        assert c.post(base, json={"question": "q", "answer": "a",
                                  "triggers": "x"}).status_code == 422
        # 元素非字符串 422
        assert c.post(base, json={"question": "q", "answer": "a",
                                  "triggers": [1]}).status_code == 422
        # 单条超长 422
        assert c.post(base, json={"question": "q", "answer": "a",
                                  "triggers": ["x" * 201]}).status_code == 422
        # 超条数上限 422
        assert c.post(base, json={"question": "q", "answer": "a",
                                  "triggers": [f"t{i}" for i in range(21)]}
                      ).status_code == 422

    def test_conflict_within_kb_rejected(self, client):
        """同库精确键冲突:触发问法撞他条触发问法/撞他条 question → 422。"""
        c, env = client
        kb = _mk_kb(env, "触发冲突库")
        base = f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations"
        c.post(base, json={"question": "如何下载证书", "answer": "a1",
                           "triggers": ["怎么下载证书"]})
        # 撞已有触发问法(带全角问号变体)
        r = c.post(base, json={"question": "另一个问题", "answer": "a2",
                               "triggers": ["怎么下载证书？"]})
        assert r.status_code == 422 and "冲突" in r.json()["detail"]
        # 撞已有条目的 question 本身
        r = c.post(base, json={"question": "再一个问题", "answer": "a3",
                               "triggers": ["如何下载证书?"]})
        assert r.status_code == 422
        # 自身更新(问法不变)不算冲突
        items = c.get(base).json()["items"]
        aid = items[0]["id"]
        r = c.put(f"{base}/{aid}", json={"triggers": ["怎么下载证书"]})
        assert r.status_code == 200

    def test_same_trigger_across_kbs_allowed(self, client):
        """跨库同问法允许(跨库逐库试,先到先得)。"""
        c, env = client
        kb1 = _mk_kb(env, "冲突跨库1")
        kb2 = _mk_kb(env, "冲突跨库2")
        for kb in (kb1, kb2):
            r = c.post(f"/api/packs/knowledge_graph/kbs/{kb['id']}/annotations",
                       json={"question": "q", "answer": "a", "triggers": ["同一问法"]})
            assert r.status_code == 200
