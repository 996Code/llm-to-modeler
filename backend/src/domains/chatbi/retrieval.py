"""chatbi 两阶段检索 —— 向量召回 topK → LLM 精筛 → schema_context。

【移植来源】自 chat-bi backend/app/ 忠实移植:
  - retriever.py(T022):两阶段检索主流程
    (阶段1 向量召回 top-K + score 阈值过滤;阶段2 LLM 精筛,
     prompt 含假阳性声明、宁缺毋滥、多表全选规则——原文保留);
  - llm_json.py:parse_json_response(剥洋葱式 JSON 容错解析,
    retriever 精筛结果的解析依赖);
  - schema_utils.py 的自包含函数:build_schema_context /
    build_metrics_hint / extract_allowed_columns(检索结果的
    schema_context 构建;图依赖的 expand_with_relationships /
    build_join_path_section 属图谱扩展栈,不在本栈移植范围)。

对标 RAG-002:
  阶段1: 向量召回 top-K (K=20) + score 阈值过滤
  阶段2: LLM 精筛 (宁缺毋滥)
  v1 教训: 检索无结果不 fallback 不随机选表 → 返回友好提示

设计要点(与源差异仅接入方式,流程 1:1):
  - 向量库:SDK MilvusVectorStore(经 stores.ChatBIVectorStore 适配),
    每数据源一个 scope → collection chatbi_{scope}_v1;
    源实现 data_source_id 标量过滤 → scope 定位(per-scope 物理隔离);
    data_source_id 为空 → 全部活跃数据源 scope 合并召回(等价 filter=None)。
  - LLM:函数参数 llm(引擎 LLMClient:.chat 返回文本,.embeddings 批量向量化),
    stage="chatbi.retrieve.*"。
  - 配置:get_settings() → 函数参数 + 模块级默认常量
    (原值取自 chat-bi app/core/config.py)。
  - 同步实现(调用方是同步线程池)。

降级语义(与源一致):
  - 无召回 → 空结果 + 原因 (不 fallback)
  - LLM 失败/非法 → 降级返回原始召回 (向量已过滤, 标记 degraded)
  - LLM 判断无真匹配 → 空结果 + 原因 (宁缺毋滥, 不选 score 最高)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Dict

from domains.chatbi.models import SemanticModelContent
from domains.chatbi.stores import (
    DOC_SCHEMA,
    ChatBIEmbedder,
    ChatBIVectorStore,
    PackRelationalDB,
    SearchResult,
    resolve_scopes,
)

logger = logging.getLogger(__name__)

# ── 默认参数(原值取自 chat-bi app/core/config.py)─────────────────────
# rag_vector_top_k = 20
DEFAULT_TOP_K = 20
# rag_similarity_threshold = 0.35 (BGE 中文分数分布偏低, 0.5 漏召回; 可调)
DEFAULT_SCORE_THRESHOLD = 0.35
# rag_max_schema_tables = 10 (关系扩展后总表数上限;本栈仅作常量预留,
# 图谱扩展栈消费)
DEFAULT_MAX_SCHEMA_TABLES = 10


# ── JSON 容错解析(自 chat-bi llm_json.py 忠实移植)────────────────────

def parse_json_response(content: Optional[str]) -> Any | None:
    """从 LLM 响应里提取 JSON (容错 markdown 包裹/前后文字)。

    解析策略 (剥洋葱):
      1. 直接 json.loads (纯 JSON, 最快路径)
      2. 去 markdown 代码块包裹 (```json ... ```)
      3. 提取第一个 { 到最后一个 } 的 JSON 对象
      4. 提取第一个 [ 到最后一个 ] 的 JSON 数组
      5. 全部失败 → 返回 None (调用方降级)

    为什么返回 None 而非抛异常:
      - 调用方有降级策略 (使用召回原始结果), None 比异常更容易链式降级。
    """
    if not content:
        return None

    text = content.strip()

    # 1. 先尝试直接解析 (纯 JSON, 最快路径)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 去 markdown 代码块包裹 (```json ... ``` 或 ``` ... ```)
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. 提取 JSON 对象 (第一个 { 到最后一个 }; DOTALL 支持跨行)
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass

    # 4. 提取 JSON 数组 (少数 LLM 返回 JSON 数组的场景)
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass

    # 5. 全部失败 → None
    return None


# ── 检索结果结构 ─────────────────────────────────────────────

@dataclass
class RetrievalResult:
    """检索结果。"""
    models: List[Dict[str, Any]] = field(default_factory=list)
    # 每项: {id, name, type, score, text}
    no_match_reason: Optional[str] = None  # 无匹配时的友好提示
    degraded: bool = False  # LLM 精筛降级标记


def retrieve(
    question: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    data_source_id: Optional[str] = None,
    skip_llm_refine: bool = False,
    llm: Any = None,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    conv_id: Optional[str] = None,
) -> RetrievalResult:
    """两阶段检索: 向量召回 → LLM 精筛。

    Args:
        question: 用户自然语言问题
        store: ChatBIVectorStore (SDK Milvus 适配壳;测试可注入替身)
        embedder: ChatBIEmbedder
        data_source_id: 限定数据源 (多租户/多源隔离, 对标 RAG-005;
            None = 全部活跃数据源合并召回, 等价源实现的无过滤检索)
        skip_llm_refine: 跳过阶段2, 直接返回向量召回结果
        llm: 引擎 LLMClient (精筛用;None 时阶段2降级返回原始召回)
        db: pack 关系库 (data_source_id → scope_id 解析)
        scope: 显式物理 scope (优先于 db 解析;测试直连用)
        top_k: 向量召回数量 (默认 20)
        score_threshold: 召回分数阈值 (默认 0.35)
        conv_id: 会话 id (LLM 调用日志关联)

    Returns:
        RetrievalResult — 无召回/无真匹配 → models=[] + no_match_reason

    Raises:
        ValueError: 既无显式 scope 又无 db 可解析 (接线缺陷, fail-fast)
    """
    # ── 阶段 1: 向量召回 ──────────────────────────────────────
    try:
        query_vecs = embedder.embed([question])
        query_vec = query_vecs[0]
    except Exception as e:
        logger.warning("retrieve embed 失败: %s", e)
        return RetrievalResult(no_match_reason="问题向量化失败, 无法检索")

    # 物理 scope 解析:源实现的全局 collection + data_source_id 标量过滤
    # → per-scope collection 定位(未登记 scope 的数据源召回为空,
    # 与源实现"过滤后无结果"同语义)
    scopes = resolve_scopes(db, data_source_id, scope=scope)

    # 多 scope(无 ds 限定的跨源检索)→ 逐 scope 召回后按分数合并截断
    candidates: List[SearchResult] = []
    for sc in scopes:
        try:
            candidates.extend(store.search_records(
                sc,
                query_vec,
                top_k=top_k,
                score_threshold=score_threshold,
                doc_id=DOC_SCHEMA,
            ))
        except Exception as e:
            # 单 scope 检索失败不拖垮跨源合并(与源实现 search 返回空的宁缺毋滥一致)
            logger.warning("retrieve search 失败 (scope=%s): %s", sc[:8], e)
    candidates.sort(key=lambda r: r.score, reverse=True)
    candidates = candidates[:top_k]

    if not candidates:
        logger.info("retrieve: 向量召回为空 (question=%r)", question[:50])
        return RetrievalResult(
            no_match_reason="无法匹配到相关表，请换一种问法或检查数据源",
        )

    # 跳过 LLM 精筛 → 直接返回召回结果 (阶段1 即终态)
    if skip_llm_refine:
        return RetrievalResult(models=_candidates_to_models(candidates))

    # ── 阶段 2: LLM 精筛 ──────────────────────────────────────
    return _llm_refine(question, candidates, llm=llm, conv_id=conv_id)


def _candidates_to_models(candidates: List[SearchResult]) -> List[Dict[str, Any]]:
    """SearchResult → 简化 dict 列表。"""
    return [
        {
            "id": c.record.id,
            "name": c.record.metadata.get("name", c.record.id),
            "type": c.record.metadata.get("type", "model"),
            "score": c.score,
            "text": c.record.text,
        }
        for c in candidates
    ]


def _llm_refine(
    question: str,
    candidates: List[SearchResult],
    llm: Any = None,
    conv_id: Optional[str] = None,
) -> RetrievalResult:
    """阶段 2: LLM 从召回候选里选真正相关的。

    prompt 关键设计 (对标 RAG-002, 原文保留):
      - 声明候选来自向量检索, 可能存在假阳性
      - 要求判断语义是否真正匹配
      - 问题涉及多张表时必须全部选出 (下游图谱扩展依赖多种子表)
      - 无真匹配返回空 (宁缺毋滥, 不选 score 最高)
    """
    if llm is None:
        # 未注入 LLM(接线缺失/调用方明确只要阶段1)→ 与 LLM 失败同款降级
        logger.warning("_llm_refine: 未提供 llm, 降级返回原始召回")
        return RetrievalResult(models=_candidates_to_models(candidates), degraded=True)

    # 构造候选清单
    candidate_lines = []
    for i, c in enumerate(candidates):
        candidate_lines.append(
            f"{i+1}. name={c.record.metadata.get('name', c.record.id)} "
            f"type={c.record.metadata.get('type', 'model')} "
            f"score={c.score:.2f} desc={c.record.text or ''}"
        )
    candidates_text = "\n".join(candidate_lines)

    prompt = (
        f"你是 BI 数据库 Schema Linking 专家。\n"
        f"用户问题: {question}\n\n"
        f"以下是向量检索召回的候选表/指标 (注意: 来自向量检索，可能存在假阳性):\n"
        f"{candidates_text}\n\n"
        f"请判断哪些候选与用户问题真正语义匹配 (维度/对象兼容)。\n"
        f"重要规则:\n"
        f"1. 只要找到问题核心涉及的对象/实体表，就必须返回，哪怕只找到1张表。下游有图谱扩展机制会自动补充关联表（如用户表、分类表），你不需要找全所有表。\n"
        f"2. 问题涉及多张表时尽量全部选出。例如'售后处理时长'涉及售后表+售后日志表。但如果你只确信1张核心表，就只返回这1张，不要返回空。\n"
        f"3. 宁缺毋滥: 只有当所有候选都与问题语义完全无关时，才返回空数组。例如问'订单数'但候选只有'天气表'、'日志表'，才返回空。\n"
        f"4. 不要只选 score 最高的, 要选问题语义涉及的所有表。\n"
        f"只返回 JSON, 格式: "
        f'{{"models": ["匹配的name列表"], "reason": "简短理由"}}'
    )

    try:
        content = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            stage="chatbi.retrieve.refine",
            conv_id=conv_id,
        )
        parsed = parse_json_response(content)
        if parsed is None:
            logger.warning("_llm_refine: LLM 未返回有效 JSON, 降级返回原始召回")
            return RetrievalResult(models=_candidates_to_models(candidates), degraded=True)
    except Exception as e:
        logger.warning("_llm_refine: LLM 精筛失败, 降级返回原始召回: %s", e)
        return RetrievalResult(models=_candidates_to_models(candidates), degraded=True)

    # LLM 选出的 name → 过滤候选
    # 防御: LLM 可能返回数组 ["t1","t2"] 而非对象 {"models":[...]}
    # 统一规整为 dict 结构, 避免类型不匹配崩溃 (对标健壮性: 不为特定 bug 写死)
    if isinstance(parsed, list):
        parsed = {"models": parsed, "reason": "LLM 返回了数组格式"}
    if not isinstance(parsed, dict):
        logger.warning("_llm_refine: LLM 返回非 dict/list, 降级返回原始召回")
        return RetrievalResult(models=_candidates_to_models(candidates), degraded=True)

    selected_names = set(parsed.get("models", []))
    if not selected_names:
        # 宁缺毋滥: LLM 判断无真匹配
        return RetrievalResult(
            no_match_reason=parsed.get("reason", "向量召回的候选均不真正匹配问题"),
        )

    refined = [
        {
            "id": c.record.id,
            "name": c.record.metadata.get("name", c.record.id),
            "type": c.record.metadata.get("type", "model"),
            "score": c.score,
            "text": c.record.text,
            "llm_selected": True,
        }
        for c in candidates
        if c.record.metadata.get("name", c.record.id) in selected_names
    ]

    logger.info("_llm_refine: %d 候选 → %d 精筛 (reason=%s)",
                len(candidates), len(refined), parsed.get("reason", ""))
    return RetrievalResult(models=refined)


# ── schema_context 构建(自 chat-bi schema_utils.py 移植的自包含部分)──

def build_schema_context(
    content: Optional[SemanticModelContent],
    model_names: Optional[List[str]] = None,
) -> str:
    """从语义层构建 schema context 文本 (供 SQL 生成 prompt)。

    格式 (含 data_type, 对标 RAG-005 类型约束):
      biz_orders(订单表): id[BIGINT] user_id[BIGINT] total_amount[DECIMAL]
    指标行 (无指标不输出):
      指标: gmv(成交总额) = SUM(total_amount) WHERE status IN ('paid','shipped')

    设计决策:
      - 列名后附 data_type (如 [BIGINT]), 对标 RAG-005: 类型约束辅助 LLM
        生成类型正确的 SQL (如避免字符串列做 SUM)
      - 列名后附中文名 (如 "中文: 订单金额"), 辅助 LLM 生成 AS 中文别名
      - 关系提示 (如 →biz_users(orders.user_id = users.id)), 辅助 LLM 理解 JOIN 依据
      - 指标行: 让 LLM 知道业务计算口径, 避免自己推断聚合逻辑

    Args:
        content: 语义层内容
        model_names: 只取指定表 (None = 全部)

    Returns:
        schema context 文本
    """
    if content is None or not content.models:
        return ""

    names_filter = set(model_names) if model_names else None
    lines: list[str] = []
    for model in content.models:
        if names_filter is not None and model.name not in names_filter:
            continue
        col_descs = []
        for col in model.columns:
            desc = col.name
            # 附上中文列名 (供 LLM 生成 AS 中文别名)
            # 如 "total_amount" → "total_amount(中文: 总金额)"
            if col.display_name and col.display_name != col.name:
                desc += f"(中文: {col.display_name})"
            if col.data_type:
                desc += f"[{col.data_type}]"
            col_descs.append(desc)
        # 含关系提示 (对标海泰 JOIN 依据)
        # 格式: →biz_users(orders.user_id = users.id)
        rels = []
        for rel in model.relationships:
            rels.append(f"→{rel.target_model}({rel.on})")
        rel_str = " ".join(rels)
        display = f"({model.display_name})" if model.display_name != model.name else ""
        line = f"{model.name}{display}: {' '.join(col_descs)} {rel_str}".strip()
        # 指标行 (有指标时追加, 让 LLM 知道业务计算口径)
        if model.metrics:
            metric_parts = []
            for m in model.metrics:
                m_desc = f"{m.name}({m.display_name}) = {m.formula}"
                if m.condition:
                    m_desc += f" WHERE {m.condition}"
                if m.type == "composite" and m.factor_metric_names:
                    m_desc += f" [子指标: {', '.join(m.factor_metric_names)}]"
                metric_parts.append(m_desc)
            line += f"\n  指标: {'; '.join(metric_parts)}"
        lines.append(line)
    return "\n".join(lines)


def build_metrics_hint(
    content: Optional[SemanticModelContent],
    model_names: Optional[List[str]] = None,
) -> str:
    """从语义层构建业务指标定义文本 (供 SQL 生成 prompt 的 metrics_hint 段)。

    格式:
      biz_orders: gmv(成交总额) = SUM(total_amount) WHERE status IN ('paid','shipped')
                  order_count(订单数) = COUNT(id)

    与 build_schema_context 的区别:
      build_schema_context 输出表级完整信息 (列 + 关系 + 指标),
      build_metrics_hint 只输出指标定义, 用于 prompt 中专用的"指标提示"段。
      两者可同时使用, 让 LLM 从不同角度理解业务语义。

    Args:
        content: 语义层内容
        model_names: 只取指定表 (None = 全部)

    Returns:
        指标定义文本 (无指标返回空字符串)
    """
    if content is None or not content.models:
        return ""

    names_filter = set(model_names) if model_names else None
    lines: list[str] = []
    for model in content.models:
        if names_filter is not None and model.name not in names_filter:
            continue
        if not model.metrics:
            continue
        metric_parts = []
        for m in model.metrics:
            m_desc = f"{m.name}({m.display_name}) = {m.formula}"
            if m.condition:
                m_desc += f" WHERE {m.condition}"
            if m.type == "composite" and m.factor_metric_names:
                m_desc += f" [子指标: {', '.join(m.factor_metric_names)}]"
            metric_parts.append(m_desc)
        lines.append(f"{model.name}: {'; '.join(metric_parts)}")

    return "\n".join(lines) if lines else ""


def extract_allowed_columns(
    content: Optional[SemanticModelContent],
    model_names: Optional[List[str]] = None,
) -> set:
    """从语义层提取白名单列名集合 (供 Layer3 白名单校验 + prompt 注入)。

    为什么从语义层而非检索文本:
      - 检索器只返回匹配的表名和文本描述, 不包含完整列定义
      - 语义层是唯一权威的列定义来源 (含 data_type / display_name)
      - 正则提取会漏列 (无 display_name 时) + 误匹配 (列名与描述文本混淆)

    边界情况:
      - content 为 None → 返回空 set (白名单校验放行所有列)
      - model_names 为 None → 返回全部表的列名
      - model_names 非空 → 只返回指定表的列, 未在列表中的表被跳过
    """
    if content is None or not content.models:
        return set()

    names_filter = set(model_names) if model_names else None
    columns: set = set()
    for model in content.models:
        if names_filter is not None and model.name not in names_filter:
            continue
        for col in model.columns:
            if col.name:
                columns.add(col.name)
    return columns


def retrieve_context(
    question: str,
    content: Optional[SemanticModelContent],
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    data_source_id: Optional[str] = None,
    llm: Any = None,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    conv_id: Optional[str] = None,
) -> Dict[str, Any]:
    """两阶段召回 + schema_context 构建的一站式入口。

    流程: retrieve(召回→精筛) → 命中表名 → build_schema_context。
    源 agent 流程中的图谱表扩展(expand_with_relationships)属图谱扩展栈,
    在其落地前以精筛命中表直接构建 context(种子表语义不变)。

    Returns:
        {retrieval: RetrievalResult, model_names: [...], schema_context: str,
         allowed_columns: set, metrics_hint: str}
    """
    result = retrieve(
        question, store, embedder,
        data_source_id=data_source_id, llm=llm, db=db, scope=scope,
        top_k=top_k, score_threshold=score_threshold, conv_id=conv_id,
    )
    names = [m["name"] for m in result.models]
    return {
        "retrieval": result,
        "model_names": names,
        "schema_context": build_schema_context(content, names),
        "allowed_columns": extract_allowed_columns(content, names),
        "metrics_hint": build_metrics_hint(content, names),
    }
