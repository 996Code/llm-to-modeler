"""PDF 文字层垃圾字符与页级 OCR 候选判定 —— 控制字符清洗/密度分级/结构复核。

事故背景(99.22 实测):JJF 京 3024-2023 内嵌字体的 CMap 把均值符号 X̄ 映到
码点 0,pypdf 提取出 X\x00 进 kg_chunks 被 PG 拒收(text 不允许 NUL),
整文档导入失败。现行行为:
  - 零星坏字符(密度 ≤ 2%):剥离保留文字层,stats.ctrlStripped 留痕;
  - 密度 > 2%:文字层不可信,该页交 OCR 从图像重识别;
  - 短文字层页:空页直接候选;非空短页需结构证据(整页大图覆盖)才 OCR,
    纯短文本页(章末空白/整页图表短图注)信任文字层,不再误送 OCR。
"""
import io
import logging

import pytest

from sdk import doc_parser
from sdk.doc_parser import _nfkc, parse_to_text
from tests.sdk.test_doc_parser_pdf_ocr import (
    _minimal_text_pdf, _scanned_pdf)


# ── 假 Reader:页文本由用例注入(隔离 pypdf 提取本身,只测判定逻辑) ──

def _fake_pdf(monkeypatch, page_texts):
    import pypdf

    class _Page:
        def __init__(self, t):
            self._t = t

        def extract_text(self):
            return self._t

    class _Reader:
        def __init__(self, _):
            self.pages = [_Page(t) for t in page_texts]

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    # 结构复核依赖真 PDF 字节,这里整体旁路(结构分支由下方真 PDF 用例覆盖)
    monkeypatch.setattr(doc_parser, "_filter_scan_pages",
                        lambda data, texts, sparse: sparse)


class TestControlCharCleaning:

    def test_low_density_nul_stripped_without_ocr(self, monkeypatch):
        """零星坏字形(0.4% 密度)只剥离不 OCR——文字层 99% 是好的。"""
        ocr_called = []
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda *a, **k: ocr_called.append(a) or a[1])
        body = "X\x00——仪器 3 次测量值的算术平均值" + "。" * 500
        _fake_pdf(monkeypatch, [body])
        stats = {}
        text = parse_to_text("doc.pdf", b"fake", stats=stats)
        assert "\x00" not in text
        assert "仪器 3 次测量值的算术平均值" in text
        assert stats["ctrlStripped"] == 1
        assert stats["ocrPages"] == 0          # 未触发 OCR
        assert not ocr_called

    def test_high_density_garbage_page_goes_ocr(self, monkeypatch):
        """文字层大面积垃圾(>2%):不可信,交 OCR 从图像重识别。"""
        captured = {}
        def fake_fill(data, texts, pages, **kwargs):
            captured["pages"] = pages
            texts = list(texts)
            texts[pages[0]] = "OCR 重识别的正常文本内容,足够长可替换"
            return texts
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages", fake_fill)
        garbage = "".join(chr(c) for c in (0, 1, 2, 3, 4, 5)) * 10  # 60 个控制符
        _fake_pdf(monkeypatch, ["正文片段" + garbage + "结尾"])
        text = parse_to_text("doc.pdf", b"fake")
        assert captured["pages"] == [0]
        assert "OCR 重识别" in text                 # 更长的 OCR 结果胜出
        assert "\x00" not in text

    def test_all_formats_exit_strips_controls(self):
        """出口不变量:NFKC + C0 剥离(保留 \\t\\n\\r),对三种格式统一。"""
        assert _nfkc("a\x00b\x01c\x7f") == "abc"
        assert _nfkc("a\tb\nc\rd") == "a\tb\nc\rd"
        assert _nfkc("first\x0csecond\x0bthird") == "first\nsecond\nthird"


class TestSurrogateRepair:
    """孤立代理对:坏 CMap 只吐出 UTF-16 代理对的一半,无法 UTF-8 编码,
    psycopg 写入抛 UnicodeEncodeError(与 NUL 同源事故的另一半)。"""

    def test_lone_surrogate_dropped(self, monkeypatch):
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda *a, **k: a[1])
        _fake_pdf(monkeypatch, ["X\ud835——仪器 3 次测量值的算术平均值" + "。" * 500])
        stats = {}
        text = parse_to_text("doc.pdf", b"fake", stats=stats)
        assert "\ud835" not in text
        assert "仪器 3 次测量值的算术平均值" in text
        assert stats["ctrlStripped"] == 1

    def test_surrogate_pair_combined_then_nfkc(self):
        """相邻高低代理合并为星面字符,再被 NFKC 折叠为 ASCII 等价——
        数学斜体 𝑋(\\ud835\\udc69)检索时按字面 X 可命中。"""
        assert _nfkc("A\ud835\udc00B") == "AAB"   # \\ud835\\udc00=𝐀 →NFKC→ A

    def test_pure_surrogate_page_empties_to_ocr_candidate(self, monkeypatch):
        """整页只剩孤立代理:清洗后为空文字层,照常进 OCR 候选。"""
        captured = {}
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda data, texts, pages, **kwargs:
                            captured.setdefault("pages", pages) or texts)
        _fake_pdf(monkeypatch, ["\ud835\ud83b\ud800"])
        parse_to_text("doc.pdf", b"fake")
        assert captured["pages"] == [0]


class TestStructuralSparseFilter:
    """短文字层页结构复核:大图覆盖才 OCR,纯短文本页信任。"""

    def test_sparse_text_without_image_trusted(self, monkeypatch):
        """章末空白/短图注页(有字体、无大图):不再误送 OCR。"""
        called = []
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda *a, **k: called.append(a) or a[1])
        text = parse_to_text("doc.pdf", _minimal_text_pdf("Hi"))
        assert text == "Hi"
        assert not called

    def test_sparse_text_with_fullpage_image_ocr(self, monkeypatch):
        """非空短文字层 + 整页大图(扫描页带水印字):结构证据成立,送 OCR。"""
        pytest.importorskip("PIL")
        captured = {}
        def fake_fill(data, texts, pages, **kwargs):
            captured["pages"] = pages
            return texts
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages", fake_fill)
        parse_to_text("doc.pdf", _image_page_pdf("Hi"))
        assert captured["pages"] == [0]

    def test_empty_page_always_candidate(self, monkeypatch):
        """空文字层无可失去,不问结构直接候选(与旧口径一致)。"""
        captured = {}
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda data, texts, pages, **kwargs:
                            captured.setdefault("pages", pages) or texts)
        parse_to_text("doc.pdf", _scanned_pdf(["高压电容电桥检定规程"]))
        assert captured["pages"] == [0]

    def test_dense_text_page_never_candidate(self, monkeypatch):
        """正常文字页无论结构如何都不进 OCR(快路径)。"""
        called = []
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda *a, **k: called.append(a) or a[1])
        parse_to_text("doc.pdf", _image_page_pdf("Hello dense text layer page"))
        assert not called


class TestWatermarkScanWarning:

    def test_watermark_scan_doc_warns(self, monkeypatch, caplog):
        """整册短文字层且 OCR 无补充:任务日志告警疑似水印扫描件。"""
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda data, texts, pages, **kwargs: texts)
        _fake_pdf(monkeypatch, ["出版社水印文字"] * 5)
        with caplog.at_level(logging.WARNING, logger="sdk.doc_parser"):
            parse_to_text("doc.pdf", b"fake", stats={})  # stats 必传:告警依赖 ocrFilled
        assert "疑似整册扫描件" in caplog.text

    def test_normal_doc_no_warning(self, monkeypatch, caplog):
        _fake_pdf(monkeypatch, ["正常正文" * 50] * 5)
        with caplog.at_level(logging.WARNING, logger="sdk.doc_parser"):
            parse_to_text("doc.pdf", b"fake")
        assert "疑似整册扫描件" not in caplog.text


# ── 带整页图片的手写 PDF(短文字层 + 大图覆盖的结构证据) ──────────

def _image_page_pdf(text: str, size=(612, 792)) -> bytes:
    """单页 PDF:一张整页 JPEG 图 + 一行文字层(扫描页带水印字的最小形态)。"""
    pytest.importorskip("PIL")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(buf, format="JPEG")
    jpg = buf.getvalue()
    stream = (f"q {size[0]} 0 0 {size[1]} 0 0 cm /Im1 Do Q "
              f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET").encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
         f"/Resources << /Font << /F1 5 0 R >> "
         f"/XObject << /Im1 6 0 R >> >> /Contents 4 0 R >>").encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
        b"/Length " + str(len(jpg)).encode() + b" >>\nstream\n" + jpg
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
