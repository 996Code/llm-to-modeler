"""D15 首尾拉链重排(lost-in-middle)——当前位置序两端交替铺位。

LLM 长上下文注意力 U 形分布:开头结尾强、中段弱。不重排序(顺序=
rerank/召回序的相关度判断,重排会丢弃 rerank 判序——审查修复);
对当前位置拉链:第 1 名@0、第 2 名@末位、第 3 名@1…两端向中间收拢。
纯顺序变换:无增删、编号权威源自动对齐。
"""
import pytest

from domains.knowledge_graph.retrieval import _reorder_lost_in_middle


def _chunks(n):
    return [{"chunkId": f"c{i}", "score": 0.9 - i * 0.1} for i in range(n)]


class TestReorder:

    def test_首尾拉链铺位(self):
        # 6 块按当前序 a b c d e f → 期望 a f b e c d
        # (头=a 最高、尾=f 次低?不——当前位置序即相关度序,第 2 名 b 应占尾)
        src = _chunks(6)
        out = _reorder_lost_in_middle(src)
        ids = [c["chunkId"] for c in out]
        # 偶数位(头)保序 a c e,奇数位倒序 f d b 反转拼尾 → [a,c,e,f,d,b]?不对:
        # 实现:i%2==0 进 out(保序 a c e),i%2==1 进 tail(b d f)反转(f d b)
        # 最终 [a,c,e] + [f,d,b] = a c e f d b —— b(第 2 名)在末位 ✓
        assert ids == ["c0", "c2", "c4", "c5", "c3", "c1"]
        # 头=当前序第 1 名;尾=当前序第 2 名(高注意力双槽都是高分)
        assert out[0]["chunkId"] == "c0"
        assert out[-1]["chunkId"] == "c1"

    def test_次高分不落中段(self):
        # 审查锚:次高分必须占尾位(高注意力),不得沉 index 2 正中盲区
        out = _reorder_lost_in_middle(_chunks(4))
        ids = [c["chunkId"] for c in out]
        assert ids == ["c0", "c2", "c3", "c1"]

    def test_不重排序_rerank序保留(self):
        """审查修复锚:输入序=rerank 判序,输出只是位置重排——
        奇偶位分组的相对序不变(rerank 高相关块仍占头部渐近位)。"""
        # 构造 score 乱序(模拟 rerank 序与 score 不一致)
        src = [{"chunkId": f"c{i}", "score": (0.1 * i) % 1.0} for i in range(5)]
        out = _reorder_lost_in_middle(src)
        # 输出首元素=输入首元素(第 1 名仍在头),不按 score 重排
        assert out[0]["chunkId"] == "c0"
        # 纯排列:无增删
        assert {c["chunkId"] for c in out} == {c["chunkId"] for c in src}

    def test_三块以内原序返回(self):
        for n in (1, 2, 3):
            src = _chunks(n)
            assert _reorder_lost_in_middle(src) is src

    def test_纯顺序变换无增删(self):
        src = _chunks(8)
        out = _reorder_lost_in_middle(src)
        assert len(out) == len(src)
        assert sorted(c["chunkId"] for c in out) == sorted(c["chunkId"] for c in src)
