# -*- coding: utf-8 -*-
"""doc_parser 章节软边界单测:中文章节标题识别 + 切块不跨章 + overlap 不污染标题块。

来源:诛仙全本(156 万字/8 部/510 章)实测——纯文本书籍的"第 X 章"不是
markdown 标题,曾被当普通文本:28% 的块把上一章结尾和下一章开头切在一起
(其中一半是 overlap 阶段把上一章尾部拼到了标题块前面)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from sdk.doc_parser import _is_heading_line, chunk_text


class TestChapterHeadingDetection:

    def test_chinese_chapter_headings(self):
        for line in ("第一章 青云", "第十二章 重逢", "第300章 大结局",
                     "第三百零五章 决战", "第二部", "第十卷 天下"):
            assert _is_heading_line(line), f"应为标题: {line!r}"

    def test_special_chapters(self):
        for line in ("序章", "楔子", "尾声", "后记", "番外 外传一"):
            assert _is_heading_line(line), f"应为标题: {line!r}"

    def test_fullwidth_indent_tolerated(self):
        # 纯文本小说常见:标题行带全角空格缩进
        assert _is_heading_line("\u3000第一章 青云")

    def test_prose_not_misjudged(self):
        # 普通叙述句(无章/节/卷/部量词)不是标题
        for line in ("第二天,张三来了。", "第二天", "他们说的第三件事很重要。",
                     "第一部手机很重要。"):  # "部"前是"一"但"第一部手机"是叙述
            assert not _is_heading_line(line), f"不应为标题: {line!r}"

    def test_long_line_not_heading(self):
        # 超长行(>40 字)即使匹配章模式也是正文引用(如《山海经》引文)
        assert not _is_heading_line("第四卷《东山经》东次二经之首曰空桑之山北临食水东望沮吴南望沙陵西望泯泽" * 2)


class TestChunkChapterBoundary:

    def _novel_text(self) -> str:
        # 模拟小说结构:两章,每章正文超 target 长度
        body1 = "张小凡望着天空出神。" * 80
        body2 = "陆雪琪御剑而来。" * 80
        return (f"第一章 青云\n\n{body1}\n\n第二章 迷局\n\n{body2}")

    def test_chapter_title_starts_new_chunk(self):
        chunks = chunk_text(self._novel_text(), target_chars=300, overlap_chars=50)
        # 找到含第二章标题的块,标题必须在块首(不带上一章尾部)
        idx = next(i for i, c in enumerate(chunks) if "第二章" in c["text"])
        first_line = chunks[idx]["text"].split("\n", 1)[0].strip()
        assert first_line.startswith("第二章"), (
            f"章标题应在块首,实际块首: {first_line[:50]!r}")

    def test_overlap_not_preprended_to_title_chunk(self):
        # overlap 阶段:标题块不加前块尾部(曾只判 markdown #,中文章标题被拼上上一章结尾)
        chunks = chunk_text(self._novel_text(), target_chars=300, overlap_chars=50)
        idx = next(i for i, c in enumerate(chunks) if "第二章" in c["text"])
        assert chunks[idx]["text"].startswith("第二章"), (
            "标题块被拼上了上一章尾部(overlap 污染)")

    def test_markdown_heading_still_works(self):
        text = "# 标题A\n\n" + "内容甲。" * 60 + "\n\n## 标题B\n\n" + "内容乙。" * 60
        chunks = chunk_text(text, target_chars=300, overlap_chars=50)
        idx = next(i for i, c in enumerate(chunks) if "标题B" in c["text"])
        assert chunks[idx]["text"].split("\n", 1)[0].strip().startswith("##")
