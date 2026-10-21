"""KG 对外暴露声明:config.yaml mcp_tools + kb_search 动态描述(知识库清单)。"""
import yaml
from types import SimpleNamespace
from sdk.tool import ToolResult


def test_config_declares_kb_search():
    from pathlib import Path
    cfg = yaml.safe_load(Path(
        "src/domains/knowledge_graph/config.yaml").read_text())
    assert cfg.get("mcp_tools") == ["kb_search"]


def test_mcp_description_lists_kbs(monkeypatch):
    from domains.knowledge_graph.tools.kb_search import KbSearchTool
    tool = KbSearchTool(SimpleNamespace())   # app_state 占位
    monkeypatch.setattr(
        "domains.knowledge_graph.runtime.get_kg_store",
        lambda st: SimpleNamespace(
            list_kbs=lambda: [{"name": "产品手册"}, {"name": "规章制度"}]))
    desc = tool.mcp_description(SimpleNamespace())
    assert "kb_search" in desc or "知识库" in desc
    assert "产品手册" in desc and "规章制度" in desc
    assert "回声" not in desc


def test_mcp_description_fail_open(monkeypatch):
    from domains.knowledge_graph.tools.kb_search import KbSearchTool
    tool = KbSearchTool(None)
    def boom(st):
        raise RuntimeError("store down")
    monkeypatch.setattr(
        "domains.knowledge_graph.runtime.get_kg_store", boom)
    assert tool.mcp_description(None) is None   # 回退默认描述
