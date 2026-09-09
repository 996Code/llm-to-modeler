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

【检索可观测】图谱/向量两路的每次调用入 call_logs(call_type='graph'/'vector'),
与 llm/upstream 同表同视图:请求(参数/种子词/top_k)、响应(召回量/匹配度
top 分数)、耗时全量留痕——检索质量与召回水位在调用日志里直接可查。
"""
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from domains.knowledge_graph import runtime

logger = logging.getLogger(__name__)

# 模块级 PromptLoader(prompts 目录在本 pack 下;线程安全:Jinja2 env 只读)
_loader = None


def _prompt_loader():
    global _loader
    if _loader is None:
        from sdk.prompt_loader import PromptLoader
        _loader = PromptLoader(packs_root=Path(__file__).resolve().parent.parent)
    return _loader


def _cfg(app_state, key, default):
    return runtime.settings_reader(app_state).get(key, default)


def _log_retrieval_call(app_state, call_type: str, endpoint: str,
                        request_data: Dict, response_data: Dict,
                        duration_ms: int, error: Optional[str] = None,
                        conv_id: Optional[str] = None) -> None:
    """图谱/向量检索调用入 call_logs(失败不影响检索主流程)。

    call_type 用 'graph'/'vector' 与 llm/upstream 并列;stage 并入
    request_data(与 llm/client.py 的约定一致,管理端据此区分环节)。
    conv_id 解析与 llm/upstream 同规则:显式参数优先,thread-local 兜底
    (graph 工作线程绑定了会话上下文,调用方漏传也能关联,链路不断)。
    """
    try:
        cs = getattr(app_state, "conversation_store", None)
        if cs is None:
            return
        if not conv_id:
            from sdk.call_context import current_conversation_id
            conv_id = current_conversation_id()
        cs.save_call_log(
            call_type=call_type,
            endpoint=endpoint,
            request_data=request_data,
            response_data=response_data,
            status_code=None if not error else 500,
            duration_ms=duration_ms,
            error_message=error,
            conv_id=conv_id,
        )
    except Exception as e:  # 观测写失败不拖垮检索
        logger.warning(f"检索调用日志写入失败({endpoint}): {e}")


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
                    top_k: Optional[int] = None,
                    on_stage=None) -> Dict[str, Any]:
    """混合检索:返回 {intent, seeds, subgraph, chunks}。

    图谱路始终执行;向量路仅 kb.vector_enabled 时执行(失败降级为空)。
    on_stage(stage_key, message): 各阶段进度回调(可选)。
    """
    def _stage(key: str, message: str) -> None:
        if on_stage is not None:
            try:
                on_stage(key, message)
            except Exception:
                pass

    store = runtime.get_kg_store(app_state)
    graph = runtime.get_graph(app_state)
    schema = kb.get("schema") or {}
    _stage("kb_search.intent", "解析检索意图(实体/关键词/跳数)…")
    intent = parse_query_intent(
        app_state, query, schema.get("relation_types") or [], conv_id=conv_id)

    # 图谱路:实体/关键词找种子 → BFS 子图(两步各记一条 call_log:
    # 种子匹配的命中数/精确率,子图的召回量与截断水位)
    terms = intent["entities"] or intent["keywords"]
    seeds: List[Dict[str, Any]] = []
    subgraph = {"nodes": [], "edges": []}
    if terms:
        _stage("kb_search.graph", f"图谱实体匹配({len(terms)} 个种子词)…")
        _t0 = time.monotonic()
        _err = None
        try:
            seeds = graph.find_entities(kb["id"], terms, limit=10)
        except Exception as e:
            _err = str(e)
            raise
        finally:
            # 逐词匹配明细:哪个词命中了哪个实体(含归一化名/类型)——
            # "为什么没召回某实体"在调用日志里直接可答。
            # 归一化必须与 find_entities 同源(sdk.normalize_name 含全角→半角),
            # 用朴素 .lower() 会在全角 query 上记出 matched=null 的假阴性
            from sdk.graph_store import normalize_name as _norm
            _norm_hits = {_norm(str(s.get("normalized") or "")): s for s in seeds}
            _term_detail = []
            for t in terms:
                _nt = _norm(t)
                _hit = _norm_hits.get(_nt)
                if _hit is None:  # 包含匹配兜底:找 normalized 包含该词的种子
                    _hit = next((s for s in seeds
                                 if _nt in _norm(str(s.get("normalized") or ""))), None)
                _term_detail.append({
                    "term": t,
                    "matched": _hit.get("name") if _hit else None,
                    "type": _hit.get("type") if _hit else None,
                })
            _log_retrieval_call(
                app_state, "graph", "neo4j:find_entities",
                request_data={"stage": "kg.find_entities", "kbId": kb["id"],
                              "kb": kb.get("name"), "terms": terms, "limit": 10,
                              "intent": {"entities": intent["entities"],
                                         "keywords": intent["keywords"]}},
                response_data={"hits": len(seeds), "termDetail": _term_detail},
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=_err, conv_id=conv_id)
    if seeds:
        _stage("kb_search.subgraph", f"扩展关联子图({len(seeds)} 个种子,BFS 邻域)…")
        _t0 = time.monotonic()
        _err = None
        _hops = int(intent["hop"])
        _max_nodes = int(_cfg(app_state, "graph_max_nodes", 80))
        _max_edges = int(_cfg(app_state, "graph_max_edges", 150))
        try:
            subgraph = graph.subgraph_around(
                kb["id"], [s["normalized"] for s in seeds if s.get("normalized")],
                hops=_hops, max_nodes=_max_nodes, max_edges=_max_edges,
            )
        except Exception as e:
            _err = str(e)
            raise
        finally:
            _nodes = subgraph.get("nodes") or []
            _edges = subgraph.get("edges") or []
            _name_of = {n.get("id"): n.get("name") for n in _nodes}
            _log_retrieval_call(
                app_state, "graph", "neo4j:subgraph_around",
                request_data={"stage": "kg.subgraph", "kbId": kb["id"],
                              "kb": kb.get("name"), "seeds": [s.get("normalized") for s in seeds],
                              "hops": _hops, "maxNodes": _max_nodes, "maxEdges": _max_edges},
                response_data={
                    "nodes": len(_nodes),
                    "edges": len(_edges),
                    # 截断水位:贴着上限说明子图被截,召回不完整(排查"为什么没召回某实体"的第一现场)
                    "nodesTruncated": len(_nodes) >= _max_nodes,
                    "edgesTruncated": len(_edges) >= _max_edges,
                    # 召回明细:实体名单(名+类型)与三元组线性概要(截断到可读量)
                    "nodeNames": [f"{n.get('name')}({n.get('type') or '?'})" for n in _nodes[:40]],
                    "triples": [
                        f"{_name_of.get(e.get('source'), '?')} -[{e.get('type')}]-> "
                        f"{_name_of.get(e.get('target'), '?')}"
                        for e in _edges[:40]
                    ],
                },
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=_err, conv_id=conv_id)

    # 向量路:query embedding → top-k chunk(embedding 本身已由 llm/client
    # 记为 call_type='llm';这里记向量检索的召回量与匹配度)
    chunks: List[Dict[str, Any]] = []
    if kb.get("vectorEnabled"):
        _stage("kb_search.vector", "向量检索相似文档片段…")
        _t0 = time.monotonic()
        _err = None
        _search_failed = False
        hits: List[Dict[str, Any]] = []
        try:
            k = top_k or int(_cfg(app_state, "vector_top_k", 5))
            vector = runtime.get_vector(app_state)
            qvec = app_state.llm_client.embeddings(
                [query], conv_id=conv_id, stage="kg.query_embed")[0]
            # doc 名映射提前(日志里直接给文档名而不是裸 ID)
            _doc_names = {d["id"]: d["filename"] for d in store.list_documents(kb["id"])}
            try:
                hits = vector.search(kb["id"], qvec, top_k=k)
                for h in hits:
                    h["docName"] = _doc_names.get(h.get("docId") or "", "")
            except Exception as e:
                _err = str(e)
                _search_failed = True  # 内层已落日志,外层降级记录跳过(防双条)
                raise
            finally:
                _scores = [float(h.get("score") or 0.0) for h in hits]
                _log_retrieval_call(
                    app_state, "vector", "milvus:search",
                    request_data={"stage": "kg.vector_search", "kbId": kb["id"],
                                  "kb": kb.get("name"), "topK": k,
                                  "query": query[:200], "metric": "COSINE"},
                    response_data={
                        "hits": len(hits),
                        # COSINE 相似度:越高越相似。top/mean 给"这轮召回质量"一眼结论,
                        # 逐条明细(文档/块序号/分数/文本摘要)直接可查
                        "topScore": round(max(_scores), 4) if _scores else None,
                        "meanScore": round(sum(_scores) / len(_scores), 4) if _scores else None,
                        "results": [
                            {"doc": h.get("docName") or h.get("docId") or "?",
                             "seq": h.get("seq"), "score": round(float(h.get("score") or 0.0), 4),
                             "text": (h.get("text") or "").strip()[:120]}
                            for h in hits
                        ],
                    },
                    duration_ms=int((time.monotonic() - _t0) * 1000),
                    error=_err, conv_id=conv_id)
            # chunk 文本进上下文(docName 已在日志前映射好)
            chunks.extend(hits)
        except Exception as e:
            # 降级事件本身也入观测:连接建不起来/元数据读失败时,
            # 调用日志里留一条 degraded 记录(否则"为什么只走了图谱路"不可查)。
            # search 本身的失败内层 finally 已记,这里跳过防同一次失败落两条
            if _search_failed:
                logger.warning(f"向量检索失败,降级纯图谱: {e}")
                return {"intent": intent, "seeds": seeds, "subgraph": subgraph, "chunks": []}
            _log_retrieval_call(
                app_state, "vector", "milvus:search",
                request_data={"stage": "kg.vector_search", "kbId": kb["id"],
                              "kb": kb.get("name"), "degraded": True},
                response_data={"hits": 0, "degraded": True},
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=str(e), conv_id=conv_id)
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
                    top_k: Optional[int] = None,
                    on_stage=None) -> Dict[str, Any]:
    """混合检索 + LLM 综合回答。Returns:
    {answer, subgraph, chunks, intent, sources}

    on_stage(stage_key, message): 检索各阶段的进度回调(kb_search 透传给
    ctx.emit → 前端 pipeline 进度条)。None 时无副作用(REST /search 直调)。
    """
    def _stage(key: str, message: str) -> None:
        if on_stage is not None:
            try:
                on_stage(key, message)
            except Exception:
                pass  # 进度回调失败不拖垮检索

    if retrieved is None:
        retrieved = hybrid_retrieve(app_state, kb, query, conv_id=conv_id,
                                    top_k=top_k, on_stage=_stage)
    _stage("kb_search.assemble", "整理三元组与文档片段…")
    ctx = linearize_context(retrieved)

    _stage("kb_search.answer", f"综合回答(依据 {len(ctx['triples'])} 条三元组 / "
                               f"{len(ctx['chunk_texts'])} 个文档片段)…")
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

    # 来源汇总(前端引用展示;chunk 带 text 摘要供点击查看)
    sub = retrieved.get("subgraph") or {}
    sources = {
        "entities": [n["name"] for n in sub.get("nodes") or []][:20],
        "chunks": [
            {
                "docId": c.get("docId"),
                "docName": c.get("docName"),
                "score": c.get("score"),
                "seq": c.get("seq"),
                # 片段原文摘要(前端点击展开看内容;600 字与进 prompt 的截断一致)
                "text": (c.get("text") or "").strip()[:600],
            }
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
