"""P2#12 KG 全文第三路验收 —— tsvector+GIN / 2-gram 分词 / 图谱零命中兜底 / NFKC。

量化门槛(蓝图原文):20 条回归 case(实体部分匹配/别名/错别字三类),
全文路 top-10 命中率 ≥0.6 且比纯包含基线高 ≥15pp;P95 ≤50ms(真实 PG)。

【基线口径】纯包含基线 = 意图关键词 term 在 chunk 原文的子串包含
(旧图谱路 find_entities 包含匹配语义平移到 chunk 层)——不是拿整句
query 做子串(那是对稻草人比,没有意义)。
"""
import time
from types import SimpleNamespace

import pytest

from domains.knowledge_graph import retrieval, runtime
from domains.knowledge_graph.store import KGStore, query_grams
from sdk.doc_parser import parse_to_text


# ── NFKC(坑位②:民法典 PDF ⼀/⼱ 兼容字符根因) ───────────────

class TestNfkcNormalize:

    def test_kangxi_radicals_normalized(self):
        """康熙部首兼容字符(⼀ U+2F00 / ⼱ U+5F51)→ 规范字(一/巾)。"""
        text = parse_to_text("a.txt", "⼀⼱".encode("utf-8"))
        assert text == "一巾"

    def test_fullwidth_normalized(self):
        """全角字母/数字(NFKC 兼容区)→ 半角。"""
        text = parse_to_text("a.txt", "ＡＢ１２３".encode("utf-8"))
        assert text == "AB123"

    def test_plain_text_unchanged(self):
        src = "普通中文与 English mix 123"
        assert parse_to_text("a.txt", src.encode("utf-8")) == src


# ── 语料与 20 条回归 case ─────────────────────────────────────

# 语料:8 个实体块 + 10 个干扰块(干扰块保证 top-10 截断有区分度)
CORPUS = [
    "张伟明担任华东区域销售总监，负责渠道拓展与大客户维护。",
    "诸葛亮，字孔明，号卧龙，蜀汉丞相，辅佐刘备建立蜀汉政权。",
    "《中华人民共和国民法典》被称为社会生活的百科全书，由总则编、物权编、合同编等七编组成。",
    "合同纠纷案件应当先行调解，调解不成的，当事人可以向人民法院提起诉讼。",
    "陆压道人又称陆压，居于北海之滨，与世无争。",
    "关羽字云长，东汉末年名将，手持青龙偃月刀，威震华夏。",
    "赵敏是明教教主张无忌的伴侣，聪慧果决，人称绍敏郡主。",
    "王阳明提出知行合一，心即理也，为明代心学集大成者。",
    # 干扰块(与上面实体无 bigram 交集)
    "李逍遥是余杭镇客栈的店小二，后来拜入蜀山派修习御剑之术。",
    "林黛玉葬花于大观园，感怀身世，写下《葬花吟》。",
    "鲁滨逊在荒岛上搭建住所，驯养山羊，度过了二十八年。",
    "郝思嘉经营塔拉庄园，历经战乱与饥荒，始终不愿低头。",
    "安德的游戏里，天才少年被送往战斗学校训练指挥舰队。",
    "福尔摩斯通过烟灰与脚印推断出凶手的职业与行踪。",
    "霍比特人比尔博随远征队穿越迷雾山脉，偶遇咕噜。",
    "简爱在桑菲尔德庄园担任家庭教师，与罗切斯特相识。",
    "堂吉诃德骑着瘦马罗西南特，把风车当作巨人冲杀。",
    "保尔柯察金在筑路工地上与严寒和疾病搏斗。",
]

CASES = [
    # 实体部分匹配(7):term 是实体子串 / 实体是 term 子串
    ("民法典主要讲了哪些内容", "民法典", 2, "partial"),
    ("张伟负责什么工作", "张伟", 0, "partial"),
    ("销售总监的职责是什么", "销售总监", 0, "partial"),
    ("民法典全文一共多少条", "民法典全文", 2, "partial"),      # 基线 miss:文本只有"民法典"
    ("张伟明的职位是什么", "张伟明的职位", 0, "partial"),       # 基线 miss:文本无"的职位"
    ("青龙偃月刀是谁的兵器", "偃月刀", 5, "partial"),
    ("知行合一是谁提出来的", "知行合一", 7, "partial"),
    # 别名(6):字/号/别称/混称
    ("孔明的老师是谁", "孔明", 1, "alias"),
    ("卧龙先生是哪位人物", "卧龙", 1, "alias"),
    ("云长公的兵器是什么", "云长", 5, "alias"),
    ("孔明诸葛亮是同一个人吗", "孔明诸葛亮", 1, "alias"),      # 基线 miss:文本无该连写
    ("陆压道人的别称是什么", "陆压道人的别称", 4, "alias"),     # 基线 miss
    ("赵敏的丈夫是谁", "赵敏", 6, "alias"),
    # 错别字(7):单字讹误,bigram 滑窗仍留有正确 gram
    ("合同纠分怎么处理", "合同纠分", 3, "typo"),               # 基线 miss
    ("销受总监是谁", "销受总监", 0, "typo"),                   # 基线 miss
    ("民法典总则偏讲什么", "总则偏", 2, "typo"),               # 基线 miss
    ("张伟明担壬什么职务", "担壬", 0, "typo"),                 # 基线 miss
    ("卧龙先生的主公是谁", "卧龙先生", 1, "typo"),             # 基线 miss:文本只有"卧龙"
    ("王杨明的心学思想", "王杨明", 7, "typo"),                 # 双侧:全文也 miss(诚实留缺口)
    ("余杭镇的李逍遥是干什么的", "李逍遥", 8, "typo"),
]


@pytest.fixture()
def corpus_store():
    """真实 PG:建库/文档/切块(kg_chunks 全文索引由 KGStore 迁移幂等保证)。"""
    store = KGStore()
    kb = store.create_kb("全文回归库")
    doc = store.create_document(kb["id"], "corpus.md", "text/markdown",
                                1000, "", "hash-ft")
    store.replace_chunks(doc["id"], kb["id"],
                         [{"seq": i, "text": t} for i, t in enumerate(CORPUS)])
    yield store, kb, doc
    store.delete_kb(kb["id"])


def _fulltext_top10_hit(store, kb_id, query, expect_idx):
    """全文路 top-10 是否命中期望块(按 chunk 文本起点识别)。"""
    hits = store.search_chunks_fulltext(kb_id, query, limit=10)
    expect_text = CORPUS[expect_idx]
    return any((h.get("text") or "").startswith(expect_text[:12]) for h in hits)


def _baseline_hit(term, expect_idx):
    """纯包含基线:term 是否为期望块原文的子串(旧包含匹配语义)。"""
    return term in CORPUS[expect_idx]


class TestFulltextRegression:
    """20 条回归 case:命中率门槛 + 基线对比(≥+15pp)+ P95。"""

    def test_migration_column_and_gin(self, corpus_store):
        """迁移幂等落位:tsvector 生成列 + GIN 索引(pack schema 内)。"""
        import psycopg
        from tests.conftest import TEST_DATABASE_URL
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            col = conn.execute(
                "SELECT is_generated, generation_expression FROM information_schema.columns "
                "WHERE table_schema='knowledge_graph' AND table_name='kg_chunks' "
                "AND column_name='search_text'").fetchone()
            idx = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='knowledge_graph' "
                "AND indexname='idx_kg_chunks_fulltext'").fetchone()
        assert col is not None, "search_text 生成列缺失"
        assert col[0] == "ALWAYS"
        assert "to_tsvector" in (col[1] or "")
        assert idx is not None and "gin" in idx[0].lower()

    def test_query_grams(self):
        """2-gram 分词:CJK 双字滑窗 + 拉丁原词 + 最小停用词典滤噪。"""
        grams = query_grams("民法典ABC")
        assert "民法" in grams and "法典" in grams and "abc" in grams
        assert "什么" not in query_grams("这是什么")
        assert query_grams("???") == []

    def test_hit_rate_and_baseline_gap(self, corpus_store):
        """门槛:全文 top-10 命中率 ≥0.6,且比纯包含基线高 ≥15pp。"""
        store, kb, _ = corpus_store
        ft_hits = sum(_fulltext_top10_hit(store, kb["id"], q, i)
                      for q, t, i, _c in CASES)
        base_hits = sum(_baseline_hit(t, i) for _q, t, i, _c in CASES)
        ft_rate = ft_hits / len(CASES)
        base_rate = base_hits / len(CASES)
        assert ft_rate >= 0.6, f"全文命中率 {ft_rate:.2f}({ft_hits}/{len(CASES)}) < 0.6"
        assert ft_rate - base_rate >= 0.15, (
            f"全文 {ft_rate:.2f} 比基线 {base_rate:.2f} 仅高 "
            f"{(ft_rate - base_rate) * 100:.0f}pp < 15pp")

    def test_p95_latency(self, corpus_store):
        """P95 ≤50ms(真实 PG + GIN,预热后 3 轮 ×20 query)。"""
        store, kb, _ = corpus_store
        queries = [c[0] for c in CASES]
        store.search_chunks_fulltext(kb["id"], queries[0])  # 预热(计划/缓存)
        samples = []
        for _round in range(3):
            for q in queries:
                t0 = time.perf_counter()
                store.search_chunks_fulltext(kb["id"], q)
                samples.append((time.perf_counter() - t0) * 1000)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        assert p95 < 50, f"P95 {p95:.1f}ms ≥ 50ms(样本 {len(samples)})"


# ── 图谱零命中兜底(hybrid_retrieve 编排层) ────────────────────

class MockLLM:
    def embeddings(self, texts, conv_id=None, stage=None, mode=None):
        return [[0.1, 0.2, 0.3] for _ in texts]


class ZeroHitGraph:
    """find_entities 恒零命中(兜底触发条件:seeds 空)。"""

    def find_entities(self, kb_id, terms, limit=20):
        return []

    def subgraph_around(self, kb_id, seed_names, hops=2, max_nodes=80, max_edges=150):
        return {"nodes": [], "edges": []}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """m4 同款装配:真实 KGStore(真实 PG)+ 零命中图 + 可控向量。"""
    monkeypatch.setenv("KG_FILES_DIR", str(tmp_path / "files"))
    runtime.reset_runtime_cache()

    llm = MockLLM()
    graph = ZeroHitGraph()
    vector_hits: list = []

    class FakeVector:
        def search(self, kb_id, query_vector, top_k=5, doc_id=None):
            return [dict(h) for h in vector_hits]

    vector = FakeVector()
    monkeypatch.setattr(runtime, "get_graph", lambda state: graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: vector)
    calls: list = []
    fake_cs = SimpleNamespace(
        save_call_log=lambda **kw: calls.append(kw) or "log-id")
    app_state = SimpleNamespace(llm_client=llm, settings_store=None,
                                conversation_store=fake_cs)
    # 意图解析出图上不存在的实体 → seeds 零命中 → 兜底触发
    monkeypatch.setattr(retrieval, "parse_query_intent",
                        lambda *a, **k: {"entities": ["查无此人"],
                                         "keywords": [], "hop": 1})
    store = runtime.get_kg_store(app_state)
    kb = store.create_kb("兜底库")
    doc = store.create_document(kb["id"], "corpus.md", "text/markdown",
                                100, "", "hash-fb")
    store.replace_chunks(doc["id"], kb["id"],
                         [{"seq": i, "text": t} for i, t in enumerate(CORPUS[:5])])
    yield SimpleNamespace(store=store, llm=llm, graph=graph, vector=vector,
                          vector_hits=vector_hits, app_state=app_state,
                          kb=kb, doc=doc, obs_calls=calls)
    runtime.reset_runtime_cache()


class TestFulltextFallback:

    def test_zero_seeds_fallback_recalls_chunks(self, env):
        """图谱零命中 → 全文兜底召回,chunk 标注 source='fulltext'。"""
        kb = dict(env.kb, vectorEnabled=False)
        out = retrieval.hybrid_retrieve(env.app_state, kb, "合同纠纷怎么处理",
                                        conv_id="conv-ft")
        assert out["seeds"] == []
        assert out["chunks"], "零命中兜底未召回任何 chunk"
        assert all(c.get("source") == "fulltext" for c in out["chunks"])
        assert any("合同纠纷" in (c.get("text") or "") for c in out["chunks"])
        assert out["chunks"][0]["docName"] == "corpus.md"

    def test_zero_seeds_fallback_logged(self, env):
        """观测:兜底调用入 call_logs,stage='kg.fulltext_fallback'。"""
        kb = dict(env.kb, vectorEnabled=False)
        retrieval.hybrid_retrieve(env.app_state, kb, "民法典讲了什么",
                                  conv_id="conv-ft")
        ft = [c for c in env.obs_calls
              if c["request_data"].get("stage") == "kg.fulltext_fallback"]
        assert len(ft) == 1
        assert ft[0]["call_type"] == "fulltext"
        assert ft[0]["response_data"]["hits"] >= 1
        assert ft[0]["response_data"]["results"], "逐条命中明细缺失"
        assert ft[0]["conv_id"] == "conv-ft"

    def test_fallback_disabled_by_setting(self, env, monkeypatch):
        """开关 fulltext_fallback_enabled=false:零命中不兜底(不召回不落日志)。"""
        monkeypatch.setattr(
            retrieval, "_cfg",
            lambda app_state, key, default:
                False if key == "fulltext_fallback_enabled" else default)
        kb = dict(env.kb, vectorEnabled=False)
        out = retrieval.hybrid_retrieve(env.app_state, kb, "合同纠纷怎么处理")
        assert out["chunks"] == []
        assert not [c for c in env.obs_calls
                    if c["request_data"].get("stage") == "kg.fulltext_fallback"]

    def test_fallback_dedupes_with_vector(self, env):
        """向量路同父块命中去重:全文先入,向量不再重复并入。"""
        chunk_id = env.store.list_chunks(env.doc["id"])[0]["id"]
        env.vector_hits.append({"chunkId": f"{chunk_id}#0", "docId": env.doc["id"],
                                "seq": 0, "text": CORPUS[0], "score": 0.9})
        kb = dict(env.kb, vectorEnabled=True)
        out = retrieval.hybrid_retrieve(env.app_state, kb, CORPUS[0][:8])
        same_parent = [c for c in out["chunks"]
                       if str(c.get("chunkId") or "").split("#", 1)[0] == chunk_id]
        assert len(same_parent) == 1, "同父块被全文与向量重复召回"

    def test_vector_degraded_keeps_fulltext_hits(self, env, monkeypatch):
        """向量路失败降级:全文兜底命中不丢(降级早退须带回 fulltext chunks)。"""
        class BoomVector:
            def search(self, *a, **k):
                raise RuntimeError("vector down")

        monkeypatch.setattr(retrieval.runtime, "get_vector",
                            lambda state: BoomVector())
        kb = dict(env.kb, vectorEnabled=True)
        out = retrieval.hybrid_retrieve(env.app_state, kb, "合同纠纷怎么处理")
        assert out["diagnostics"]["vectorDegraded"] is True
        assert out["chunks"] and out["chunks"][0]["source"] == "fulltext"

    def test_seeds_present_skips_fallback(self, env, monkeypatch):
        """图谱有命中:不触发兜底(第三路只在零命中时补位)。"""
        monkeypatch.setattr(retrieval, "parse_query_intent",
                            lambda *a, **k: {"entities": ["甲"], "keywords": [],
                                             "hop": 1})
        monkeypatch.setattr(env.graph, "find_entities",
                            lambda kb_id, terms, limit=20: [
                                {"id": "x:甲", "name": "甲", "normalized": "甲",
                                 "type": "person", "description": "", "aliases": []}])
        monkeypatch.setattr(env.graph, "subgraph_around",
                            lambda *a, **k: {"nodes": [], "edges": []})
        kb = dict(env.kb, vectorEnabled=False)
        out = retrieval.hybrid_retrieve(env.app_state, kb, "甲在哪")
        assert out["seeds"], "前置:图谱路确有命中"
        assert not [c for c in env.obs_calls
                    if c["request_data"].get("stage") == "kg.fulltext_fallback"]
