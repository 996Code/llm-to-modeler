"""抽取原文锚定过滤(_anchor_filter)测试——线上质量事故的回归固化。

真实事故:通用模板 schema 的示例词(person.examples=["张三",...])被 LLM
照抄进抽取结果;"第三章 宏愿"章节标题、整句引文("九天玄刹,化为神雷…")
也成了图谱实体。_anchor_filter 是 prompt 规则的代码强制层。
"""
from domains.knowledge_graph.tasks import _anchor_filter

CHUNK = (
    "第一章 青云\n青云山脉巍峨高耸,山阳乃重镇\"河阳城\"。"
    "张小凡望着青云山,想起了草庙村的往事。普智盘膝而坐,默诵大梵般若。"
)


def _e(name, aliases=None):
    return {"name": name, "normalized_name": name, "type": "person",
            "description": "", "aliases": aliases or [], "chunk_id": ""}


class TestAnchorFilter:

    def test_example_word_hallucination_dropped(self):
        """schema 示例词不在原文中 → 丢弃(张三/产品部类事故)。"""
        ents = [_e("张小凡"), _e("张三"), _e("产品部"), _e("李四")]
        kept, dropped = _anchor_filter(ents, CHUNK)
        assert [e["name"] for e in kept] == ["张小凡"]
        assert dropped == 3

    def test_alias_match_kept(self):
        """name 是别称,但 alias 的原文写法在文中 → 保留。"""
        ents = [_e("小凡", aliases=["张小凡"])]
        kept, _ = _anchor_filter(ents, CHUNK)
        assert [e["name"] for e in kept] == ["小凡"]

    def test_chapter_heading_dropped(self):
        """章节标题不是实体(第X章/序章/楔子/番外…)。"""
        for name in ("第三章 宏愿", "第十二章 山谷", "序章", "楔子", "番外 岁月"):
            kept, dropped = _anchor_filter([_e(name)], CHUNK + "\n" + name)
            assert kept == [], name
            assert dropped == 1, name

    def test_sentence_dropped(self):
        """整句/含句读标点的 name 不是实体。"""
        for name in ("九天玄刹,化为神雷。煌煌天威,以剑引之",
                     "时光流逝,岁月如梭!",
                     "他问道:\"什么是修行?\""):
            kept, dropped = _anchor_filter([_e(name)], CHUNK + name)
            assert kept == [], name
            assert dropped == 1, name

    def test_overlong_name_dropped(self):
        kept, dropped = _anchor_filter([_e("超" * 31)], CHUNK)
        assert kept == [] and dropped == 1

    def test_real_entity_kept(self):
        ents = [_e("张小凡"), _e("河阳城"), _e("普智"), _e("大梵般若")]
        kept, dropped = _anchor_filter(ents, CHUNK)
        assert [e["name"] for e in kept] == ["张小凡", "河阳城", "普智", "大梵般若"]
        assert dropped == 0
