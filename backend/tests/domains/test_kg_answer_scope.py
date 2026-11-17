"""回答模板的收录范围声明注入与拒答分型(answer.j2,2026-11-17)。

无关问题干净拒答、不罗列检索资料的指令常驻模板;库声明(kb.description)
由调用方按 kb_scope 传入——空/缺声明不注入,行为同前(StrictUndefined
下 `is defined` 兜底,漏传不炸)。
"""

from domains.knowledge_graph.retrieval import _prompt_loader


def _render(**kw):
    base = dict(
        kb_name="强检目录",
        query="怎么申请营业执照",
        triples=[],
        node_details=[],
        chunk_texts=[],
    )
    base.update(kw)
    return _prompt_loader().render("knowledge_graph", "answer", **base)


def test_scope_declared_lands_in_prompt():
    scope = "收录强制检定计量器具目录与强检工作计量器具管理办法解读"
    prompt = _render(kb_scope=scope)
    assert "收录范围声明" in prompt
    assert scope in prompt, "库声明值应原样进入 prompt"
    assert "干净拒答" in prompt, "范围外问题应指向第 4 条无关情形(不罗列资料)"


def test_scope_absent_renders_without_decl_line():
    prompt = _render()  # 不传 kb_scope:漏传不炸,声明行为同前
    assert "收录范围声明" not in prompt


def test_refusal_split_rule_always_present():
    prompt = _render(kb_scope="")
    assert "不要罗列、概述" in prompt, "无关情形不罗列资料的指令应常驻"
    assert "相关但不完整" in prompt, "相关但不足情形应保留透明说明"
    assert "人工确认" in prompt
