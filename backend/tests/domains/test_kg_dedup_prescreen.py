"""P2#9 消歧预筛——类型分桶+Levenshtein/字符重合双阈值+别名桥接。

仅可疑对送 LLM 身份复合(千级库调用量降一个数量级)。
阈值参照 RAGFlow entity_resolution.py:Levenshtein ≤ 2 或 字符重合 ≥ 0.5。
"""
from domains.knowledge_graph.dedup_prescreen import (
    char_overlap, levenshtein, prescreen_pairs)


def _e(name, etype, aliases=None, normalized=None):
    return {"name": name, "normalized": normalized or name, "type": etype,
            "aliases": aliases or [], "description": "", "source_docs": ["d"]}


class TestPrescreenPairs:

    def test_same_type_only(self):
        """类型分桶:不同类型不配对(person vs organization 同名不算可疑)。"""
        pairs = prescreen_pairs([_e("张三", "person"), _e("张三", "organization")])
        assert pairs == []

    def test_levenshtein_near_match(self):
        """编辑距离 ≤ 2 视为可疑(错别字:张三/张叁)。"""
        pairs = prescreen_pairs([_e("张三", "person"), _e("张叁", "person")])
        assert ("张三", "张叁") in pairs or ("张叁", "张三") in pairs

    def test_char_overlap_match(self):
        """字符重合 ≥ 0.5 视为可疑(孙悟空/孙大圣:交集{孙,大,圣}?)——
        用真 ≥0.5 的对:孙悟空/孙悟饭(交集{孙,悟} 2/并集4=0.5)。"""
        pairs = prescreen_pairs([_e("孙悟空", "person"), _e("孙悟饭", "person")])
        assert len(pairs) == 1

    def test_alias_bridging(self):
        """别名桥接:字面差异大但别名互指(齐天大圣/孙悟空)。"""
        pairs = prescreen_pairs([
            _e("孙悟空", "person", aliases=["齐天大圣"]),
            _e("齐天大圣", "person"),
        ])
        assert len(pairs) == 1

    def test_dissimilar_not_paired(self):
        """字面差异大且无别名桥接:不可疑,不送 LLM。"""
        pairs = prescreen_pairs([_e("孙悟空", "person"), _e("猪八戒", "person")])
        assert pairs == []

    def test_empty_and_single(self):
        assert prescreen_pairs([]) == []
        assert prescreen_pairs([_e("独", "person")]) == []

    def test_cap_keeps_most_suspicious(self):
        """cap 截断顺序回归(审查 Important):先全量判可疑再按可疑度升序
        入选——「张三丰 + 变体池 + 张三」场景,lev=1 的高可疑对(张三/张三丰)
        不得被 lev=2 的平庸变体挤掉(首版先到先得,名录顺序不稳定=随机)。
        同分平局按 normalized 字典序确定(行为可复现)。"""
        entities = [_e("张三丰", "person")]
        # 变体池:两字距(lev=2)的可疑对——分数比 张三/张三丰(lev=1)差
        entities += [_e(f"张三丰{c}{c}", "person") for c in "abcdefghij"]
        entities.append(_e("张三", "person"))
        pairs = prescreen_pairs(entities)
        assert ("张三", "张三丰") in pairs  # 最可疑对必须在
        # 确定性:同输入两次结果一致(cap 竞争不随机)
        assert pairs == prescreen_pairs(entities)

    def test_performance_5000(self):
        """千级验收:5000 实体预筛 < 2s(纯 Python 无 IO;首字分桶+单实体
        对上限后 O(n²) 降为桶内配对——极端同前缀构造 1.7s,真实人名/地名
        场景远快于此)。"""
        import time
        entities = [_e(f"实体{i:04d}", "person") for i in range(5000)]
        t0 = time.monotonic()
        pairs = prescreen_pairs(entities)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        # 单实体对上限:总对数 ≤ 实体数 × 上限 / 2(每对计两实体各 1)
        assert len(pairs) <= 5000 * 8


class TestHelpers:

    def test_levenshtein(self):
        assert levenshtein("abc", "abc") == 0
        assert levenshtein("abc", "ab") == 1
        assert levenshtein("kitten", "sitting") == 3

    def test_char_overlap(self):
        assert char_overlap("孙悟空", "孙行者") == 1 / 5  # 交集{孙},并集 5 字
        assert char_overlap("abc", "xyz") == 0.0
        assert char_overlap("孙悟饭", "孙悟空") == 0.5      # 交集{孙,悟},并集 4
