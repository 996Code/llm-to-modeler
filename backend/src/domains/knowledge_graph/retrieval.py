"""混合检索编排 —— 图谱子图 + 向量召回 → LLM 综合回答(GraphRAG)。

【检索链路】
  query →(query.j2)→ 检索意图{entities, keywords, hop}
        → 图谱路:find_entities 精确/包含匹配 → BFS 邻域子图(限量)
        → 全文兜底(P2#12):图谱零命中时 kg_chunks PG 全文检索
          (tsvector+GIN,2-gram 分词)补充召回原文片段
        → 向量路:query embedding → 该库 collection top-k chunk(可选)
        → 三元组线性化 + 实体详情 + 片段原文
        →(answer.j2)→ 带来源引用的回答

【降级语义】
  - LLM 意图解析失败 → 抛异常(不静默降级,用户裁决)
  - 向量未启用/失败 → 只走图谱路(+零命中时的全文兜底)
  - 图谱无命中 → 全文兜底补充召回(开关 fulltext_fallback_enabled,
    默认开;兜底也失败/无命中 → 让 LLM 明确回答"未找到")

kb_search 对话工具与 POST /search REST 共用本模块;跨库(多库)检索走
answer_question_multi(意图解析一次/逐库检索/合并策略见其 docstring)。

【检索可观测】图谱/向量两路的每次调用入 call_logs(call_type='graph'/'vector'),
与 llm/upstream 同表同视图:请求(参数/种子词/top_k)、响应(召回量/匹配度
top 分数)、耗时全量留痕——检索质量与召回水位在调用日志里直接可查。
"""
import logging
import re
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


# 与前端 ChatPanel.vue 同款正则:兼容 [片段:N]/[片段N]/[片段：N]
_CITED_CHUNK_RE = re.compile(r"\[片段[:：]?\s*(\d+)\]")


def parse_cited_chunks(answer: str) -> List[int]:
    """从回答文本解析被引用的片段编号([片段:N] → N 去重升序)。

    编号权威源是 sources.chunks 顺序(片段N ↔ sources.chunks[N-1]),
    这里只做文本解析回传,前端据此区分"已引用/已召回未引用"。
    """
    return sorted({int(m) for m in _CITED_CHUNK_RE.findall(answer or "")})


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
        from sdk.call_context import current_pack_name
        cs.save_call_log(
            call_type=call_type,
            endpoint=endpoint,
            request_data=request_data,
            response_data=response_data,
            status_code=None if not error else 500,
            duration_ms=duration_ms,
            error_message=error,
            conv_id=conv_id,
            pack_name=current_pack_name(),
        )
    except Exception as e:  # 观测写失败不拖垮检索
        logger.warning(f"检索调用日志写入失败({endpoint}): {e}")


# ── 检索意图解析 ─────────────────────────────────────────────

def parse_query_intent(app_state, query: str, relation_types: List[Dict],
                       conv_id: Optional[str] = None) -> Dict[str, Any]:
    """query → {entities, keywords, relation_types, hop}。

    LLM 失败直接抛异常(不降级):降级出的"整句关键词找种子"在真图上
    基本零命中,给用户的是假可用——宁可让 /search 502/对话工具报错
    使用户感知,也不静默劣化(导入任务链路不经过此处,不受影响)。
    """
    llm = app_state.llm_client
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


# ── 两路检索 ─────────────────────────────────────────────────

def hybrid_retrieve(app_state, kb: Dict[str, Any], query: str,
                    conv_id: Optional[str] = None,
                    top_k: Optional[int] = None,
                    on_stage=None,
                    intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """混合检索:返回 {intent, seeds, subgraph, chunks}。

    图谱路始终执行;向量路仅 kb.vector_enabled 时执行(失败降级为空)。
    on_stage(stage_key, message): 各阶段进度回调(可选)。
    intent: 预解析的检索意图(可选;跨库检索 answer_question_multi 传入
    ——所有库共享同一 query 意图,不逐库重复烧 LLM)。传入时跳过意图
    解析与对应 stage 事件(由调用方统一发)。
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
    if intent is None:
        _stage("kb_search.intent", "解析检索意图(实体/关键词/跳数)…")
        intent = parse_query_intent(
            app_state, query, schema.get("relation_types") or [], conv_id=conv_id)

    # 图谱路:实体/关键词找种子 → BFS 子图(两步各记一条 call_log:
    # 种子匹配的命中数/精确率,子图的召回量与截断水位)
    terms = intent["entities"] or intent["keywords"]
    # P0#2 检索测试页:召回明细/水位出返回体(此前只进 call_logs,管理端
    # 测试页与日志共用同一数据源;用户级 /search 不透传该键,防枚举口径不变)
    diagnostics: Dict[str, Any] = {"termDetail": [], "nodesTruncated": None,
                                   "edgesTruncated": None, "maxNodes": None,
                                   "maxEdges": None, "vectorDegraded": False}
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
            diagnostics["termDetail"] = list(_term_detail)
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
            diagnostics.update(
                nodesTruncated=len(_nodes) >= _max_nodes,
                edgesTruncated=len(_edges) >= _max_edges,
                maxNodes=_max_nodes, maxEdges=_max_edges)
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

    # 全文兜底(P2#12 第三路):图谱零命中(意图实体/关键词在图上找不到
    # 种子——别名/错别字/部分匹配场景)时,kg_chunks PG 全文检索补充召回。
    # 旧路在此场景整体哑火(纯包含匹配对"孔明诸葛亮"这类查询永不命中),
    # 第三路按 2-gram 全文召回原文片段,让回答仍有资料可依。
    chunks: List[Dict[str, Any]] = []
    fulltext_hits: List[Dict[str, Any]] = []
    if not seeds and _cfg(app_state, "fulltext_fallback_enabled", True):
        _stage("kb_search.fulltext", "图谱零命中,全文检索文档片段兜底…")
        _t0 = time.monotonic()
        _err = None
        try:
            _doc_names = {d["id"]: d["filename"]
                          for d in store.list_documents(kb["id"])}
            fulltext_hits = store.search_chunks_fulltext(kb["id"], query, limit=10)
            for h in fulltext_hits:
                h["docName"] = _doc_names.get(h.get("docId") or "", "")
            chunks.extend(fulltext_hits)
        except Exception as e:
            _err = str(e)
            # 兜底失败不拖垮检索:降级为"图谱零命中"原语义(LLM 答未找到)
            logger.warning(f"全文兜底检索失败,降级跳过: {e}")
        finally:
            _log_retrieval_call(
                app_state, "fulltext", "pg:search_chunks_fulltext",
                request_data={"stage": "kg.fulltext_fallback", "kbId": kb["id"],
                              "kb": kb.get("name"), "query": query[:200],
                              "limit": 10},
                response_data={
                    "hits": len(fulltext_hits),
                    "results": [
                        {"doc": h.get("docName") or h.get("docId") or "?",
                         "seq": h.get("seq"),
                         "score": round(float(h.get("score") or 0.0), 4),
                         "text": (h.get("text") or "").strip()[:120]}
                        for h in fulltext_hits
                    ],
                },
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=_err, conv_id=conv_id)
    diagnostics["fulltextHits"] = len(fulltext_hits)

    # 向量路:query embedding → top-k chunk(embedding 本身已由 llm/client
    # 记为 call_type='llm';这里记向量检索的召回量与匹配度)
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
                [query], conv_id=conv_id, stage="kg.query_embed",
                mode="query")[0]
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
                    app_state, "vector", f"{_vector_backend()}:search",
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
            # chunk 文本进上下文(docName 已在日志前映射好)。
            # 父块去重:向量子块 id 形如 {chunk_id}#i,同一父块(一章)的
            # 多个段可能同时命中 top_k——按父块保留得分最高的一段,
            # 否则答案资料里同一章反复出现,挤占召回名额。
            # 全文兜底已入列的父块同样跳过(第三路与向量路同父不重复)
            seen_parent = {str(h.get("chunkId") or "").split("#", 1)[0]
                           for h in chunks}
            for h in hits:
                parent = str(h.get("chunkId") or "").split("#", 1)[0]
                if parent in seen_parent:
                    continue
                seen_parent.add(parent)
                chunks.append(h)
        except Exception as e:
            # 降级事件本身也入观测:连接建不起来/元数据读失败时,
            # 调用日志里留一条 degraded 记录(否则"为什么只走了图谱路"不可查)。
            # search 本身的失败内层 finally 已记,这里跳过防同一次失败落两条
            if _search_failed:
                logger.warning(f"向量检索失败,降级纯图谱: {e}")
                diagnostics["vectorDegraded"] = True
                # 全文兜底命中不随降级丢弃(第三路独立于向量路)
                return {"intent": intent, "seeds": seeds, "subgraph": subgraph,
                        "chunks": chunks, "diagnostics": diagnostics}
            _log_retrieval_call(
                app_state, "vector", f"{_vector_backend()}:search",
                request_data={"stage": "kg.vector_search", "kbId": kb["id"],
                              "kb": kb.get("name"), "degraded": True},
                response_data={"hits": 0, "degraded": True},
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=str(e), conv_id=conv_id)
            logger.warning(f"向量检索失败,降级纯图谱: {e}")
            diagnostics["vectorDegraded"] = True

    return {"intent": intent, "seeds": seeds, "subgraph": subgraph,
            "chunks": chunks, "diagnostics": diagnostics}


def linearize_context(retrieved: Dict[str, Any]) -> Dict[str, List[str]]:
    """检索结果 → answer.j2 需要的三段上下文(三元组/实体详情/片段)。"""
    sub = retrieved.get("subgraph") or {}
    nodes = {n["id"]: n for n in sub.get("nodes") or []}
    id_short = lambda nid: nodes.get(nid, {}).get("name", str(nid).rsplit(":", 1)[-1])  # noqa: E731

    def _defang(s) -> str:
        # 图谱字段(描述/别名/证据)源自不可信文档的 LLM 抽取,与 chunk 同款
        # defang,防内容里的 ``` 拆 answer 模板的围栏
        return str(s).replace("```", "~~~")

    triples: List[str] = []
    seen_triples = set()   # 同一 (source,type,target) 跨多块被抽到会存多条
    for e in (sub.get("edges") or []):
        key = (id_short(e["source"]), e.get("type"), id_short(e["target"]))
        if key in seen_triples:
            continue        # 块级留痕在图里,答案资料里重复列出只会稀释注意力
        seen_triples.add(key)
        desc = f"({_defang(e.get('description'))})" if e.get("description") else ""
        ev = f" 证据:「{_defang(e['evidence'])}」" if e.get("evidence") else ""
        triples.append(
            f"{_defang(id_short(e['source']))} -[{_defang(e.get('type'))}]{desc}-> "
            f"{_defang(id_short(e['target']))}{ev}")

    node_details: List[str] = []
    for n in (sub.get("nodes") or [])[:40]:
        parts = [f"{_defang(n['name'])}({n.get('type') or '未知类型'})"]
        if n.get("description"):
            parts.append(_defang(n["description"]))
        if n.get("aliases"):
            parts.append(f"别名: {'、'.join(_defang(a) for a in n['aliases'][:5])}")
        node_details.append(":".join(parts))

    chunk_texts: List[str] = []
    for i, c in enumerate(retrieved.get("chunks") or []):
        src = c.get("docName") or "文档"
        # 跨库检索的 chunk 带 kbName:资料前缀追加库名(〔库名/文档名〕),
        # LLM 回答能标注来源库;单库 chunk 无该键,前缀保持〔文档名〕不变
        if c.get("kbName"):
            src = f"{c['kbName']}/{src}"
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
    {answer, cited_chunks, subgraph, chunks, intent, sources}

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
    # P0#4 rerank:linearize 前对 chunks 重排(排序对象=资料段非子图)。
    # 开关在 KG settings(默认关);失败降级原序不阻断(排序质量项,
    # 与向量路降级同语义)。重排发生在 sources.chunks 构造前——
    # 排序后顺序即编号权威源,[片段:N] 自动对齐。
    if retrieved.get("chunks") and _cfg(app_state, "rerank_enabled", False):
        _stage("kb_search.rerank", f"重排 {len(retrieved['chunks'])} 个片段…")
        try:
            order = app_state.llm_client.rerank(
                query, [(c.get("text") or "")[:600] for c in retrieved["chunks"]],
                conv_id=conv_id, stage="kg.rerank")
            retrieved["chunks"] = [retrieved["chunks"][i] for i in order]
        except Exception as e:
            logger.warning(f"rerank 失败,降级原召回序: {e}")
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
        # 被引用片段编号(从 answer 解析 [片段:N];编号权威源=sources.chunks 顺序)
        "cited_chunks": parse_cited_chunks(answer),
        "kb": {"id": kb["id"], "name": kb.get("name") or ""},
        "intent": retrieved.get("intent"),
        "subgraph": sub,
        "chunks": retrieved.get("chunks") or [],
        "sources": sources,
    }


# ── 跨库(多库)综合回答 ─────────────────────────────────────

def answer_question_multi(app_state, kbs: List[Dict[str, Any]], query: str,
                          conv_id: Optional[str] = None,
                          top_k: Optional[int] = None,
                          on_stage=None) -> Dict[str, Any]:
    """跨库检索 + LLM 综合回答(N3:知识库检索支持跨库)。

    合并策略:
      - 意图解析**一次**:所有库共享同一 query 意图(硬约束——多库不重复
        烧 LLM;各库 schema 的 relation_types 合并去重后一起给意图解析)
      - 逐库 hybrid_retrieve(图谱+向量各库独立,共享意图)
      - chunks 跨库按相似度排序取全局 top_k(每库先各取 top_k,合并后再
        截全局 top_k),逐条带 kbName 库来源标注
      - subgraph 各库 concat(节点 id 天然带 kb 前缀,跨库不冲突)
      - LLM 综合回答**一次**:资料前缀〔库名/文档名〕(linearize_context
        对带 kbName 的 chunk 追加库名)
      - cited_chunks 编号权威源=合并后 chunks 顺序(P0#3 契约不变)

    单库列表委托 answer_question(与单库路径行为完全一致),仅补 kbs 键。
    Returns: {answer, cited_chunks, kb, kbs, intent, subgraph, chunks, sources}
      kb 为展示兼容字段(前端卡片标题用):{id: "", name: 库名拼接}。
    """
    def _stage(key: str, message: str) -> None:
        if on_stage is not None:
            try:
                on_stage(key, message)
            except Exception:
                pass  # 进度回调失败不拖垮检索

    if not kbs:
        raise ValueError("跨库检索至少需要一个知识库")
    # 去重(同名/同 ID 混传防御),保持传入顺序
    _seen = set()
    kbs = [kb for kb in kbs if not (kb["id"] in _seen or _seen.add(kb["id"]))]
    if len(kbs) == 1:
        result = answer_question(app_state, kbs[0], query, conv_id=conv_id,
                                top_k=top_k, on_stage=on_stage)
        result["kbs"] = [{"id": kbs[0]["id"], "name": kbs[0].get("name") or ""}]
        return result

    # ── 意图解析一次(所有库共享) ──
    _stage("kb_search.intent", "解析检索意图(实体/关键词/跳数)…")
    _rel_types, _seen_rt = [], set()
    for kb in kbs:
        for rt in (kb.get("schema") or {}).get("relation_types") or []:
            _name = rt.get("name") if isinstance(rt, dict) else str(rt)
            if _name and _name not in _seen_rt:
                _seen_rt.add(_name)
                _rel_types.append(rt)
    intent = parse_query_intent(app_state, query, _rel_types, conv_id=conv_id)

    # ── 逐库检索(共享意图,图谱/向量各库独立) ──
    per_kb: List[Dict[str, Any]] = []
    for i, kb in enumerate(kbs):
        _stage("kb_search.graph",
               f"({i + 1}/{len(kbs)}) 库「{kb.get('name')}」图谱实体匹配…")
        per_kb.append(hybrid_retrieve(
            app_state, kb, query, conv_id=conv_id, top_k=top_k,
            on_stage=_stage, intent=intent))

    # ── 合并 ──
    # chunks:跨库按相似度排序取全局 top_k;逐条标注库来源(kbName 进
    # linearize 的资料前缀与 sources,回答/前端都能看到出处库)
    merged_chunks: List[Dict[str, Any]] = []
    for kb, r in zip(kbs, per_kb):
        for c in r.get("chunks") or []:
            c["kbName"] = kb.get("name") or ""
            merged_chunks.append(c)
    k = top_k or int(_cfg(app_state, "vector_top_k", 5))
    merged_chunks.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
    merged_chunks = merged_chunks[:k]

    # subgraph:各库 concat(节点 id 带 kb 前缀不冲突;线性化的 id→name
    # 映射按 id 查,跨库同名实体各归各的库,不会串)
    subgraph = {
        "nodes": [n for r in per_kb for n in (r.get("subgraph") or {}).get("nodes") or []],
        "edges": [e for r in per_kb for e in (r.get("subgraph") or {}).get("edges") or []],
    }
    # diagnostics:逐库合并(按库名分组;answer 路径不外透,与单库口径一致)
    diagnostics = {"perKb": {kb.get("name"): r.get("diagnostics")
                             for kb, r in zip(kbs, per_kb)}}
    retrieved = {
        "intent": intent,
        "seeds": [s for r in per_kb for s in r.get("seeds") or []],
        "subgraph": subgraph,
        "chunks": merged_chunks,
        "diagnostics": diagnostics,
    }

    # P0#4 rerank:与单库同款——合并后、linearize 前重排;排序后顺序即
    # 编号权威源。开关默认关;失败降级原序不阻断
    if merged_chunks and _cfg(app_state, "rerank_enabled", False):
        _stage("kb_search.rerank", f"重排 {len(merged_chunks)} 个片段…")
        try:
            order = app_state.llm_client.rerank(
                query, [(c.get("text") or "")[:600] for c in merged_chunks],
                conv_id=conv_id, stage="kg.rerank")
            merged_chunks = [merged_chunks[i] for i in order]
            retrieved["chunks"] = merged_chunks
        except Exception as e:
            logger.warning(f"rerank 失败,降级原召回序: {e}")

    _stage("kb_search.assemble", "整理三元组与文档片段…")
    ctx = linearize_context(retrieved)

    _stage("kb_search.answer", f"综合回答(依据 {len(ctx['triples'])} 条三元组 / "
                               f"{len(ctx['chunk_texts'])} 个文档片段)…")
    prompt = _prompt_loader().render(
        "knowledge_graph", "answer",
        kb_name="、".join(kb.get("name") or "" for kb in kbs), query=query,
        triples=ctx["triples"], node_details=ctx["node_details"],
        chunk_texts=ctx["chunk_texts"],
    )
    temperature = int(_cfg(app_state, "answer_temperature", 30)) / 100.0
    answer = app_state.llm_client.chat(
        [{"role": "user", "content": prompt}],
        temperature=temperature, conv_id=conv_id, stage="kg.answer",
    ).strip()

    # 来源汇总:实体跨库合并去重(展示名);chunks 带库来源标注
    entities: List[str] = []
    _seen_names = set()
    for r in per_kb:
        for n in (r.get("subgraph") or {}).get("nodes") or []:
            if n["name"] not in _seen_names:
                _seen_names.add(n["name"])
                entities.append(n["name"])
    sources = {
        "entities": entities[:20],
        "chunks": [
            {
                "docId": c.get("docId"),
                "docName": c.get("docName"),
                "kbName": c.get("kbName"),
                "score": c.get("score"),
                "seq": c.get("seq"),
                "text": (c.get("text") or "").strip()[:600],
            }
            for c in merged_chunks
        ],
    }
    return {
        "answer": answer,
        "cited_chunks": parse_cited_chunks(answer),
        # 展示兼容(前端卡片标题):跨库时 id 置空、name 拼接
        "kb": {"id": "", "name": "、".join(kb.get("name") or "" for kb in kbs)},
        "kbs": [{"id": kb["id"], "name": kb.get("name") or ""} for kb in kbs],
        "intent": intent,
        "subgraph": subgraph,
        "chunks": merged_chunks,
        "sources": sources,
    }

def _vector_backend() -> str:
    """观测 endpoint 前缀(向量检索统一 pg:search)。"""
    try:
        from sdk.vector_store import resolve_vector_backend
        return resolve_vector_backend()
    except Exception:
        return "vector"
