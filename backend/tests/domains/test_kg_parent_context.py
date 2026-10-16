"""父子回带(Dify 对标)—— 向量命中子段回带父块上下文窗口。

覆盖:窗口计算(居中/章首/章尾/句读收口/cap 护栏/定位失败)、
hybrid_retrieve 端到端回带(真实 store 父块 + fake 向量命中子块)、
降级语义(孤儿子块/父块回查异常/定位失败不静默丢内容)、
linearize 预算护栏、answer_question sources 标注。
"""
import pytest

from domains.knowledge_graph import retrieval
from sdk.doc_parser import sub_chunks_for_embedding

# 复用 m4 的检索 fake 环境(MockLLM/RetrievalFakeGraph/RetrievalFakeVector)
from tests.domains.test_knowledge_graph_m4 import env  # noqa: F401


# ── 窗口计算(纯函数) ─────────────────────────────────────────

def _chapter(n: int = 60) -> str:
    """n 句、每句 6 字的均匀章节(第000话。~ 第059话。)——边界可手算。"""
    return "".join(f"第{i:03d}话。" for i in range(n))


class TestParentContextWindow:

    def test_centered_window_with_context(self):
        """命中段居中:窗口含命中段+前后各若干句,边界句读对齐。"""
        parent = _chapter()
        child = "第030话。"
        w = retrieval.parent_context_window(parent, child, before=24, after=24)
        assert child in w
        # before=24 字 ≈ 4 句:窗口从第 26 句起(第 25 句完整保留在界外)
        assert "第026话。" in w and "第025话。" not in w
        assert "第035话。" in w and "第036话。" not in w

    def test_window_snaps_to_sentence_boundary(self):
        """窗口头尾收口到句读边界:不以句中字开头/结尾。"""
        parent = _chapter()
        child = "第030话。"
        w = retrieval.parent_context_window(parent, child, before=25, after=25)
        # 25 字不在句边界上,收口应把头推到上一句号后(第026话。开头)
        assert w.startswith("第")
        assert w.endswith("。")

    def test_child_at_chapter_start(self):
        """命中段在章首:窗口无前文,从 0 起。"""
        parent = _chapter()
        w = retrieval.parent_context_window(parent, "第000话。", before=24, after=24)
        assert w.startswith("第000话。")
        assert "第000话。" in w and "第004话。" in w

    def test_child_at_chapter_end(self):
        """命中段在章尾:窗口无后文,到 len(parent) 止。"""
        parent = _chapter()
        w = retrieval.parent_context_window(parent, "第059话。", before=24, after=24)
        assert w.endswith("第059话。")

    def test_child_not_in_parent_returns_none(self):
        """定位失败 → None(调用方降级子段原文)。"""
        assert retrieval.parent_context_window(_chapter(), "不存在的句子。") is None
        assert retrieval.parent_context_window("", "x") is None
        assert retrieval.parent_context_window(_chapter(), "") is None

    def test_cap_hard_limit(self):
        """窗口超 cap 从更宽一侧硬裁:不超 cap(护栏优先于句读对齐)。"""
        parent = _chapter(600)
        child = "第300话。"
        w = retrieval.parent_context_window(parent, child,
                                            before=5000, after=5000, cap=60)
        assert len(w) <= 60
        assert child in w  # 子段保留

    def test_child_with_inserted_newlines_still_located(self):
        """子块文本经 _hard_split 重组(插入 \\n/吞空白)仍能定位——
        两侧压空白匹配,窗口切自父块原文。"""
        parent = "甲是研发部经理。乙是产品部经理。丙是测试部经理。"
        # _hard_split 重组形态:句间 \n 是父块中不存在的(sub_chunks
        # 的 size 下限 300,短文本造不出多段,这里手工构造同形态)
        child = "甲是研发部经理。\n乙是产品部经理。"
        w = retrieval.parent_context_window(parent, child, before=10, after=10)
        assert w is not None
        assert "甲是研发部经理" in w  # 窗口是父块原文(无重组 \n)
        assert "\n" not in w


# ── hybrid_retrieve 端到端回带 ───────────────────────────────

def _kb_with_chapter(env, name: str, chapter_text: str):
    """真实 store 建库+文档+单章块;返回 (kb dict, chunk)。"""
    kb = env.store.create_kb(f"库-{name}", schema_json={"relation_types": []})
    doc = env.store.create_document(
        kb["id"], f"{name}.md", "text/markdown", len(chapter_text.encode()), "",
        "hash-" + name)
    env.store.replace_chunks(doc["id"], kb["id"], [{"seq": 0, "text": chapter_text}])
    chunks = env.store.list_chunks(doc["id"])
    kb_dict = {"id": kb["id"], "name": kb["name"], "description": "",
               "schema": {"relation_types": []},
               "vectorEnabled": True, "vectorDim": 3}
    return kb_dict, chunks[0]


class TestHybridRetrieveParentContext:

    def test_vector_hit_gets_parent_window(self, env, monkeypatch):
        """向量命中子段 → text 换成父块上下文窗口,标注 childSeq。"""
        chapter = _chapter(300)   # 1800 字,500 字子切成 5 段
        kb, chunk = _kb_with_chapter(env, "回带", chapter)
        subs = sub_chunks_for_embedding(chapter, size=500)
        assert len(subs) >= 3
        env.vector.hits = [{
            "chunkId": f"{chunk['id']}#2", "docId": chunk["docId"],
            "seq": 0, "text": subs[2], "score": 0.9,
        }]
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])
        out = retrieval.hybrid_retrieve(env.app_state, kb, "第150话讲了什么?")
        assert out["chunks"]
        c = out["chunks"][0]
        assert c["parentContext"] is True
        assert c["childSeq"] == 2
        assert c["childText"] == subs[2]
        # 回带文本是父块窗口:含命中段 + 前后上下文,比子段长
        assert len(c["text"]) > len(subs[2])
        assert "第150话。" in c["text"]
        assert out["diagnostics"]["parentContextHits"] == 1

    def test_orphan_child_keeps_own_text(self, env, monkeypatch):
        """孤儿子块(父块已删,向量残留)→ 保留子段原文,不炸不丢。"""
        chapter = _chapter(20)
        kb, chunk = _kb_with_chapter(env, "孤儿", chapter)
        env.vector.hits = [{
            "chunkId": f"{chunk['id']}#0", "docId": chunk["docId"],
            "seq": 0, "text": "第000话。", "score": 0.9,
        }]
        # 父块删除(模拟重导入先删后插后的向量残留)
        env.store.replace_chunks(chunk["docId"], kb["id"], [])
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])
        out = retrieval.hybrid_retrieve(env.app_state, kb, "第0话?")
        assert out["chunks"]
        c = out["chunks"][0]
        assert "parentContext" not in c
        assert c["text"] == "第000话。"   # 降级:子段原文
        assert c["childSeq"] == 0          # 序号仍可标(前端展示用)
        assert out["diagnostics"]["parentContextHits"] == 0

    def test_store_lookup_failure_degrades(self, env, monkeypatch):
        """父块回查异常 → 整批降级子段原文,检索不失败。"""
        chapter = _chapter(20)
        kb, chunk = _kb_with_chapter(env, "回查挂", chapter)
        env.vector.hits = [{
            "chunkId": f"{chunk['id']}#0", "docId": chunk["docId"],
            "seq": 0, "text": "第000话。", "score": 0.9,
        }]
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])

        def boom(self, ids):
            raise RuntimeError("pg down")
        monkeypatch.setattr(type(env.store), "get_chunks_by_ids", boom)
        out = retrieval.hybrid_retrieve(env.app_state, kb, "第0话?")
        assert out["chunks"][0]["text"] == "第000话。"

    def test_fulltext_hit_not_callbacked(self, env):
        """全文兜底命中的整章块(无 #i)本就是父块,不回带。"""
        chapter = _chapter(20)
        kb, chunk = _kb_with_chapter(env, "全文路", chapter)
        env.llm.query_result = {"entities": ["查无"], "keywords": [], "hop": 1}
        kb["vectorEnabled"] = False   # 只走图谱(零命中)+全文兜底
        out = retrieval.hybrid_retrieve(env.app_state, kb, "第000话讲了什么?")
        # 前置确认:全文兜底真实命中(查询 gram "000" 命中章内 alnum 段)
        assert out["chunks"] and out["chunks"][0]["chunkId"] == chunk["id"]
        for c in out["chunks"]:
            assert "childSeq" not in c and "parentContext" not in c

    def test_parent_dedup_still_applies(self, env, monkeypatch):
        """同父多子段命中仍按父去重(回带后一名额一窗口,不重复)。"""
        chapter = _chapter(300)
        kb, chunk = _kb_with_chapter(env, "去重", chapter)
        subs = sub_chunks_for_embedding(chapter, size=500)
        env.vector.hits = [
            # 真实向量检索按分数降序返回:0.9 的子段在前(去重留首个)
            {"chunkId": f"{chunk['id']}#2", "docId": chunk["docId"],
             "seq": 0, "text": subs[2], "score": 0.9},
            {"chunkId": f"{chunk['id']}#1", "docId": chunk["docId"],
             "seq": 0, "text": subs[1], "score": 0.8},
        ]
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])
        out = retrieval.hybrid_retrieve(env.app_state, kb, "第150话?")
        assert len(out["chunks"]) == 1          # 同父只留最高分
        assert out["chunks"][0]["childSeq"] == 2


# ── linearize 预算护栏 + sources 标注 ─────────────────────────

class TestLinearizeAndSources:

    def test_linearize_keeps_window_not_600(self):
        """回带窗口整段进 chunk_texts(旧 600 截断会把窗口腰斩)。"""
        retrieved = {"chunks": [{
            "docName": "文档", "text": "句。" * 1500,  # 4500 字窗口
        }]}
        ctx = retrieval.linearize_context(retrieved)
        assert len(ctx["chunk_texts"][0]) > 2000  # 600 口径下只有 ~606

    def test_linearize_total_budget_floor(self):
        """总预算耗尽后:条目保留(不静默丢),退回 600 保底。"""
        chunks = [{"docName": "d", "text": "句。" * 2500} for _ in range(20)]
        ctx = retrieval.linearize_context({"chunks": chunks})
        assert len(ctx["chunk_texts"]) == 20          # 条目全保留
        # 前几条吃满预算(5000/条),后续退 600(前缀〔d〕3 字)
        assert len(ctx["chunk_texts"][0]) >= 5000
        assert len(ctx["chunk_texts"][-1]) <= 603

    def test_linearize_budget_priority_over_order(self):
        """预算分配与顺序解耦:回带窗口优先吃预算——图谱零命中时
        全文兜底整章块先入列,不得挤掉后入列的向量回带窗口。"""
        chunks = [{"docName": "d", "text": "句。" * 2500, "source": "fulltext"}
                  for _ in range(6)]
        chunks.append({"docName": "d", "text": "句。" * 2500,
                       "parentContext": True, "score": 0.9})   # 末位=回带窗口
        ctx = retrieval.linearize_context({"chunks": chunks})
        # 末位回带窗口整段(优先吃预算),先入列的兜底块有被挤到 600 的
        assert len(ctx["chunk_texts"][6]) >= 5000
        assert any(len(t) <= 603 for t in ctx["chunk_texts"][:6])

    def test_answer_chunks_slimmed_for_user(self, env, monkeypatch):
        """用户级返回的 raw chunks 不带内部诊断字段(childText/parentChars);
        sources.chunks 的展示字段(childSeq/parentContext)保留。"""
        chapter = _chapter(300)
        kb, chunk = _kb_with_chapter(env, "瘦身", chapter)
        subs = sub_chunks_for_embedding(chapter, size=500)
        env.vector.hits = [{
            "chunkId": f"{chunk['id']}#2", "docId": chunk["docId"],
            "seq": 0, "text": subs[2], "score": 0.9,
        }]
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])
        out = retrieval.answer_question(env.app_state, kb, "第150话讲了什么?")
        c = out["chunks"][0]
        assert "childText" not in c and "parentChars" not in c
        assert c["parentContext"] is True and c["childSeq"] == 2

    def test_answer_sources_carry_child_seq(self, env, monkeypatch):
        """answer_question 的 sources.chunks 带 childSeq + 回带文本。"""
        chapter = _chapter(300)
        kb, chunk = _kb_with_chapter(env, "来源", chapter)
        subs = sub_chunks_for_embedding(chapter, size=500)
        env.vector.hits = [{
            "chunkId": f"{chunk['id']}#2", "docId": chunk["docId"],
            "seq": 0, "text": subs[2], "score": 0.9,
        }]
        env.llm.query_result = {"entities": [], "keywords": [], "hop": 1}
        monkeypatch.setattr(env.store, "search_chunks_fulltext", lambda *a, **k: [])
        out = retrieval.answer_question(env.app_state, kb, "第150话讲了什么?")
        sc = out["sources"]["chunks"][0]
        assert sc["childSeq"] == 2
        assert len(sc["text"]) > len(subs[2])   # 展开看到的是回带窗口
        assert "第150话。" in sc["text"]
