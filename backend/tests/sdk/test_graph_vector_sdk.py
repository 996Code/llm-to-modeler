"""SDK 图/向量存储单测 —— Protocol 结构/参数化命名/scope 契约。

不要求真实 Neo4j/Milvus:结构检查用类属性与源码,行为检查用构造参数
生成的名字与入口防御(driver/client 用桩绕过构造)。
"""
import inspect
from unittest.mock import MagicMock, patch

import pytest

from sdk import graph_store as gs
from sdk import vector_store as vs
from sdk.scope_registry import register_prefix

UUID_A = "11111111-1111-1111-1111-111111111111"


def _graph(prefix="kg_", label="Entity", rel="RELATES", sp="kb_id") -> gs.Neo4jGraphStore:
    # 绕过 __init__ 的 driver 构造(不依赖真实 Neo4j)
    store = gs.Neo4jGraphStore.__new__(gs.Neo4jGraphStore)
    store._database = "neo4j"
    store._driver = MagicMock()
    store._prefix = prefix
    store._label = label
    store._rel = rel
    store._sp = sp
    return store


def _vector(prefix="kg") -> vs.MilvusVectorStore:
    store = vs.MilvusVectorStore.__new__(vs.MilvusVectorStore)
    store._client = MagicMock()
    store._prefix = prefix
    return store


class TestProtocolStructure:
    """实现类具备 Protocol 声明的全部方法(结构化鸭子检查)。"""

    def test_neo4j_implements_graph_protocol(self):
        for m in ("upsert_batch", "delete_document", "delete_scope", "counts",
                  "document_counts", "get_graph", "expand_node", "find_entities",
                  "subgraph_around", "close"):
            assert callable(getattr(gs.Neo4jGraphStore, m, None)), f"缺 {m}"

    def test_milvus_implements_vector_protocol(self):
        for m in ("ensure_collection", "drop_collection", "upsert_chunks",
                  "delete_by_doc", "search", "count", "close"):
            assert callable(getattr(vs.MilvusVectorStore, m, None)), f"缺 {m}"

    def test_dead_code_removed(self):
        # 死代码已删:has_collection / delete_by_chunks 不在公开面
        assert not hasattr(vs.MilvusVectorStore, "has_collection")
        assert not hasattr(vs.MilvusVectorStore, "delete_by_chunks")


class TestParameterizedNaming:
    """参数化命名:不同前缀/标签生成不同的约束名与 collection 名。"""

    def test_collection_name_rules(self):
        # 与存量 kg_{kb}_v1 完全兼容
        assert vs.collection_name("kg", UUID_A) == f"kg_{UUID_A.replace('-', '_')}_v1"
        # 不同插件前缀不撞
        assert vs.collection_name("bi", UUID_A) == f"bi_{UUID_A.replace('-', '_')}_v1"
        assert vs.collection_name("kg", UUID_A) != vs.collection_name("bi", UUID_A)

    def test_ensure_constraints_uses_prefix(self):
        store = _graph(prefix="bi_")
        store.ensure_constraints()
        session = store._driver.session.return_value.__enter__.return_value
        run_texts = [str(c.args[0]) if c.args else "" for c in session.run.call_args_list]
        assert any("bi_entity_key" in t for t in run_texts), run_texts
        assert any("bi_entity_name" in t for t in run_texts)
        assert not any("kg_entity_key" in t for t in run_texts)

    def test_default_params_backward_compatible(self):
        # 构造参数默认值与 KG 存量数据完全兼容(源码级断言)
        src = inspect.getsource(gs.Neo4jGraphStore.__init__)
        assert 'prefix: str = "kg_"' in src
        assert 'node_label: str = "Entity"' in src
        assert 'rel_type: str = "RELATES"' in src
        assert 'scope_prop: str = "kb_id"' in src

    def test_vector_name_uses_prefix(self):
        store = _vector(prefix="bi")
        store._client.has_collection.return_value = False
        store.ensure_collection(UUID_A, 8)
        created = store._client.create_collection.call_args
        assert created.kwargs["collection_name"] == f"bi_{UUID_A.replace('-', '_')}_v1"

    def test_cypher_uses_custom_label(self):
        store = _graph(label="ReportNode", rel="DERIVES", sp="report_id")
        store._driver.session.return_value.__enter__.return_value.run.return_value \
            .single.return_value = {"c": 0}
        store.counts(UUID_A)
        cypher = store._driver.session.return_value.__enter__.return_value.run.call_args.args[0]
        assert ":ReportNode" in cypher and "report_id: $kb" in cypher


class TestScopeContract:
    """scope_id 契约的入口防御:非 UUID 拒收,UUID 放行。"""

    @pytest.mark.parametrize("bad", ["我的库", "kb-001", "../../x", ""])
    def test_graph_rejects_user_input(self, bad):
        store = _graph()
        with pytest.raises(ValueError, match="scope_id"):
            store.counts(bad)
        with pytest.raises(ValueError, match="scope_id"):
            store.delete_scope(bad)

    @pytest.mark.parametrize("bad", ["我的库", "kb-001", ""])
    def test_vector_rejects_user_input(self, bad):
        store = _vector()
        with pytest.raises(ValueError, match="scope_id"):
            store.count(bad)
        with pytest.raises(ValueError, match="scope_id"):
            store.ensure_collection(bad, 8)

    def test_uuid_scope_passes(self):
        store = _graph()
        store._driver.session.return_value.__enter__.return_value.run.return_value \
            .single.return_value = {"c": 7}
        assert store.counts(UUID_A) == {"entities": 7, "relations": 7}


class TestPrefixIsolation:
    """前缀登记:KG 前缀由 stores.py 声明;登记表工作正常。"""

    def test_registry_smoke(self):
        register_prefix("zz_sdk_test", "zz_owner")
        register_prefix("zz_sdk_test", "zz_owner")   # 幂等

    def test_kg_prefix_declared_by_adapter(self):
        # stores.py 声明 kg 前缀(源码级检查——适配层是前缀唯一声明点)
        src = open("src/domains/knowledge_graph/stores.py").read()
        assert 'GRAPH_PREFIX = "kg_"' in src
        assert 'VECTOR_PREFIX = "kg"' in src
        assert 'register_prefix("kg"' in src


class TestCypherRendering:
    """全部公开方法的 Cypher 渲染断言(历史最易错:f-string 转义/漏 f 前缀)。

    每次 upsert/find_entities 类事故都是"某条 Cypher 没在 f-string 里"
    或"花括号转义错"——这里把 22 条生成路径全部驱动一遍并断言关键形态。
    """

    def _store(self):
        store = _graph()
        sess = store._driver.session.return_value.__enter__.return_value
        sess.run.return_value.single.return_value = {"c": 0}
        sess.run.return_value.data.return_value = []
        sess.execute_write.side_effect = lambda cb: cb(sess)
        return store, sess

    def test_upsert_batch_renders_valid_cypher(self):
        store, sess = self._store()
        store.upsert_batch(UUID_A, "doc1",
                           [{"normalized_name": "a", "name": "a"}],
                           [{"source": "a", "target": "b", "type": "t"}])
        cys = [str(c.args[0]) for c in sess.run.call_args_list if c.args]
        assert len(cys) == 2                       # 实体 MERGE + 关系 MERGE
        for cy in cys:
            assert "{self._" not in cy             # 无未插值残留
            assert "MERGE (e:Entity {kb_id: $kb" in cy or "MATCH (s:Entity" in cy
        # 关系块的花括号转义正确(多行 {{ ... }} 块)
        assert "MERGE (s)-[rel:RELATES {" in cys[1]

    def test_upsert_batch_custom_model(self):
        store = _graph(label="ReportNode", rel="DERIVES", sp="report_id")
        sess = store._driver.session.return_value.__enter__.return_value
        sess.execute_write.side_effect = lambda cb: cb(sess)
        store.upsert_batch(UUID_A, "d", [{"normalized_name": "x"}],
                           [{"source": "x", "target": "x", "type": "r"}])
        cy = str(sess.run.call_args_list[-1].args[0])
        assert ":ReportNode" in cy and "report_id: $kb" in cy and "rel:DERIVES" in cy

    def test_all_query_methods_render(self):
        store, sess = self._store()
        for fn in (lambda: store.counts(UUID_A),
                   lambda: store.document_counts(UUID_A, "d"),
                   lambda: store.delete_document(UUID_A, "d"),
                   lambda: store.delete_scope(UUID_A),
                   lambda: store.get_graph(UUID_A),
                   lambda: store.expand_node(UUID_A, "nid"),
                   lambda: store.find_entities(UUID_A, ["x"]),
                   lambda: store.subgraph_around(UUID_A, ["x"])):
            sess.run.call_args_list.clear()
            sess.execute_write.call_args_list.clear()
            sess.execute_write.side_effect = lambda cb: cb(sess)
            fn()
            for c in sess.run.call_args_list:
                cy = str(c.args[0]) if c.args else ""
                assert "{self._" not in cy, cy[:80]
                assert "{{" not in cy and "}}" not in cy, cy[:80]  # 转义残留


class TestFactoryPrefix:
    """工厂级前缀回归(H1):get_*_store 必须把前缀透传进构造——
    漏传时两个前缀的插件拿到不同对象但 _prefix 全是默认值,静默互踩
    命名空间(KG 因恰好等于默认值而幸免,NL2BI 按文档示例接入即中招)。
    """

    def test_vector_factory_passes_prefix(self, monkeypatch):
        import sdk.vector_store as vs_mod
        vs_mod._cached_store, vs_mod._cached_fp = None, ()
        calls = []

        real_cls = vs_mod.MilvusVectorStore

        def fake_ctor(uri, user="", password="", collection_prefix="kg"):
            calls.append(collection_prefix)
            s = real_cls.__new__(real_cls)
            s._client = MagicMock()
            s._prefix = collection_prefix
            return s

        monkeypatch.setattr(vs_mod, "MilvusVectorStore", fake_ctor)
        try:
            s1 = vs_mod.get_vector_store({"milvus_uri": "u"}, collection_prefix="kg")
            s2 = vs_mod.get_vector_store({"milvus_uri": "u"}, collection_prefix="bi")
            assert s1._prefix == "kg"
            assert s2._prefix == "bi", "工厂必须把 collection_prefix 透传进构造(H1)"
            assert s1 is not s2
            assert calls == ["kg", "bi"]   # 两个前缀各构造一次(指纹区分)
        finally:
            vs_mod._cached_store, vs_mod._cached_fp = None, ()

    def test_graph_factory_passes_prefix(self, monkeypatch):
        import sdk.graph_store as gs_mod
        gs_mod._cached_store, gs_mod._cached_fp = None, ()
        calls = []

        real_cls = gs_mod.Neo4jGraphStore

        def fake_ctor(uri, user="", password="", database="neo4j", **kw):
            calls.append(kw)
            s = real_cls.__new__(real_cls)
            s._database = database
            s._driver = MagicMock()
            s._prefix = kw.get("prefix", "kg_")
            s._label = kw.get("node_label", "Entity")
            s._rel = kw.get("rel_type", "RELATES")
            s._sp = kw.get("scope_prop", "kb_id")
            s.ensure_constraints = lambda: None
            return s

        monkeypatch.setattr(gs_mod, "Neo4jGraphStore", fake_ctor)
        try:
            s1 = gs_mod.get_graph_store({"neo4j_uri": "u"})
            s2 = gs_mod.get_graph_store({"neo4j_uri": "u"}, prefix="bi_", node_label="R")
            assert s1._prefix == "kg_"
            assert s2._prefix == "bi_"
            assert s2._label == "R"
            assert s1 is not s2
            assert calls[1].get("prefix") == "bi_"
        finally:
            gs_mod._cached_store, gs_mod._cached_fp = None, ()
