"""PDF 解析 OCR 兜底 —— 扫描页自动识别 / 文字层 PDF 不走 OCR / 依赖缺失降级。

扫描件用 PIL 生成真实"整页图片 PDF"(中文文字画进位图,无文字层),
端到端验证 RapidOCR 兜底链路。
"""
import sys

import pytest

from sdk import doc_parser
from sdk.doc_parser import parse_to_text


# ── 测试素材构造 ─────────────────────────────────────────────

def _minimal_text_pdf(text: str) -> bytes:
    """手写最小 PDF:单页,内容流一个 Tj 文本对象(有真实文字层)。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def _scanned_pdf(lines: list) -> bytes:
    """PIL 生成扫描件:中文文字画进整页位图,存为无文字层 PDF。"""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont
    font_path = "/System/Library/Fonts/STHeiti Medium.ttc"
    import os
    if not os.path.exists(font_path):
        pytest.skip("无系统中文字体(非 macOS 环境)")
    img = Image.new("RGB", (1240, 1754), "white")  # A4 @150dpi
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 48)
    y = 200
    for line in lines:
        draw.text((120, y), line, fill="black", font=font)
        y += 90
    import io
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150)
    return buf.getvalue()


# ── 测试 ─────────────────────────────────────────────────────

class TestPdfParse:

    def test_text_pdf_no_ocr(self, monkeypatch):
        """文字层 PDF 走快路径:OCR 兜底函数不被调用。"""
        called = []
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages",
                            lambda *a, **k: called.append(a) or a[1])
        text = parse_to_text("doc.pdf", _minimal_text_pdf("Hello text layer PDF content here"))
        assert "Hello text layer" in text
        assert not called

    def test_sparse_text_triggers_ocr_path(self, monkeypatch):
        """文字层低于阈值(扫描页水印)的页进入 OCR 兜底路径。"""
        captured = {}
        def fake_fill(data, texts, sparse, stats=None):
            captured["sparse"] = sparse
            return texts
        monkeypatch.setattr(doc_parser, "_ocr_fill_pages", fake_fill)
        parse_to_text("doc.pdf", _minimal_text_pdf("Hi"))
        assert captured["sparse"] == [0]

    def test_ocr_deps_missing_degrades(self, monkeypatch):
        """OCR 依赖缺失:不抛异常,保持原文字层(空)返回。"""
        monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
        text = parse_to_text("doc.pdf", _minimal_text_pdf("Hi"))
        assert text == "Hi"  # 原文字层原样返回

    def test_scanned_pdf_ocr_end_to_end(self):
        """端到端:整页图片 PDF(无文字层)→ OCR 识别出中文,stats 出参带回页数。"""
        pytest.importorskip("rapidocr_onnxruntime")
        pytest.importorskip("pypdfium2")
        lines = ["高压电容电桥检定规程", "本规程经国家质量监督检验检疫总局批准"]
        stats = {}
        text = parse_to_text("scan.pdf", _scanned_pdf(lines), stats=stats)
        assert "检定规程" in text
        assert "质量监督" in text
        assert stats["pages"] == 1
        assert stats["ocrPages"] == 1
        assert stats["ocrFilled"] == 1

    def test_precheck_skips_ocr(self):
        """上传预检(ocr=False):扫描件不做 OCR(秒回),stats 如实记 0。"""
        stats = {}
        text = parse_to_text("scan.pdf", _scanned_pdf(["高压电容电桥检定规程"]), ocr=False, stats=stats)
        assert text == ""  # 预检只验坏文件,文字层为空不算失败
        assert stats["ocrPages"] == 0
