"""P1#6 检索混合化 —— 分词/关键词计分/RRF 融合纯函数 + T2 retrieve 接线。"""
from domains.chatbi.hybrid import keyword_scores, rrf_fuse, tokenize_query
from domains.chatbi.models import SemanticModelContent
from domains.chatbi.retrieval import retrieve

from tests.domains.test_chatbi_evalset import (
    EVALSET, EVAL_SCOPE, SIX_TABLE_MODELS, make_eval_fixture,
    recall_at_k, register_query_vector)


class TestTokenize:

    def test_chinese_bigram(self):
        """中文 2-gram:'员工薪资' → 员工/工薪/薪资(单字过滤)。"""
        toks = tokenize_query("员工薪资")
        assert "员工" in toks and "薪资" in toks

    def test_english_and_number_words(self):
        """连续英文/数字整词:'查 order 表 top10' → order/top10。"""
        toks = tokenize_query("查 order 表 top10")
        assert "order" in toks and "top10" in toks

    def test_stopwords_dropped(self):
        """停用词(的/了/查询/所有/一下等)不产生 2-gram 噪声主项。"""
        toks = tokenize_query("查询所有的员工")
        assert "查询" not in toks or toks.count("查询") == 0


class TestKeywordScores:

    def test_bidirectional_match(self):
        """双向包含:表文本含查询词 + 查询点名表名都计分。"""
        from domains.chatbi.models import Column, Model
        emp = Model(name="employee", display_name="员工表",
                    description="员工基本信息",
                    columns=[Column(name="name", display_name="姓名",
                                    data_type="VARCHAR")])
        scores = keyword_scores("查员工姓名", [emp])
        assert "employee" in scores and scores["employee"] > 0

    def test_no_match_empty(self):
        from domains.chatbi.models import Column, Model
        m = Model(name="t", display_name="无关表", description="x",
                  columns=[])
        assert keyword_scores("查员工姓名", [m]) == {}

    def test_pointed_table_name_wins(self):
        """查询点名英文表名 → 精确包含强命中。"""
        from domains.chatbi.models import Column, Model
        m = Model(name="orders", display_name="订单表", description="",
                  columns=[])
        s1 = keyword_scores("orders", [m])
        s2 = keyword_scores("订单数据", [m])
        assert s1.get("orders", 0) > s2.get("orders", 0)

    def test_reverse_match_requires_word_boundary(self):
        """反向点名须词边界:'按user_id查'不得给 user 表 +3(裸子串误命中,
        T1 审查 major);'user 表'才点名 user。"""
        from domains.chatbi.models import Model
        user = Model(name="user", display_name="用户表", description="",
                     columns=[])
        s_sub = keyword_scores("按user_id查数据", [user])
        s_full = keyword_scores("看 user 表的数据", [user])
        assert s_sub.get("user", 0.0) == 0.0, f"子串误命中: {s_sub}"
        assert s_full.get("user", 0.0) >= 3.0


class TestRRFFuse:

    def test_basic_fusion(self):
        """两路各有排名 → RRF 融合;双路命中者胜单路。"""
        fused = rrf_fuse(["a", "b", "c"], ["c", "d"])
        assert fused[0] in ("a", "c")  # a:1/61, c:1/61+1/62 双路
        # c 双路命中,融合分必然高于单路的 d
        assert fused.index("c") < fused.index("d")

    def test_empty_keyword_path(self):
        """关键词路空 → 融合退化为向量序。"""
        assert rrf_fuse(["a", "b"], []) == ["a", "b"]

    def test_union_all_kept(self):
        """两路并集全保留(未入任一路者不出现;单路者排后按原序)。"""
        fused = rrf_fuse(["a", "b"], ["c", "a"])
        assert set(fused) == {"a", "b", "c"} and len(fused) == 3


# ── T2 集成:融合进 retrieve 管线 ─────────────────────────────


class TestRetrieveHybrid:
    """retrieve() 混合检索接线:签名(content/hybrid_enabled)+融合段行为。"""

    def _content(self):
        return SemanticModelContent(models=list(SIX_TABLE_MODELS))

    def test_hybrid_off_by_default(self):
        """默认关(不传参):行为与现状一致——纯向量。"""
        store, embedder, llm = make_eval_fixture()
        register_query_vector(llm, "查询所有员工的名字", ["employee"])
        r1 = retrieve("查询所有员工的名字", store, embedder,
                      skip_llm_refine=True, scope=EVAL_SCOPE)
        r2 = retrieve("查询所有员工的名字", store, embedder,
                      skip_llm_refine=True, scope=EVAL_SCOPE,
                      content=self._content(), hybrid_enabled=False)
        assert [m["name"] for m in r1.models] == [m["name"] for m in r2.models]

    def test_keyword_path_rescues_vector_miss(self):
        """关键词路独立价值:向量对不上的表靠关键词补召回(RRF 融合进候选)。
        场景:假向量只注册了 department(员工表向量缺失=向量路漏),
        开关开+content 注入 → employee 靠'员工'关键词命中并进候选。"""
        store, embedder, llm = make_eval_fixture()
        register_query_vector(llm, "查询所有员工的名字", ["department"])
        r = retrieve("查询所有员工的名字", store, embedder,
                     skip_llm_refine=True, scope=EVAL_SCOPE,
                     content=self._content(), hybrid_enabled=True)
        names = [m["name"] for m in r.models]
        assert "employee" in names, f"关键词路未补召回: {names}"

    def test_hybrid_no_content_degrades(self):
        """content=None(未扫描/未注入):关键词路跳过,纯向量不报错。"""
        store, embedder, llm = make_eval_fixture()
        register_query_vector(llm, "查询所有员工的名字", ["employee"])
        r = retrieve("查询所有员工的名字", store, embedder,
                     skip_llm_refine=True, scope=EVAL_SCOPE,
                     content=None, hybrid_enabled=True)
        assert r.models and r.models[0]["name"] == "employee"


def test_hybrid_on_baseline_unchanged():
    """开关开+content 注入跑 60 条评测集:recall@5 仍 =1.000。

    关键词路只补不删(RRF 并集融合,向量命中不丢)——开关开不得
    引入基线回退(P1#6 收益数字的护栏面)。"""
    store, embedder, llm = make_eval_fixture()
    content = SemanticModelContent(models=list(SIX_TABLE_MODELS))
    scores = []
    misses = []
    for case in EVALSET:
        register_query_vector(llm, case["query"], case["expected_tables"])
        r = retrieve(case["query"], store, embedder,
                     skip_llm_refine=True, scope=EVAL_SCOPE,
                     content=content, hybrid_enabled=True)
        got = [m["name"] for m in r.models]
        s = recall_at_k(set(case["expected_tables"]), got, 5)
        scores.append(s)
        if s < 1.0:
            misses.append((case["id"], case["expected_tables"], got[:5]))
    mean = sum(scores) / len(scores)
    assert mean == 1.000, f"开关开基线回退 {mean:.3f}: {misses}"


def test_hybrid_and_rerank_both_enabled():
    """hybrid+rerank 双开(4 开关组合缺的最后一种):融合序被 rerank 排,
    rerank 失败回退融合序——两层增强叠加不互斥不崩(T2 审查 minor)。"""
    store, embedder, llm = make_eval_fixture()
    content = SemanticModelContent(models=list(SIX_TABLE_MODELS))
    # 向量只指 department(员工表漏)——关键词路补 employee,rerank 再排
    register_query_vector(llm, "查询所有员工的名字", ["department"])
    llm.rerank = lambda q, texts, conv_id=None, stage=None: list(range(len(texts)))
    r = retrieve("查询所有员工的名字", store, embedder,
                 skip_llm_refine=True, scope=EVAL_SCOPE,
                 content=content, hybrid_enabled=True,
                 llm=llm, rerank_enabled=True)
    names = [m["name"] for m in r.models]
    assert "employee" in names and "department" in names
