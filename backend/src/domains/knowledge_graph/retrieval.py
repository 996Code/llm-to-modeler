"""混合检索编排 —— 图谱子图 + 向量召回 → LLM 综合回答(GraphRAG)。

【检索链路】
  query →(query.j2)→ 检索意图{entities, keywords, hop}
        → 图谱路:find_entities 精确/包含匹配 → BFS 邻域子图(限量)
        → 全文兜底(P2#12):图谱零命中时 kg_chunks PG 全文检索
          (tsvector+GIN,2-gram 分词)补充召回原文片段
        → 向量路:query embedding → 该库 collection top-k chunk(可选)
        → 父子回带(Dify 对标):命中子段回带父块上下文窗口(句读对齐)
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
from sdk.observability import (CALL_TYPE_GRAPH as CT_GRAPH,
                             CALL_TYPE_VECTOR as CT_VECTOR,
                             CALL_TYPE_FULLTEXT as CT_FULLTEXT)

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


def _reorder_lost_in_middle(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """D15 首尾拉链重排(lost-in-middle 缓解):对**当前位置序**做两端交替。

    LLM 对长上下文的开头/结尾注意力强、中段弱。不重排序——顺序本身
    是相关度判断的结果(rerank 开时=rerank 序,关时=召回序),重排会
    把 rerank 的判序丢弃(审查修复:初版按陈旧 score 重排,rerank 白烧)。
    拉链铺位:当前位置第 1 名放 0,第 2 名放**末位**,第 3 名放 1,
    第 4 名放倒数第 2…两端向中间收拢——最高分占头、次高分占尾、
    中段留给末位块。

    纯顺序变换:chunks 顺序即引用编号权威源,重排后 [片段:N] 自动
    对齐新序,无数据增删。块数 ≤3 时无中段可言,原序返回(零成本)。
    """
    if len(chunks) <= 3:
        return chunks
    out: List[Dict[str, Any]] = []
    tail: List[Dict[str, Any]] = []
    for i, c in enumerate(chunks):
        (out if i % 2 == 0 else tail).append(c)
    out.extend(reversed(tail))
    return out


# ── 父子回带(Dify 对标) ─────────────────────────────────────
# 向量命中子段 → 回带父块(整章)上下文窗口进 prompt:子段小而聚焦
# (命中准),LLM 看到的是命中点前后的连贯上下文而非孤立碎段。
# 我们的父块是章节级(上限万级字符,远大于 Dify 父段 ~1024 字),整章
# 全文进 prompt 会撞上下文窗口——所以取"以命中子段为中心的窗口"
# (前后各 _PARENT_WINDOW_CHARS 字,句读对齐),窗口总量已超过 Dify
# 父段全文,连贯性不吃亏。
_PARENT_WINDOW_CHARS = 2000      # 命中子段前后各带的父块上下文
_PARENT_CONTEXT_MAX_CHARS = 5000  # 单块进 prompt 上限(窗口+子段+句读余量)
_CHUNK_TOTAL_BUDGET_CHARS = 24000  # 全部片段进 prompt 的总字数预算
_SNAP_SLACK_CHARS = 200           # 句读收口搜索半径
_SENT_BOUNDARY_RE = re.compile(r"[。！？；!?;\n]")


def _snap_start(text: str, start: int, slack: int = _SNAP_SLACK_CHARS) -> int:
    """窗口头向前收口到最近句读边界(找不到就硬切)。"""
    lo = max(0, start - slack)
    last = None
    for m in _SENT_BOUNDARY_RE.finditer(text, lo, start):
        last = m.end()
    return last if last is not None else start


def _snap_end(text: str, end: int, slack: int = _SNAP_SLACK_CHARS) -> int:
    """窗口尾向后收口到最近句读边界(找不到就硬切)。"""
    hi = min(len(text), end + slack)
    m = _SENT_BOUNDARY_RE.search(text, end, hi)
    return m.end() if m else end


def _compact(text: str):
    """压掉空白字符,返回 (紧凑文本, 紧凑索引→原索引映射)。

    子块文本经 _hard_split 重组:句间插入父块没有的 \n 连接符、split
    又吞掉标点后的空白——子块在父块里直接 find 永不命中。两侧压空白
    后匹配,窗口边界再映射回原文切片(进 prompt 的是父块原文,不是
    子块的重组文本)。
    """
    chars: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def parent_context_window(parent_text: str, child_text: str,
                          before: int = _PARENT_WINDOW_CHARS,
                          after: int = _PARENT_WINDOW_CHARS,
                          cap: int = _PARENT_CONTEXT_MAX_CHARS) -> Optional[str]:
    """以 child_text 在 parent_text 中的位置为中心取上下文窗口(句读对齐)。

    返回 None = 定位失败(child 不在 parent 中,如父块文本已变更),
    调用方降级用子段自身文本。窗口边界向句读边界收口——截在句中的
    窗口本身就不连贯,违背回带初衷。超 cap 时从更宽的一侧硬裁(裁剪
    边不再收口:护栏场景宁可硬切也不放大窗口);宽侧吸收不完 excess
    (子段自身接近 cap)时仍超,保子段头部硬切。
    定位取 find 首次命中:子段文本在父块中重复出现(模板句式)时窗口
    中心可能落在前一处——无偏移存储的已知取舍,内容相同不影响正确性。
    """
    if not parent_text or not child_text:
        return None
    compact_parent, idx_map = _compact(parent_text)
    compact_child = "".join(ch for ch in child_text if not ch.isspace())
    if not compact_child:
        return None
    pos_c = compact_parent.find(compact_child)
    if pos_c < 0:
        return None
    pos = idx_map[pos_c]
    c_end = idx_map[pos_c + len(compact_child) - 1] + 1
    start = _snap_start(parent_text, max(0, pos - before))
    end = _snap_end(parent_text, min(len(parent_text), c_end + after))
    if end - start > cap:
        excess = end - start - cap
        left, right = pos - start, end - c_end
        if left >= right:
            start = min(pos, start + excess)
        else:
            end = max(c_end, end - excess)
        if end - start > cap:  # 宽侧吸收不完 excess:保子段头部硬切
            end = start + cap
    return parent_text[start:end]


def _apply_parent_context(store, hits: List[Dict[str, Any]]) -> int:
    """向量命中子块原地回带父块上下文(改写 text,标注 childSeq)。

    子块 id 形如 {chunk_id}#i(父指针内嵌,无需 Dify 的 ChildChunk 中间
    表):拆父 id 批量回查 kg_chunks,以命中子段为中心取窗口替换 text。
    降级语义(不静默丢内容,也不拖垮检索):
      - 无 #i 的块(全文兜底整章块/旧数据)本就是父块,不回带;
      - 父块缺失(重导入后向量残留的孤儿子块)→ 保留子段原文;
      - 父块回查异常 → 整批保留子段原文(告警日志);
      - 定位失败(父块文本变更)→ 保留子段原文。
    返回成功回带的条数(诊断口径)。
    """
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for h in hits:
        cid = str(h.get("chunkId") or "")
        if "#" not in cid:
            continue
        pid, _, sub = cid.partition("#")
        h["childSeq"] = int(sub) if sub.isdigit() else None
        by_parent.setdefault(pid, []).append(h)
    if not by_parent:
        return 0
    try:
        rows = {r["id"]: r for r in store.get_chunks_by_ids(list(by_parent))}
    except Exception as e:
        logger.warning(f"父块回查失败,回带降级子段原文: {e}")
        return 0
    n = 0
    for pid, group in by_parent.items():
        row = rows.get(pid)
        if not row:
            continue  # 孤儿子块:父块已删(重导入先删后插),保留子段原文
        ptext = row.get("text") or ""
        for h in group:
            h["childText"] = str(h.get("text") or "")  # 原子段留档(rerank/前端高亮用)
            window = parent_context_window(ptext, h["childText"])
            if window is None:
                continue  # 定位失败:保留子段原文
            h["text"] = window
            h["parentContext"] = True
            h["parentChars"] = len(ptext)
            n += 1
    return n


def _log_retrieval_call(app_state, call_type: str, endpoint: str,
                        request_data: Dict, response_data: Dict,
                        duration_ms: int, error: Optional[str] = None,
                        conv_id: Optional[str] = None) -> None:
    """图谱/向量检索调用入 call_logs——委托 SDK log_pack_call(P0-2)。"""
    from sdk.observability import log_pack_call
    log_pack_call(app_state, call_type, endpoint, request_data,
                  response_data, duration_ms, error=error, conv_id=conv_id)


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
                    intent: Optional[Dict[str, Any]] = None,
                    doc_filter: Optional[set] = None,
                    auto_doc_scope_enabled: bool = True) -> Dict[str, Any]:
    """混合检索:返回 {intent, seeds, subgraph, chunks}。

    图谱路仅无文档范围时执行;向量路仅 kb.vector_enabled 时执行(失败降级为空)。
    on_stage(stage_key, message): 各阶段进度回调(可选)。
    intent: 预解析的检索意图(可选;跨库检索 answer_question_multi 传入
    ——所有库共享同一 query 意图,不逐库重复烧 LLM)。传入时跳过意图
    解析与对应 stage 事件(由调用方统一发)。
    doc_filter: 已解析的文档 ID 白名单,向量路与全文路均按此过滤。
    None 不过滤;空集零召回。实体属性跨文档合并,无法证明范围时
    省略图谱路(含种子与诊断),不把库级内容带入受限文档范围。
    """
    def _stage(key: str, message: str) -> None:
        if on_stage is not None:
            try:
                on_stage(key, message)
            except Exception:
                pass

    store = runtime.get_kg_store(app_state)
    graph = runtime.get_graph(app_state) if doc_filter is None else None
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
                                   "maxEdges": None, "vectorDegraded": False,
                                   "parentContextHits": 0}
    seeds: List[Dict[str, Any]] = []
    subgraph = {"nodes": [], "edges": []}
    if doc_filter is not None:
        diagnostics["graphOmitted"] = "document_scope"
        _stage("kb_search.graph", "文档范围已限定,省略无法证明文档归属的图谱召回")
    if terms and doc_filter is None:
        _stage("kb_search.graph",
               f"库「{kb.get('name')}」图谱实体匹配({len(terms)} 个种子词)…")
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
    # 文档实体收敛(通用,零业务词):意图命中 document 类型实体且为
    # "点名"级强匹配时,该实体的来源文档就是用户所指范围——同模板
    # 批量库(兄弟文档条款文本几乎相同)的邻居片段不再混入。未命中
    # document 实体(用户没点名)不收敛,行为与原先完全一致。
    # 强匹配=归一化后 term 与实体名 相等/前缀/后缀(编号或完整标题);
    # 仅中段子串(如泛指型号词)不算点名——find_entities 的 CONTAINS
    # 兜底会让短词命中长文档名,弱信号触发收敛会误挡同类多文档素材。
    _DOC_SCOPE_MAX_DOCS = 3  # 来源文档超过此数=泛指而非点名,不收敛
    _NAMED_TERM_MIN_CHARS = 3  # 归一化后短于此=体裁词(大纲/规程/总纲),不算点名
    auto_doc_scope: Optional[set] = None
    if doc_filter is None and auto_doc_scope_enabled and seeds:
        from sdk.graph_store import normalize_name as _norm

        def _identifier_like(t: str) -> bool:
            # 编号特征(含数字)是跨命名体系的通用标识符信号——
            # GB/T、ISO、JJF、企标编号都含数字;不算业务硬编码
            return any(c.isdigit() for c in t)

        def _named(entity_name: str) -> bool:
            name = _norm(str(entity_name or ""))
            if not name:
                return False
            for t in {_norm(str(x)) for x in terms}:
                if len(t) < _NAMED_TERM_MIN_CHARS:
                    continue  # 体裁短词(大纲/总纲)不算点名
                if name == t:
                    return True
                # 前缀/后缀=引用了标题的头部(编号)或尾部(完整标题),
                # 但须防主题词误点名:通用词("压力表")恰是文档名
                # ("压力表检定规程")前缀时,要求 term 覆盖标题多数,
                # 或 term 带编号特征(引用的是标识符而非主题词)
                if (name.startswith(t) or name.endswith(t)) and (
                        len(t) * 2 >= len(name) or _identifier_like(t)):
                    return True
            return False

        scope_docs = set()
        for s in seeds:
            if str(s.get("type") or "") == "document" and _named(s.get("name")):
                scope_docs.update(s.get("sourceDocs") or [])
        if 0 < len(scope_docs) <= _DOC_SCOPE_MAX_DOCS:
            auto_doc_scope = scope_docs
            diagnostics["docScopeFromEntity"] = sorted(auto_doc_scope)
            _stage("kb_search.vector",
                   f"命中文档实体,检索收敛到 {len(auto_doc_scope)} 份指定文档…")
    effective_filter = doc_filter if doc_filter is not None else auto_doc_scope
    # 设计取舍(留痕):自动收敛只过滤片段路(向量/全文),不过滤 BFS 子图
    # ——实体关系归属无法按文档证明(实体属性跨文档合并),过滤会连
    # 定位信息一起丢;kb_search 结论只消费 chunks,子图仅作定位/展示。

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
            if effective_filter is not None:
                fulltext_hits = [h for h in fulltext_hits
                                 if h.get("docId") in effective_filter]
            for h in fulltext_hits:
                h["docName"] = _doc_names.get(h.get("docId") or "", "")
            chunks.extend(fulltext_hits)
        except Exception as e:
            _err = str(e)
            # 兜底失败不拖垮检索:降级为"图谱零命中"原语义(LLM 答未找到)
            logger.warning(f"全文兜底检索失败,降级跳过: {e}")
        finally:
            _log_retrieval_call(
                app_state, CT_FULLTEXT, "pg:search_chunks_fulltext",
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
                # 范围收敛时放大检索窗再过滤(而非全库 top_k 后过滤):
                # 同模板批量库的兄弟文档条款分数与本尊近乎相同,全库取
                # top_k 时兄弟占席,目标文档自己的高分章节可能排在
                # LIMIT 之外被系统性截断——先过滤后截断才拿得足块数
                search_k = (k * max(1, len(effective_filter) * 2)
                            if effective_filter is not None else k)
                hits = vector.search(kb["id"], qvec, top_k=search_k)
                if effective_filter is not None:
                    # D7 元数据白名单/文档实体收敛:命中按文档过滤(空集=零召回)
                    hits = [h for h in hits if h.get("docId") in effective_filter][:k]
                for h in hits:
                    h["docName"] = _doc_names.get(h.get("docId") or "", "")
            except Exception as e:
                _err = str(e)
                _search_failed = True  # 内层已落日志,外层降级记录跳过(防双条)
                raise
            finally:
                _scores = [float(h.get("score") or 0.0) for h in hits]
                _log_retrieval_call(
                    app_state, CT_VECTOR, f"{_vector_backend()}:search",
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
            # 全文兜底已入列的同父块:向量命中**替换**而非跳过——向量
            # 命中带语义分数+父子回带,整章兜底块只有裸 ts_rank 分;
            # 原地替换保住位置(引用编号稳定)。图谱零命中(别名/错字
            # 查询)时两路常同时命中同父,跳过会让回带静默失效。
            parent_idx = {str(h.get("chunkId") or "").split("#", 1)[0]: i
                          for i, h in enumerate(chunks)}
            kept: List[Dict[str, Any]] = []
            for h in hits:
                parent = str(h.get("chunkId") or "").split("#", 1)[0]
                idx = parent_idx.get(parent)
                if idx is not None:
                    if "#" in str(chunks[idx].get("chunkId") or ""):
                        continue   # 已是向量命中(同父多子段):留最高分
                    chunks[idx] = h  # 替换全文兜底块(向量命中质量更高)
                else:
                    chunks.append(h)
                    parent_idx[parent] = len(chunks) - 1
                kept.append(h)
            # 父子回带:命中子段回带父块上下文窗口(检索测试页与真实
            # 问答共用本函数,一处接线两路生效)。失败降级子段原文
            # (_apply_parent_context 内消化,不拖垮检索)
            diagnostics["parentContextHits"] = _apply_parent_context(store, kept)
        except Exception as e:
            # search 本身的失败内层 finally 已记日志,这里跳过防同一次
            # 失败落两条;前置失败(embeddings/元数据)在此补一条 degraded
            # 记录(否则"为什么只走了图谱路"不可查)。
            # 不提前 return:收敛场景仍需走下方 relax 兜底(检索失败恰是
            # 最该放开的时刻);无收敛时 relax 条件不满足,落到函数尾
            # 返回,语义与原提前 return 一致(全文兜底块仍保留)。
            if not _search_failed:
                _log_retrieval_call(
                    app_state, CT_VECTOR, f"{_vector_backend()}:search",
                    request_data={"stage": "kg.vector_search", "kbId": kb["id"],
                                  "kb": kb.get("name"), "degraded": True},
                    response_data={"hits": 0, "degraded": True},
                    duration_ms=int((time.monotonic() - _t0) * 1000),
                    error=str(e), conv_id=conv_id)
            logger.warning(f"向量检索失败,降级纯图谱: {e}")
            diagnostics["vectorDegraded"] = True

    # 文档实体收敛后零召回:实体指向的文档已删/未解析,或语义查询与
    # 该文档全部片段都不相似——放开范围全库重查一轮,不能空手而归
    # (标注短路/答案生成在 kb_search 层,这里保证 chunks 有料)。
    if auto_doc_scope is not None and not chunks and kb.get("vectorEnabled"):
        _stage("kb_search.vector", "指定文档内零召回,放开范围全库检索…")
        diagnostics["docScopeRelaxed"] = True
        _t0 = time.monotonic()
        _err = None
        k = top_k or int(_cfg(app_state, "vector_top_k", 5))  # 先绑定:finally 引用它
        try:
            vector = runtime.get_vector(app_state)
            qvec = app_state.llm_client.embeddings(
                [query], conv_id=conv_id, stage="kg.query_embed", mode="query")[0]
            hits = sorted(vector.search(kb["id"], qvec, top_k=k),
                          key=lambda h: float(h.get("score") or 0.0), reverse=True)
            # 同父块去重(与主路同款):同一章多个子段只留最高分,
            # 不让同章挤占全部名额
            _seen_parents = set()
            hits = [h for h in hits
                    if (p := str(h.get("chunkId") or "").split("#", 1)[0])
                    not in _seen_parents and not _seen_parents.add(p)]
            _doc_names = {d["id"]: d["filename"]
                          for d in store.list_documents(kb["id"])}
            for h in hits:
                h["docName"] = _doc_names.get(h.get("docId") or "", "")
            chunks = hits
            diagnostics["parentContextHits"] = _apply_parent_context(store, hits)
        except Exception as e:
            _err = str(e)
            logger.warning("文档收敛回退全库检索失败", exc_info=True)
        finally:
            _log_retrieval_call(
                app_state, CT_VECTOR, f"{_vector_backend()}:search",
                request_data={"stage": "kg.vector_search", "kbId": kb["id"],
                              "kb": kb.get("name"), "topK": k,
                              "query": query[:200], "docScopeRelaxed": True},
                response_data={"hits": len(chunks),
                               "results": [
                                   {"doc": h.get("docName") or h.get("docId") or "?",
                                    "seq": h.get("seq"),
                                    "score": round(float(h.get("score") or 0.0), 4)}
                                   for h in chunks]},
                duration_ms=int((time.monotonic() - _t0) * 1000),
                error=_err, conv_id=conv_id)

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

    raw_chunks = retrieved.get("chunks") or []
    # 备料:来源前缀 + defang 后文本(预算按实际长度扣,不虚占)
    srcs: List[str] = []
    texts: List[str] = []
    for c in raw_chunks:
        src = c.get("docName") or "文档"
        # 跨库检索的 chunk 带 kbName:资料前缀追加库名(〔库名/文档名〕),
        # LLM 回答能标注来源库;单库 chunk 无该键,前缀保持〔文档名〕不变
        if c.get("kbName"):
            src = f"{c['kbName']}/{src}"
        # 文档原文是不可信输入:反引号 defang,防片段内容干扰 answer 模板
        text = (c.get("text") or "").strip().replace("\n", " ").replace("```", "~~~")
        srcs.append(src)
        texts.append(text)
    # 预算分配与 chunks 顺序解耦:顺序是引用编号权威源([片段N]↔下标)
    # 不能动,但预算优先级独立——回带窗口(精准命中)先吃预算。否则图谱
    # 零命中时全文兜底的整章块先入列先吃光预算,更精准的向量回带窗口
    # 反被挤到 600 保底(顺序倒挂)。同优先级按分数降序;预算耗尽的
    # 块退 600 保底(条目保留不静默丢)。
    caps = [600] * len(raw_chunks)
    remaining = _CHUNK_TOTAL_BUDGET_CHARS
    priority = sorted(
        range(len(raw_chunks)),
        key=lambda i: (not raw_chunks[i].get("parentContext"),
                       -float(raw_chunks[i].get("score") or 0.0)))
    for i in priority:
        if remaining <= 600:
            break
        cap = min(_PARENT_CONTEXT_MAX_CHARS, remaining)
        caps[i] = cap
        remaining -= min(len(texts[i]), cap)
    # 单块上限 5000:回带窗口整段进——600 的旧口径会把窗口腰斩,回带等于白做
    chunk_texts = [f"〔{srcs[i]}〕{texts[i][:caps[i]]}"
                   for i in range(len(raw_chunks))]

    return {"triples": triples, "node_details": node_details, "chunk_texts": chunk_texts}


# ── D17 检索自主循环(证据不足时补检一轮) ─────────────────────


def _judge_evidence(app_state, kb, query: str, retrieved: Dict[str, Any],
                    conv_id: Optional[str]) -> Optional[str]:
    """D17 证据充分性判定:不足时返回改写 query(补检用),足够返回 None。

    轻量 chat_json 判定,fail-open(异常/格式坏都 None=按原行为直接
    回答,补检是增强不是依赖)。零命中不判(摘要块/兜底路已尽力,
    同 query 补检无增益)。
    """
    chunks = retrieved.get("chunks") or []
    if not chunks:
        return None
    preview = "\n".join(
        f"- 〔{c.get('docName') or '?'}〕{(c.get('text') or '')[:150]}"
        for c in chunks[:6])
    prompt = (
        f"问题:{query}\n知识库「{kb.get('name') or ''}」检索到以下片段:\n{preview}\n\n"
        "判断这些片段是否足以回答问题。只输出 JSON:"
        '{"sufficient": true/false, "better_query": "不足时给出的更优检索词'
        '(换角度,同义改写或拆解子问题;sufficient=true 时留空)"}'
    )
    try:
        data = app_state.llm_client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.0, conv_id=conv_id, stage="kg.sufficiency")
        if not isinstance(data, dict) or data.get("sufficient") is not False:
            return None
        bq = str(data.get("better_query") or "").strip()
        return bq[:120] or None
    except Exception as e:
        logger.warning(f"D17 证据判定失败(跳过补检,按原行为回答): {e}")
        return None


def _merge_retrieved(base: Dict[str, Any], extra: Dict[str, Any],
                     limit: int = 12) -> Dict[str, Any]:
    """补检结果融合:chunks 按父块 id 去重(同块保留高分),子图并集。

    limit=合并后块数上限(与检索预算同量级——补检也要克制,防上下文
    膨胀;D16 整文档被否的同一担忧)。
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for c in (base.get("chunks") or []) + (extra.get("chunks") or []):
        pid = str(c.get("chunkId") or "").split("#", 1)[0]
        if not pid:
            continue
        if pid not in seen or float(c.get("score") or 0) > float(seen[pid].get("score") or 0):
            seen[pid] = c
    merged = sorted(seen.values(),
                    key=lambda c: -float(c.get("score") or 0))[:limit]
    bsub, esub = base.get("subgraph") or {}, extra.get("subgraph") or {}
    nodes = {n["id"]: n for n in bsub.get("nodes") or []}
    for n in esub.get("nodes") or []:
        nodes.setdefault(n["id"], n)
    edges = {(e["source"], e["type"], e["target"]): e
             for e in bsub.get("edges") or []}
    for e in esub.get("edges") or []:
        edges.setdefault((e["source"], e["type"], e["target"]), e)
    return {**base, "chunks": merged,
            "subgraph": {"nodes": list(nodes.values()),
                         "edges": list(edges.values())}}


def _merged_relation_types(kbs: List[Dict[str, Any]]) -> List[Any]:
    """多库 relation_types 合并去重(保持传入顺序)——意图解析的共享口径。"""
    rel_types, seen = [], set()
    for kb in kbs:
        for rt in (kb.get("schema") or {}).get("relation_types") or []:
            name = rt.get("name") if isinstance(rt, dict) else str(rt)
            if name and name not in seen:
                seen.add(name)
                rel_types.append(rt)
    return rel_types


def _requery_once(app_state, kbs, query: str, retrieved: Dict[str, Any],
                  conv_id: Optional[str], top_k: Optional[int],
                  _stage, doc_filter) -> Dict[str, Any]:
    """D17 公共接线:单库/多库共用的"判定→补检→融合"一段。

    多库补检走逐库 hybrid_retrieve+轻合并(不递归整个回答函数——
    那会白烧一次回答生成)。失败降级首轮结果。
    """
    from domains.knowledge_graph import runtime as _rt  # noqa: F401(口径引用)
    _stage("kb_search.requery", "核查检索证据是否充分…")
    bq = _judge_evidence(app_state, kbs[0], query, retrieved, conv_id)
    if not bq:
        return retrieved
    _stage("kb_search.requery", f"证据不足,换角度补检:{bq[:30]}…")
    try:
        # 补检意图一次解析、全库共享(与首轮同款硬约束):逐库重解析会
        # 白烧 N 次 kg.query LLM(线上 3 库实测多耗 ~6.5s),且重发
        # kb_search.intent 让前端进度条倒退
        bq_intent = parse_query_intent(
            app_state, bq, _merged_relation_types(kbs), conv_id=conv_id)
        per_extra = [hybrid_retrieve(app_state, kb, bq, conv_id=conv_id,
                                     top_k=top_k, on_stage=_stage,
                                     intent=bq_intent, doc_filter=doc_filter,
                                     auto_doc_scope_enabled=False)
                     for kb in kbs]
        extra = per_extra[0]
        for r in per_extra[1:]:
            extra = _merge_retrieved(extra, r)
        return _merge_retrieved(retrieved, extra)
    except Exception as e:
        logger.warning(f"D17 补检失败,降级首轮结果: {e}")
        return retrieved


# ── 综合回答 ─────────────────────────────────────────────────

def answer_question(app_state, kb: Dict[str, Any], query: str,
                    conv_id: Optional[str] = None,
                    retrieved: Optional[Dict[str, Any]] = None,
                    top_k: Optional[int] = None,
                    on_stage=None,
                    doc_filter: Optional[set] = None) -> Dict[str, Any]:
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
                                    top_k=top_k, on_stage=_stage,
                                    doc_filter=doc_filter)
        # D17 检索自主循环(auto_retrieve_loop 默认开,仅补检一轮):
        # 判定 fail-open,补检失败降级首轮结果
        if _cfg(app_state, "auto_retrieve_loop", True):
            retrieved = _requery_once(app_state, [kb], query, retrieved,
                                      conv_id, top_k, _stage, doc_filter)
    # P0#4 rerank:linearize 前对 chunks 重排(排序对象=资料段非子图)。
    # 开关在 KG settings(默认关);失败降级原序不阻断(排序质量项,
    # 与向量路降级同语义)。重排发生在 sources.chunks 构造前——
    # 排序后顺序即编号权威源,[片段:N] 自动对齐。
    if retrieved.get("chunks") and _cfg(app_state, "rerank_enabled", False):
        _stage("kb_search.rerank", f"重排 {len(retrieved['chunks'])} 个片段…")
        try:
            order = app_state.llm_client.rerank(
                query, [((c.get("childText") or c.get("text") or "")[:600])
                        for c in retrieved["chunks"]],
                conv_id=conv_id, stage="kg.rerank")
            retrieved["chunks"] = [retrieved["chunks"][i] for i in order]
        except Exception as e:
            logger.warning(f"rerank 失败,降级原召回序: {e}")
    # D15 奇偶重排(默认开,纯顺序变换零成本):高分占头尾、低分沉中段,
    # lost-in-middle 缓解。发生在 rerank 之后(rerank 关时对原召回序做),
    # 编号权威源自动对齐新序——与 rerank 出站调用默认关的口径不同
    if retrieved.get("chunks") and _cfg(app_state, "reorder_enabled", True):
        retrieved["chunks"] = _reorder_lost_in_middle(retrieved["chunks"])
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

    # D29 块命中统计:被引用块([片段:N] 解析)hit_count+1——"哪些知识
    # 真被用"的治理数据。编号权威源=chunks 顺序;子块 id 去掉 #i 归到
    # 父块;失败静默(统计项不拖垮回答主链路)
    try:
        _cited = parse_cited_chunks(answer)
        _chunks_now = retrieved.get("chunks") or []
        _ids = {(_chunks_now[n - 1].get("chunkId") or "").split("#", 1)[0]
                for n in _cited if 0 < n <= len(_chunks_now)}
        if _ids:
            runtime.get_kg_store(app_state).bump_chunk_hits(sorted(_ids))
    except Exception as e:
        logger.warning(f"hit_count 统计失败(不阻断): {e}")

    # 来源汇总(前端引用展示;chunk 带 text 摘要供点击查看)
    sub = retrieved.get("subgraph") or {}
    sources = {
        "entities": [n["name"] for n in sub.get("nodes") or []][:20],
        "chunks": [
            {
                # 来源库定位(前端"下载来源文件"用:kbId+docId 定位端点)
                "kbId": kb["id"],
                "docId": c.get("docId"),
                "docName": c.get("docName"),
                "score": c.get("score"),
                "seq": c.get("seq"),
                # 命中子段序号(父子回带标注:chip 显示"块N·子段M")
                "childSeq": c.get("childSeq"),
                # 是否真实回带了父块上下文(降级命中的子段序号照标,
                # 但徽标不亮——"回带"必须是真回带)
                "parentContext": c.get("parentContext"),
                # 片段原文(前端点击展开看内容):回带窗口整段给,与进
                # prompt 的口径一致——展开看到的就是 AI 依据的上下文
                "text": (c.get("text") or "").strip()[:_PARENT_CONTEXT_MAX_CHARS],
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
        # 用户级 chunks 瘦身:childText/parentChars 是检索内部诊断字段
        # (rerank 输入/排障口径),诊断明细归 admin 门 retrieve-test;
        # childSeq/parentContext 是前端展示字段,保留
        "chunks": [{k: v for k, v in c.items()
                    if k not in ("childText", "parentChars")}
                   for c in retrieved.get("chunks") or []],
        "sources": sources,
    }


# ── 跨库(多库)综合回答 ─────────────────────────────────────

def answer_question_multi(app_state, kbs: List[Dict[str, Any]], query: str,
                          conv_id: Optional[str] = None,
                          top_k: Optional[int] = None,
                          on_stage=None,
                          doc_filter: Optional[set] = None) -> Dict[str, Any]:
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
                                top_k=top_k, on_stage=on_stage, doc_filter=doc_filter)
        result["kbs"] = [{"id": kbs[0]["id"], "name": kbs[0].get("name") or ""}]
        return result

    # ── 意图解析一次(所有库共享) ──
    _stage("kb_search.intent", "解析检索意图(实体/关键词/跳数)…")
    intent = parse_query_intent(
        app_state, query, _merged_relation_types(kbs), conv_id=conv_id)

    # ── 逐库检索(共享意图,图谱/向量各库独立;graph 阶段由
    # hybrid_retrieve 自带库名上报,不在此重复发) ──
    per_kb: List[Dict[str, Any]] = []
    for kb in kbs:
        per_kb.append(hybrid_retrieve(
            app_state, kb, query, conv_id=conv_id, top_k=top_k,
            on_stage=_stage, intent=intent, doc_filter=doc_filter))

    # ── 合并 ──
    # chunks:跨库按相似度排序取全局 top_k;逐条标注库来源(kbName 进
    # linearize 的资料前缀与 sources,回答/前端都能看到出处库)
    merged_chunks: List[Dict[str, Any]] = []
    for kb, r in zip(kbs, per_kb):
        for c in r.get("chunks") or []:
            c["kbName"] = kb.get("name") or ""
            c["kbId"] = kb["id"]
            merged_chunks.append(c)
    k = top_k or int(_cfg(app_state, "vector_top_k", 5))
    merged_chunks.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
    # D13 跨库内容去重:同一份资料导入多个库时,同内容(压空白后全文
    # 相等)会各占一个 top_k 名额——答案资料里同一段反复出现,挤掉
    # 别的候选。分数降序下同内容首个即高分者,保留它;被挤掉的库记
    # 进 dedupFrom(可观测:哪些库是同一份资料)。空文本不参与去重。
    _kept: Dict[str, Dict[str, Any]] = {}
    dedup_dropped = 0
    for c in merged_chunks:
        key = "".join(str(c.get("text") or "").split())
        if not key:
            _kept[f"\x00{len(_kept)}"] = c   # 空文本:不去重,占位保序
            continue
        if key in _kept:
            _kept[key].setdefault("dedupFrom", []).append(c.get("kbName") or "")
            dedup_dropped += 1
        else:
            _kept[key] = c
    if dedup_dropped:
        merged_chunks = list(_kept.values())   # dict 保序=首次出现序(分数序)
    merged_chunks = merged_chunks[:k]

    # subgraph:各库 concat(节点 id 带 kb 前缀不冲突;线性化的 id→name
    # 映射按 id 查,跨库同名实体各归各的库,不会串)
    subgraph = {
        "nodes": [n for r in per_kb for n in (r.get("subgraph") or {}).get("nodes") or []],
        "edges": [e for r in per_kb for e in (r.get("subgraph") or {}).get("edges") or []],
    }
    # diagnostics:逐库合并(按库名分组;answer 路径不外透,与单库口径一致)
    # dedupDropped:D13 跨库内容去重挤掉的重复条数(0=无重复)
    diagnostics = {"perKb": {kb.get("name"): r.get("diagnostics")
                             for kb, r in zip(kbs, per_kb)},
                   "dedupDropped": dedup_dropped}
    retrieved = {
        "intent": intent,
        "seeds": [s for r in per_kb for s in r.get("seeds") or []],
        "subgraph": subgraph,
        "chunks": merged_chunks,
        "diagnostics": diagnostics,
    }
    # D17 检索自主循环(与单库同款,判定用合并结果;多库补检走逐库
    # hybrid_retrieve 轻合并,不递归整个回答函数)
    if _cfg(app_state, "auto_retrieve_loop", True):
        retrieved = _requery_once(app_state, kbs, query, retrieved,
                                  conv_id, top_k, _stage, doc_filter)
        # 补检后同步本地变量:下游 rerank/reorder/sources/返回值全部读
        # merged_chunks——不同步会拿首轮旧清单覆盖补检成果(线上实测:
        # 补检捞回含答案的摘要/正文块后全被丢弃,多库查询答"查不到",
        # D17 自上线起补检在多库路径从未生效)。单库路径无此问题。
        merged_chunks = retrieved.get("chunks") or merged_chunks

    # P0#4 rerank:与单库同款——合并后、linearize 前重排;排序后顺序即
    # 编号权威源。开关默认关;失败降级原序不阻断
    if merged_chunks and _cfg(app_state, "rerank_enabled", False):
        _stage("kb_search.rerank", f"重排 {len(merged_chunks)} 个片段…")
        try:
            order = app_state.llm_client.rerank(
                query, [((c.get("childText") or c.get("text") or "")[:600])
                        for c in merged_chunks],
                conv_id=conv_id, stage="kg.rerank")
            merged_chunks = [merged_chunks[i] for i in order]
            retrieved["chunks"] = merged_chunks
        except Exception as e:
            logger.warning(f"rerank 失败,降级原召回序: {e}")
    # D15 奇偶重排(与单库同款,默认开):高分占头尾低分沉中段
    if merged_chunks and _cfg(app_state, "reorder_enabled", True):
        merged_chunks = _reorder_lost_in_middle(merged_chunks)
        retrieved["chunks"] = merged_chunks

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

    # D29 块命中统计:被引用块([片段:N] 解析)hit_count+1——"哪些知识
    # 真被用"的治理数据。编号权威源=chunks 顺序;子块 id 去掉 #i 归到
    # 父块;失败静默(统计项不拖垮回答主链路)
    try:
        _cited = parse_cited_chunks(answer)
        _chunks_now = retrieved.get("chunks") or (merged_chunks or [])
        _ids = {(_chunks_now[n - 1].get("chunkId") or "").split("#", 1)[0]
                for n in _cited if 0 < n <= len(_chunks_now)}
        if _ids:
            runtime.get_kg_store(app_state).bump_chunk_hits(sorted(_ids))
    except Exception as e:
        logger.warning(f"hit_count 统计失败(不阻断): {e}")

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
                "childSeq": c.get("childSeq"),
                "parentContext": c.get("parentContext"),
                "kbId": c.get("kbId"),
                "kbName": c.get("kbName"),
                "text": (c.get("text") or "").strip()[:_PARENT_CONTEXT_MAX_CHARS],
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
        # 用户级 chunks 瘦身(与单库 answer_question 同口径)
        "chunks": [{k: v for k, v in c.items()
                    if k not in ("childText", "parentChars")}
                   for c in merged_chunks],
        "sources": sources,
    }

def _vector_backend() -> str:
    """观测 endpoint 前缀(向量检索统一 pg:search)。"""
    try:
        from sdk.vector_store import resolve_vector_backend
        return resolve_vector_backend()
    except Exception:
        return "vector"
