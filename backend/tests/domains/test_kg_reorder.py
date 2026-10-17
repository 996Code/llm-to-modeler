"""D15 奇偶重排(lost-in-middle)——高分占头尾、低分沉中段。

LLM 长上下文注意力 U 形分布:开头结尾强、中段弱。重排把 score 高的
块放偶数位(0,2,4…头部+尾部),低的放奇数位(1,3,5…中段)。
纯顺序变换:无增删、编号权威源自动对齐。
"""
import pytest

from domains.knowledge_graph.retrieval import _reorder_lost_in_middle


def _chunks(scores):
    return [{"chunkId": f"c{i}", "score": s} for i, s in enumerate(scores)]


class TestReorder:

    def test_高分占头尾低分沉中段(self):
        # 6 块:排序后 [0.9,0.8,0.7,0.6,0.5,0.4],前半 0.9/0.8/0.7 占偶数位
        # (0,2,4),后半 0.4/0.5/0.6 反向占奇数位(1,3,5)——头尾是高分,
        # 中段是低分(0.4@1、0.5@3 沉在中段,0.6 收尾)
        out = _reorder_lost_in_middle(_chunks([0.4, 0.9, 0.5, 0.8, 0.6, 0.7]))
        scores = [c["score"] for c in out]
        assert scores == [0.9, 0.4, 0.8, 0.5, 0.7, 0.6]
        assert scores[0] == max(scores)            # 头=最高分
        # 偶数位(头尾)分数都高于相邻奇数位(中段)
        for i in range(1, len(scores) - 1, 2):
            assert scores[i] < scores[i - 1] and scores[i] < scores[i + 1]

    def test_纯顺序变换无增删(self):
        src = _chunks([0.3, 0.7, 0.2, 0.9, 0.4, 0.6, 0.1, 0.8])
        out = _reorder_lost_in_middle(src)
        assert len(out) == len(src)
        assert {c["chunkId"] for c in out} == {c["chunkId"] for c in src}

    def test_偶数位分数单调不升(self):
        out = _reorder_lost_in_middle(_chunks([0.1, 0.9, 0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6]))
        evens = [out[i]["score"] for i in range(0, len(out), 2)]
        assert evens == sorted(evens, reverse=True)

    def test_三块以内原序返回(self):
        for n in (1, 2, 3):
            src = _chunks([0.9 - i * 0.1 for i in range(n)])
            assert _reorder_lost_in_middle(src) is src

    def test_缺分数块按零分处理(self):
        # None 按 0 分:排序垫底进后半段,占尾部奇数位;头仍是最高分
        out = _reorder_lost_in_middle(_chunks([0.9, None, 0.8, None, 0.7, 0.6]))
        assert out[0]["score"] == pytest.approx(0.9)
        assert sum(1 for c in out if c["score"] is None) == 2
