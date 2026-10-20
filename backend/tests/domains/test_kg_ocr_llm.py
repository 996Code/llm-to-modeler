"""KG OCR 引擎配置——设置读取守卫 + llm 页识别回调构造(R1)。

覆盖:_ocr_engine_config 默认/合法/未配模型抛错;_make_llm_page_ocr 的
多模态消息形状(PNG base64 data URI)、模型覆盖与 call_logs stage 口径。
"""
from types import SimpleNamespace

import pytest

from domains.knowledge_graph import tasks
from domains.knowledge_graph.tasks import (
    _LLM_OCR_PROMPT, _make_llm_page_ocr, _ocr_engine_config,
)


def _with_cfg(monkeypatch, values: dict):
    monkeypatch.setattr(tasks, "_cfg", lambda state, key, default: values.get(key, default))


class TestOcrEngineConfig:

    def test_默认rapidocr(self, monkeypatch):
        _with_cfg(monkeypatch, {})
        assert _ocr_engine_config(object()) == ("rapidocr", "")

    def test_llm未配模型抛错(self, monkeypatch):
        _with_cfg(monkeypatch, {"ocr_engine": "llm", "ocr_model": ""})
        with pytest.raises(RuntimeError, match="ocr_model"):
            _ocr_engine_config(object())

    def test_llm配齐通过(self, monkeypatch):
        _with_cfg(monkeypatch, {"ocr_engine": "llm", "ocr_model": "qwen3.7"})
        assert _ocr_engine_config(object()) == ("llm", "qwen3.7")


class _FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return "  转录文本  "


class TestLlmPageOcr:

    def test_回调消息形状与模型覆盖(self):
        pytest.importorskip("PIL")
        from PIL import Image

        llm = _FakeLLM()
        cb = _make_llm_page_ocr(SimpleNamespace(llm_client=llm), "qwen3.7", "conv-1")
        assert cb(Image.new("RGB", (10, 10), "white")) == "转录文本"  # strip 过

        messages, kw = llm.calls[0]
        content = messages[0]["content"]
        assert isinstance(content, list)                       # 多模态 content
        assert content[0] == {"type": "text", "text": _LLM_OCR_PROMPT}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert kw["model"] == "qwen3.7"                        # 走 ocr_model 覆盖
        assert kw["stage"] == "kg.ocr" and kw["conv_id"] == "conv-1"
        assert kw["temperature"] == 0.0
