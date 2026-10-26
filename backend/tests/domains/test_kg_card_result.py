"""KG card-only contract, without shared database fixtures."""
import json
from types import SimpleNamespace

import pytest

from domains.knowledge_graph import annotations, retrieval, runtime
from domains.knowledge_graph.tools.kb_search import KbSearchTool
from engine.nodes import handle_result_node
from engine.stream import _save_result_conversation
from sdk.tool import AskSpec, ToolContext, ToolResult
from services.conversation_store import ConversationStore


KB = {"id": "kb1", "name": "Knowledge"}
ANSWER = "## Complete answer\n\n" + "Evidence and explanation. " * 60 + "[片段:1]"


@pytest.fixture
def tool_env(monkeypatch):
    state = SimpleNamespace(settings_store=None, conversation_store=None)
    store = SimpleNamespace(list_kbs=lambda: [KB], get_kb_by_name=lambda name: None)
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: store)
    monkeypatch.setattr(annotations, "match_annotation", lambda *args: None)
    ctx = ToolContext(llm_client=None, asset_client=None, conversation=None,
                      emit=lambda *args: None, conv_id="card-test")
    return KbSearchTool(state), state, ctx


def _result(answer=ANSWER):
    return {"answer": answer, "kb": KB,
            "subgraph": {"nodes": [{"id": "n1", "name": "Entity"}], "edges": []},
            "sources": {"entities": ["Entity"], "chunks": [
                {"kbId": "kb1", "docId": "d1", "docName": "source.pdf",
                 "text": "Original evidence", "seq": 0, "score": 0.9}]},
            "cited_chunks": [1]}


def _payload(result):
    serialized = json.loads(result.model_dump_json())
    return handle_result_node({"tool_result": serialized})["sse_events"][0]["data"]


@pytest.mark.parametrize("cross_kb", [False, True])
def test_complete_answer_survives_tool_and_platform_serialization(tool_env, monkeypatch, cross_kb):
    tool, state, ctx = tool_env
    expected = _result()
    if cross_kb:
        expected["kbs"] = [KB, {"id": "kb2", "name": "Other"}]
        monkeypatch.setattr(runtime, "get_kg_store", lambda state: SimpleNamespace(
            list_kbs=lambda: expected["kbs"]))
    monkeypatch.setattr(retrieval, "answer_question_multi" if cross_kb else "answer_question",
                        lambda *args, **kwargs: expected)
    result = tool.execute({"user_input": "Question"}, ctx)
    payload = _payload(result)
    assert result.reply is None
    assert payload["artifactType"] == "data"
    assert payload["summary"] == ANSWER
    assert payload["data"]["pack"] == "knowledge_graph"
    assert payload["data"]["resultDisplay"] == "card"
    assert payload["data"]["answer"] == ANSWER
    for key in ("kb", "subgraph", "sources", "cited_chunks"):
        assert payload["data"][key] == expected[key]
    if cross_kb:
        assert payload["data"]["kbs"] == expected["kbs"]


def test_no_evidence_answer_uses_existing_plain_text_model_contract(tool_env, monkeypatch):
    tool, state, ctx = tool_env
    answer = "No supporting evidence was found; this cannot be determined."
    state.llm_client = SimpleNamespace(chat=lambda *args, **kwargs: answer)
    real_answer = retrieval.answer_question
    monkeypatch.setattr(retrieval, "answer_question", lambda state, kb, query, **kwargs:
                        real_answer(state, kb, query, retrieved={
                            "subgraph": {"nodes": [], "edges": []}, "chunks": [], "intent": None}))
    result = tool.execute({"user_input": "Unknown question"}, ctx)
    assert result.artifact["answer"] == answer
    assert result.artifact["resultDisplay"] == "card"
    assert result.artifact["cited_chunks"] == []
    assert result.artifact["sources"] == {"entities": [], "chunks": []}


def test_annotation_answer_is_data_not_reply_and_not_truncated(tool_env, monkeypatch):
    tool, state, ctx = tool_env
    monkeypatch.setattr(annotations, "match_annotation", lambda *args: {
        "question": "Question", "answer": ANSWER, "score": 1.0})
    result = tool.execute({"user_input": "Question"}, ctx)
    payload = _payload(result)
    assert result.reply is None
    assert payload["artifactType"] == "data"
    assert payload["data"]["annotationHit"] is True
    assert payload["data"]["answer"] == ANSWER
    assert payload["data"]["pack"] == "knowledge_graph"
    assert payload["data"]["resultDisplay"] == "card"


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_missing_answer_does_not_enable_card_only(tool_env, monkeypatch, answer):
    tool, state, ctx = tool_env
    monkeypatch.setattr(retrieval, "answer_question", lambda *args, **kwargs: _result(answer))
    result = tool.execute({"user_input": "Question"}, ctx)
    assert result.artifact.get("resultDisplay") != "card"


@pytest.mark.parametrize("answer", [None, "", "   "])
def test_empty_annotation_answer_does_not_short_circuit(tool_env, monkeypatch, answer):
    tool, state, ctx = tool_env
    monkeypatch.setattr(annotations, "match_annotation", lambda *args: {
        "question": "Question", "answer": answer, "score": 1.0})
    monkeypatch.setattr(retrieval, "answer_question", lambda *args, **kwargs: _result())
    result = tool.execute({"user_input": "Question"}, ctx)
    assert result.artifact["answer"] == ANSWER
    assert not result.artifact.get("annotationHit")


def test_retrieval_failure_stays_on_error_path(tool_env, monkeypatch):
    tool, state, ctx = tool_env
    def fail(*args, **kwargs):
        raise RuntimeError("retrieval unavailable")
    monkeypatch.setattr(retrieval, "answer_question", fail)
    result = tool.execute({"user_input": "Question"}, ctx)
    assert result.artifact is None
    assert _payload(result)["error"] is True


def test_unknown_kb_and_no_kb_keep_existing_fallbacks(tool_env, monkeypatch):
    tool, state, ctx = tool_env
    assert tool.execute({"user_input": "Question", "kb": "missing"}, ctx).error_for_llm
    monkeypatch.setattr(runtime, "get_kg_store", lambda state: SimpleNamespace(list_kbs=lambda: []))
    result = tool.execute({"user_input": "Question"}, ctx)
    assert result.reply
    assert result.artifact is None


def test_ask_serialization_does_not_create_a_result_card():
    result = ToolResult(ask=AskSpec(questions=[]), summary="Needs clarification")
    restored = ToolResult.model_validate_json(result.model_dump_json())
    assert restored.ask is not None
    assert restored.artifact is None
    # execute_tool consumes AskSpec before handle_result; no terminal artifact event.
    assert handle_result_node({"tool_result": None}) == {"sse_events": []}


@pytest.mark.parametrize("legacy", [False, True])
def test_history_envelope_preserves_answer_and_legacy_shape(tool_env, monkeypatch, legacy):
    tool, state, ctx = tool_env
    monkeypatch.setattr(retrieval, "answer_question", lambda *args, **kwargs: _result())
    result = tool.execute({"user_input": "Question"}, ctx)
    if legacy:
        for key in ("pack", "resultDisplay", "answer"):
            result.artifact.pop(key, None)
    payload = _payload(result)
    messages = []
    # Real persistence adapter and row mapper, without writing to shared PG.
    _save_result_conversation(SimpleNamespace(add_message=lambda **kwargs: messages.append(kwargs)),
                              "conv", "user", "Question", payload, None)
    assert len(messages) == 1
    saved = messages[0]
    row = {"id": "m1", "kind": "assistant", "created_at": "now", "payload": json.dumps({
        "role": saved["role"], "content": saved["content"], "data_artifact": saved["data_artifact"]})}
    history = ConversationStore._row_to_message(row)
    assert history["content"] == ANSWER
    assert history["dataArtifact"] == payload["data"]
    if legacy:
        assert "pack" not in history["dataArtifact"]
        assert "resultDisplay" not in history["dataArtifact"]
    else:
        assert history["dataArtifact"]["answer"] == ANSWER
        assert history["dataArtifact"]["resultDisplay"] == "card"
