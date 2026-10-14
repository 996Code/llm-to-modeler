"""KG answer.j2 句尾引用规则测试。"""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

PROMPTS = Path(__file__).parent.parent.parent / "src" / "domains" / "knowledge_graph" / "prompts"

def render_answer(**kw):
    env = Environment(loader=FileSystemLoader(PROMPTS))
    return env.get_template("answer.j2").render(**kw)

def test_citation_rule_present():
    out = render_answer(kb_name="测试库", triples=[], node_details=[],
                        chunk_texts=["〔A〕内容一", "〔B〕内容二"], query="q")
    # 规则：句尾引用指令存在
    assert "句尾" in out and "[片段" in out
    # 编号列表注入：LLM 可用的编号被显式列出
    assert "片段1" in out and "片段2" in out
