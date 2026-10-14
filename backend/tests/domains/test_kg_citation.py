"""KG 引用编号测试 —— answer.j2 句尾引用规则 + cited_chunks 解析回传。"""
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from domains.knowledge_graph import retrieval

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
    # 总表行：「可用引用编号」显式汇总(仅靠循环行的 [片段N] 满足不了
    # 上面断言——总表行被删时旧断言抓不住,这里单独锚定)
    assert "可用引用编号" in out


# ── cited_chunks 解析回传 ─────────────────────────────────────

class TestParseCitedChunks:

    def test_parse_basic(self):
        """[片段:N]/[片段N]/[片段：N] 三形态 + 去重升序;与前端 ChatPanel 同款正则。"""
        assert retrieval.parse_cited_chunks(
            "事实A[片段:1]。事实B[片段：2]和[片段:1]") == [1, 2]
        assert retrieval.parse_cited_chunks("事实C[片段3] 细节[片段: 4]") == [3, 4]

    def test_parse_no_citation(self):
        assert retrieval.parse_cited_chunks("无引用回答") == []

    def test_parse_ignores_non_citation_brackets(self):
        """非片段引用的方括号(如 [实体:X]/[链接文字])不产生编号。"""
        assert retrieval.parse_cited_chunks("结论[实体:甲],参见[备注]与[片段:x]") == []

    def test_parse_out_of_range_passthrough(self):
        """越界编号原样透传(不 clamp/不滤除)——T2 裁决:过滤责任在前端
        消费端(双界 n>=1 && n<=len),后端解析到什么算什么,契约锚定。
        返回去重升序:99 与 0 均在,顺序按升序。"""
        assert retrieval.parse_cited_chunks("幻觉[片段:99]与[片段:0]") == [0, 99]


class TestAnswerQuestionCitedChunks:

    def test_answer_question_parses_cited_chunks(self):
        """answer_question 返回体新增 cited_chunks:从 answer 文本解析
        [片段:N] 编号(去重升序);sources.chunks 顺序不变(编号权威源)。"""
        class CiteLLM:
            def chat(self, messages, temperature=None, max_tokens=None,
                     conv_id=None, stage=None):
                return "甲任职于A部门[片段:1]。细节见[片段:2]与[片段:1]。"

        app_state = SimpleNamespace(llm_client=CiteLLM(), settings_store=None,
                                    conversation_store=None)
        # 预构造 retrieved:跳过图谱/向量检索,集成测试纯内存
        retrieved = {
            "subgraph": {"nodes": [{"id": "kb1:甲", "name": "甲"}], "edges": []},
            "chunks": [
                {"docId": "d1", "docName": "handbook.md", "seq": 0,
                 "text": "甲担任A部门经理。", "score": 0.9},
                {"docId": "d2", "docName": "org-chart.md", "seq": 1,
                 "text": "部门职责细节。", "score": 0.8},
            ],
            "intent": {"entities": ["甲"], "keywords": [], "hop": 1},
        }
        kb = {"id": "kb1", "name": "一号库"}
        out = retrieval.answer_question(app_state, kb, "甲在哪?", retrieved=retrieved)
        assert out["cited_chunks"] == [1, 2]
        # 编号权威源未受影响:片段N ↔ sources.chunks[N-1] 映射保持
        # (两个 chunk 用异名 docName——同名时顺序交换断言不生效)
        assert [c["docName"] for c in out["sources"]["chunks"]] == \
            ["handbook.md", "org-chart.md"]

    def test_answer_question_no_citation_empty_list(self):
        """LLM 未标引用 → cited_chunks 为空列表(键恒存在,前端免判空)。"""
        class PlainLLM:
            def chat(self, messages, temperature=None, max_tokens=None,
                     conv_id=None, stage=None):
                return "知识库中未找到相关内容。"

        app_state = SimpleNamespace(llm_client=PlainLLM(), settings_store=None,
                                    conversation_store=None)
        retrieved = {"subgraph": {"nodes": [], "edges": []}, "chunks": [], "intent": None}
        out = retrieval.answer_question(app_state, {"id": "kb1", "name": "空库"},
                                        "q", retrieved=retrieved)
        assert out["cited_chunks"] == []


# ── kb_search 工具 artifact 透传(前端消费的前提) ─────────────

class TestKbSearchArtifactCitedChunks:
    """answer_question 的 cited_chunks 必须经 kb_search 工具 artifact 到达
    前端——artifact 是字段白名单式组装,新键不透传整链就断(T3 前置)。"""

    def test_artifact_carries_cited_chunks(self, monkeypatch):
        class CiteLLM:
            def chat(self, messages, temperature=None, max_tokens=None,
                     conv_id=None, stage=None):
                return "甲任职于A部门[片段:1]。"

        monkeypatch.setattr(retrieval, "answer_question",
                            lambda *a, **k: {
                                "answer": "甲任职于A部门[片段:1]。",
                                "cited_chunks": [1],
                                "kb": {"id": "kb1", "name": "一号库"},
                                "intent": None,
                                "subgraph": {"nodes": [], "edges": []},
                                "chunks": [],
                                "sources": {"entities": [], "chunks": []},
                            })
        from domains.knowledge_graph.tools import kb_search as kb_search_mod
        from domains.knowledge_graph import runtime as kg_runtime

        tool = kb_search_mod.KbSearchTool(app_state=SimpleNamespace())
        # 选库路径打断真实存储依赖:唯一库自动选中
        # (kb_search 内函数级导入,kb_runtime 同一模块对象,patch 生效)
        monkeypatch.setattr(kg_runtime, "get_kg_store",
                            lambda state: SimpleNamespace(
                                list_kbs=lambda: [{"id": "kb1", "name": "一号库"}]))

        class Ctx:
            conv_id = "t"
            session_state = None

            def emit(self, *a, **k):
                pass

            def trace(self, *a, **k):
                pass

        result = tool.execute({"user_input": "甲在哪?"}, Ctx())
        assert result.artifact["cited_chunks"] == [1]
        assert result.artifact["type"] == "kg_search_result"
