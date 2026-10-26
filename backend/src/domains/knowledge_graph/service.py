"""知识库受控证据服务(客服等编排方消费的薄入口;设计文档 C2/4.2)。

与 /search、kb_search 的分工:那些是面向终端用户的交互入口(含答案
生成、标注短路、选库记忆);本服务面向编排方,只做三件事——
  1. 显式指定知识库(不存在即报错,不默认全库、不猜唯一库);
  2. 元数据白名单(空集=零结果,不回退全库);
  3. 返回依据(chunks/subgraph/诊断),不生成答案——业务结论归调用方。

Demo 薄版:单库+单组元数据过滤;P1 扩展为 kb_filters 列表与文本预算。
"""
from typing import Any, Dict, Optional


def retrieve_evidence(app_state, kb_name: str, query: str,
                      conv_id: Optional[str] = None,
                      top_k: Optional[int] = None,
                      on_stage=None,
                      metadata_filter: Optional[Dict[str, Any]] = None,
                      ) -> Dict[str, Any]:
    """显式库+范围 → 混合检索 → 依据。失败抛异常,不静默降级。

    返回 {kb, subgraph, chunks, diagnostics}:
      kb         {"id", "name"}
      subgraph   {"nodes", "edges"}(图谱路命中,前端图谱卡用)
      chunks     检索片段(已按用户级口径瘦身,childText/parentChars 不出域)
      diagnostics 范围与召回观测(docFilterCount/chunkCount),不含未授权信息
    """
    from domains.knowledge_graph import retrieval, runtime

    store = runtime.get_kg_store(app_state)
    kb = store.get_kb_by_name(kb_name)
    if not kb:
        raise ValueError(f"知识库「{kb_name}」不存在——请检查资料库配置")

    doc_filter = None
    if metadata_filter:
        doc_filter = store.filter_doc_ids_by_metadata(kb["id"], metadata_filter)
    diagnostics: Dict[str, Any] = {
        "kb": kb_name,
        "docFilterCount": (len(doc_filter) if doc_filter is not None else None),
    }
    if doc_filter is not None and not doc_filter:
        # 白名单为空:零结果(空范围不是"放行全库"的理由)
        return {"kb": {"id": kb["id"], "name": kb.get("name") or ""},
                "subgraph": {"nodes": [], "edges": []},
                "chunks": [], "diagnostics": diagnostics}

    retrieved = retrieval.hybrid_retrieve(
        app_state, kb, query, conv_id=conv_id, top_k=top_k,
        on_stage=on_stage, doc_filter=doc_filter)
    # 瘦身口径与 answer_question 一致:检索内部诊断字段不出域
    chunks = [{k: v for k, v in c.items()
               if k not in ("childText", "parentChars")}
              for c in retrieved.get("chunks") or []]
    diagnostics["chunkCount"] = len(chunks)
    return {"kb": {"id": kb["id"], "name": kb.get("name") or ""},
            "subgraph": retrieved.get("subgraph") or {"nodes": [], "edges": []},
            "chunks": chunks, "diagnostics": diagnostics}
