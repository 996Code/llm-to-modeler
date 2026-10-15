"""向量默认启用（用户 10-15 裁决）——本地 bge-m3 是定死能力,不做每库开关探测。

- 新建库 vectorEnabled 默认 True
- 存量未决库(曾因探测失败落 0 且无 embedding_model)迁移翻转为 True
- 曾因"模型变更"显式关闭的库(vector_enabled=0 且有 embedding_model)不迁移
  ——那是兼容性保护,不是未决态
"""
import psycopg

from domains.knowledge_graph.store import KGStore

_TEST_URL = __import__("tests.conftest", fromlist=["TEST_DATABASE_URL"]).TEST_DATABASE_URL


class TestVectorDefaultOn:

    def test_new_kb_vector_enabled(self):
        """新建库向量默认启用(本地 bge-m3 定死,无需配置)。"""
        s = KGStore()
        kb = s.create_kb("默认向量库甲")
        assert kb["vectorEnabled"] is True

    def test_undecided_legacy_kb_migrated_on_init(self):
        """存量未决库(0 且无模型记录)在 store 重新初始化时翻转为启用。"""
        s = KGStore()
        kb = s.create_kb("存量库乙")
        with psycopg.connect(_TEST_URL, autocommit=True) as conn:
            conn.execute("UPDATE knowledge_graph.kg_knowledge_bases "
                         "SET vector_enabled=0, embedding_model='' "
                         "WHERE id=%s", (kb["id"],))
        KGStore()   # 重新初始化触发迁移
        fresh = KGStore().get_kb(kb["id"])
        assert fresh["vectorEnabled"] is True

    def test_model_mismatch_closed_kb_not_migrated(self):
        """模型变更关闭的库(0 但有模型记录)不迁移——兼容性保护态。"""
        s = KGStore()
        kb = s.create_kb("模型关闭库丙")
        with psycopg.connect(_TEST_URL, autocommit=True) as conn:
            conn.execute("UPDATE knowledge_graph.kg_knowledge_bases "
                         "SET vector_enabled=0, embedding_model='bge-m3' "
                         "WHERE id=%s", (kb["id"],))
        KGStore()
        assert KGStore().get_kb(kb["id"])["vectorEnabled"] is False
