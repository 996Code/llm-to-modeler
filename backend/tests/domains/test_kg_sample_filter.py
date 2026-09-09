"""归纳采样修复测试——目录页污染本体归纳的事故回归固化。

真实事故:诛仙全书开头是目录页(几百行"第X章 XXX"),_collect_samples
从文件头顺取样本,LLM 见到的全是章节列表,归纳出 book/chapter/volume/
section 的书籍结构本体,没有人物/门派/法宝。
"""
from domains.knowledge_graph.tasks import _heading_density, _pick_samples


def _toc_block(n_lines=40):
    return "\n".join(f"第{i}章 章节标题{i}" for i in range(1, n_lines + 1))


def _prose_block(idx=0):
    lines = [
        f"张小凡在河阳城的街道上走着,心里想着青云门的日子。这是第{idx}段正文。",
        "草庙村的往事如影随形,普智传授的大梵般若在他体内流转。",
        "他握紧了手里的烧火棍,望向青云山深处。",
    ]
    return "\n".join(lines)


class TestHeadingDensity:

    def test_toc_is_dense(self):
        assert _heading_density(_toc_block()) > 0.9

    def test_prose_is_sparse(self):
        assert _heading_density(_prose_block()) < 0.1

    def test_empty_is_max(self):
        assert _heading_density("") == 1.0
        assert _heading_density("\n\n") == 1.0


class TestPickSamples:

    def test_toc_head_replaced_by_middle_prose(self):
        """文件头是目录、中部是正文 → 应选中正文块,跳过目录块(核心事故场景)。"""
        texts = [_toc_block(), _toc_block(), _prose_block(1), _prose_block(2),
                 _prose_block(3), _prose_block(4)]
        samples = _pick_samples(texts, target=3, limit_chars=1500)
        assert len(samples) == 3
        assert all("章节标题" not in s for s in samples)
        assert all("张小凡" in s for s in samples)

    def test_middle_first(self):
        """中间块优先于头部块。"""
        texts = [_prose_block(i) for i in range(6)]
        texts[0] = "第一章 头部\n头部内容"  # 头部是短块,应被最后选到
        samples = _pick_samples(texts, target=3, limit_chars=1500)
        assert len(samples) == 3
        assert all("头部" not in s for s in samples)

    def test_fallback_when_all_toc(self):
        """全是目录块时退化为密度最低的块(硬失败好过错误本体)。"""
        texts = [_toc_block(30), _toc_block(35), _toc_block(30) + "\n正文一行而已"]
        samples = _pick_samples(texts, target=2, limit_chars=1500)
        assert len(samples) == 2
        # 密度最低的(含一行正文的)排前面
        assert "正文一行" in samples[0]

    def test_respects_limit_chars(self):
        texts = [_prose_block(i) * 100 for i in range(4)]
        samples = _pick_samples(texts, target=2, limit_chars=200)
        assert all(len(s) <= 200 for s in samples)
