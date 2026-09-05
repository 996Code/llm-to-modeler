"""混合检索编排 —— 图谱子图 + 向量召回 → LLM 综合回答(GraphRAG)。

【检索链路】
  query →(query.j2)→ 检索意图{entities, keywords, hop}
        → 图谱路:find_entities 精确/包含匹配 → BFS 邻域子图(限量)
        → 向量路:query embedding → 该库 collection top-k chunk(可选)
        → 三元组线性化 + 实体详情 + 片段原文
        →(answer.j2)→ 带来源引用的回答

【降级语义】
  - LLM 意图解析失败 → 直接用整个 query 做关键词找种子(不烧重试)
  - 向量未启用/失败 → 只走图谱路
  - 图谱无命中且无向量命中 → 让 LLM 明确回答"未找到"(prompt 已约束)

kb_search 对话工具与 POST /search REST 共用本模块。
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from domains.knowledge_graph import runtime

logger = logging.getLogger(__name__)

# 模块级 PromptLoader(prompts 目录在本 pack 下;线程安全:Jinja2 env 只读)
_loader = None


def _prompt_loader():
    global _loader
    if _loader is None:
        from engine.prompt_loader import PromptLoader
        _loader = PromptLoader(packs_root=Path(__file__).resolve().parent.parent)
    return _loader


def _cfg(app_state, key, default):
    return runtime.settings_reader(app_state).get(key, default)


# ── 检索意图解析 ─────────────────────────────────────────────

def parse_query_intent(app_state, query: str, relation_types: List[Dict],
                       conv_id: Optional[str] = None) -> Dict[str, Any]:
    """query → {entities, keywords, relation_types, hop};失败降级为整句关键词。"""
    llm = app_state.llm_client
    try:
        prompt = _prompt_loader().render(
            "knowledge_graph", "query",
            query=query, relation_types=relation_types or [],
        )
        data = llm.chat_json([{"role": "user", "content": prompt}],
                             temperature=0.0, conv_id=conv_id, stage="kg.query")
        entities = [str(e).strip() for e in (data.get("entities") or []) if str(e).strip()][:10]
        keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:8]
        hop = data.get("hop")
        hop = int(hop) if isinstance(hop, (int, str)) and str(hop).isdigit() else 1
        return {"entities": entities, "keywords": keywords, "hop": max(1, min(hop, 3))}
    except Exception as e:
        logger.warning(f"检索意图解析失败,降级整句关键词: {e}")
        return {"entities": [], "keywords": [query[:40]], "hop": 1}


# ── 两路检索 ─────────────────────────────────────────────────

def hybrid_retrieve(app_state, kb: Dict[str, Any], query: str,
                    conv_id: Optional[str] = None,
                    top_k: Optional[int] = None) -> Dict[str, Any]:
    """混合检索:返回 {intent, seeds, subgraph, chunks}。

    图谱路始终执行;向量路仅 kb.vector_enabled 时执行(失败降级为空)。
    """
    store = runtime.get_kg_store(app_state)
    graph = runtime.get_graph(app_state)
    schema = kb.get("schema") or {}
    intent = parse_query_intent(
        app_state, query, schema.get("relation_types") or [], conv_id=conv_id)

    # 图谱路:实体/关键词找种子 → BFS 子图
    terms = intent["entities"] or intent["keywords"]
    seeds = graph.find_entities(kb["id"], terms, limit=10) if terms else []
    subgraph = {"nodes": [], "edges": []}
    if seeds:
        subgraph = graph.subgraph_around(
            kb["id"], [s["normalized"] for s in seeds if s.get("normalized")],
            hops=intent["hop"],
            max_nodes=int(_cfg(app_state, "graph_max_nodes", 80)),
            max_edges=int(_cfg(app_state, "graph_max_edges", 150)),
        )

    # 向量路:query embedding → top-k chunk
    chunks: List[Dict[str, Any]] = []
    if kb.get("vectorEnabled"):
        try:
            k = top_k or int(_cfg(app_state, "vector_top_k", 5))
            vector = runtime.get_vector(app_state)
            qvec = app_state.llm_client.embeddings(
                [query], conv_id=conv_id, stage="kg.query_embed")[0]
            hits = vector.search(kb["id"], qvec, top_k=k)
            # chunk 文本进上下文,并映射回文档名(来源引用用)
            doc_names = {d["id"]: d["filename"] for d in store.list_documents(kb["id"])}
            for h in hits:
                h["docName"] = doc_names.get(h.get("docId") or "", "")
                chunks.append(h)
        except Exception as e:
            logger.warning(f"向量检索失败,降级纯图谱: {e}")

    return {"intent": intent, "seeds": seeds, "subgraph": subgraph, "chunks": chunks}


def linearize_context(retrieved: Dict[str, Any]) -> Dict[str, List[str]]:
    """检索结果 → answer.j2 需要的三段上下文(三元组/实体详情/片段)。"""
    sub = retrieved.get("subgraph") or {}
    nodes = {n["id"]: n for n in sub.get("nodes") or []}
    id_short = lambda nid: nodes.get(nid, {}).get("name", str(nid).rsplit(":", 1)[-1])  # noqa: E731

    triples: List[str] = []
    for e in (sub.get("edges") or []):
        desc = f"({e.get('description')})" if e.get("description") else ""
        ev = f" 证据:「{e['evidence']}」" if e.get("evidence") else ""
        triples.append(f"{id_short(e['source'])} -[{e.get('type')}]{desc}-> {id_short(e['target'])}{ev}")

    node_details: List[str] = []
    for n in (sub.get("nodes") or [])[:40]:
        parts = [f"{n['name']}({n.get('type') or '未知类型'})"]
        if n.get("description"):
            parts.append(n["description"])
        if n.get("aliases"):
            parts.append(f"别名: {'、'.join(n['aliases'][:5])}")
        node_details.append(":".join(parts))

    chunk_texts: List[str] = []
    for i, c in enumerate(retrieved.get("chunks") or []):
        src = c.get("docName") or "文档"
        # 文档原文是不可信输入:反引号 defang,防片段内容干扰 answer 模板
        text = (c.get("text") or "").strip().replace("\n", " ").replace("```", "~~~")
        chunk_texts.append(f"〔{src}〕{text[:600]}")

    return {"triples": triples, "node_details": node_details, "chunk_texts": chunk_texts}


# ── 综合回答 ─────────────────────────────────────────────────

def answer_question(app_state, kb: Dict[str, Any], query: str,
                    conv_id: Optional[str] = None,
                    retrieved: Optional[Dict[str, Any]] = None,
                    top_k: Optional[int] = None) -> Dict[str, Any]:
    """混合检索 + LLM 综合回答。Returns:
    {answer, subgraph, chunks, intent, sources}
    """
    if retrieved is None:
        retrieved = hybrid_retrieve(app_state, kb, query, conv_id=conv_id, top_k=top_k)
    ctx = linearize_context(retrieved)

    prompt = _prompt_loader().render(
        "knowledge_graph", "answer",
        kb_name=kb.get("name") or "", query=query,
        triples=ctx["triples"], node_details=ctx["node_details"],
        chunk_texts=ctx["chunk_texts"],
    )
    temperature = int(_cfg(app_state, "answer_temperature", 30)) / 100.0
    answer = app_state.llm_client.chat(
        [{"role": "user", "content": prompt}],
        temperature=temperature, conv_id=conv_id, stage="kg.answer",
    ).strip()

    # 来源汇总(前端引用展示)
    sub = retrieved.get("subgraph") or {}
    sources = {
        "entities": [n["name"] for n in sub.get("nodes") or []][:20],
        "chunks": [
            {"docName": c.get("docName"), "score": c.get("score"), "seq": c.get("seq")}
            for c in retrieved.get("chunks") or []
        ],
    }
    return {
        "answer": answer,
        "kb": {"id": kb["id"], "name": kb.get("name") or ""},
        "intent": retrieved.get("intent"),
        "subgraph": sub,
        "chunks": retrieved.get("chunks") or [],
        "sources": sources,
    }
