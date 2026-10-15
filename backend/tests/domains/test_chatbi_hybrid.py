"""P1#6 检索混合化 —— 分词/关键词计分/RRF 融合纯函数。"""
from domains.chatbi.hybrid import keyword_scores, rrf_fuse, tokenize_query


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
