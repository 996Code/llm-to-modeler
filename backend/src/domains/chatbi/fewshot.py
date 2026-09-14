"""chatbi few-shot 历史匹配 —— 召回相似问题的审核 SQL。

【移植来源】自 chat-bi backend/app/services/fewshot.py(T024)忠实移植:
  - find_fewshot_examples:embed 问题 → 检索 few-shot 分区 → 最多 top_k 条;
  - format_fewshot_prompt:示例 → prompt 片段(格式原文保留);
  - index_fewshot_example:审核通过的 Question-SQL Pair 回流向量库
    (数据回流;失败降级只记日志,不阻塞审核流程)。

对标 RAG-004:
  - 审核过的 Question-SQL Pair 作 few-shot, 最多 3 条
  - Claude Code: Relevant Recall (按需召回, 不全量灌入 prompt)

设计要点(与源差异仅存储接入,流程 1:1):
  - 独立分区(DOC_FEWSHOT)与语义层索引(DOC_SCHEMA)分开,
    对应源实现的独立 fewshot collection;
  - score 阈值过滤 (宁缺毋滥, 弱相关示例会误导 SQL 生成);
  - 失败降级: 返回空列表 (Agent 无 fewshot 也能生成);
  - 源实现的 metadata{question, sql} → 本仓库 SDK chunk 无元数据列,
    chunk text 存 JSON{question, sql};同时经 stores.save_fewshot_example
    把权威元数据行持久化到 PG chatbi_few_shot_examples(管理/审计/多租户删除)。
  - 同步实现(调用方是同步线程池)。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from domains.chatbi.stores import (
    DOC_FEWSHOT,
    ChatBIEmbedder,
    ChatBIVectorStore,
    PackRelationalDB,
    VectorRecord,
    get_or_create_scope,
    resolve_scopes,
    save_fewshot_example,
)

logger = logging.getLogger(__name__)

# fewshot 默认阈值 (历史 SQL 匹配要求较高相关性, 否则误导)
FEWSHOT_SCORE_THRESHOLD = 0.5
# 默认返回条数 (源实现 rag_max_fewshot_examples=3, 防 prompt token 爆炸)
DEFAULT_TOP_K = 3


@dataclass
class FewShotExample:
    """一条 few-shot 示例 (供 prompt 注入)。"""
    question: str
    sql: str
    score: float


def find_fewshot_examples(
    question: str,
    store: ChatBIVectorStore,
    embedder: ChatBIEmbedder,
    data_source_id: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = FEWSHOT_SCORE_THRESHOLD,
    db: Optional[PackRelationalDB] = None,
    scope: Optional[str] = None,
    conv_id: Optional[str] = None,
) -> List[FewShotExample]:
    """检索相似历史 SQL 作为 few-shot 示例。

    Args:
        question: 当前用户问题
        store: ChatBIVectorStore (few-shot 分区)
        embedder: ChatBIEmbedder
        data_source_id: 数据源 ID (scope 定位, 防跨数据源召回; None = 全部)
        top_k: 最多返回条数 (默认 3, 对标 RAG-004 防 prompt token 爆炸)
        score_threshold: score 低于此值不返回 (宁缺毋滥)
        db: pack 关系库 (data_source_id → scope_id 解析)
        scope: 显式物理 scope (优先于 db 解析;测试直连用)
        conv_id: 会话 id (embed 观测关联)

    Returns:
        FewShotExample 列表, 按 score 降序, 最多 top_k 条。
        失败/无结果返回空列表 (不抛)。
    """
    try:
        vecs = embedder.embed([question])
        query_vec = vecs[0]
    except Exception as e:
        logger.debug("find_fewshot embed 失败, 返回空: %s", e)
        return []

    # scope 定位:源实现的 data_source_id 标量过滤 → per-scope collection
    # (未登记 scope 的数据源召回为空, 与源实现过滤后无结果同语义)
    try:
        scopes = resolve_scopes(db, data_source_id, scope=scope)
    except ValueError as e:
        logger.warning("find_fewshot scope 解析失败, 返回空: %s", e)
        return []

    results = []
    for sc in scopes:
        try:
            results.extend(store.search_records(
                sc,
                query_vec,
                top_k=top_k,
                score_threshold=score_threshold,
                doc_id=DOC_FEWSHOT,
            ))
        except Exception as e:
            logger.debug("find_fewshot search 失败, 返回空: %s", e)
    results.sort(key=lambda r: r.score, reverse=True)
    results = results[:top_k]

    examples: List[FewShotExample] = []
    for r in results:
        # 源实现从 metadata 取 sql;本仓库 chunk text 存 JSON{question, sql}
        payload = _parse_payload(r.record.text or "")
        sql = payload.get("sql") or ""
        if not sql:
            continue
        examples.append(FewShotExample(
            question=payload.get("question") or r.record.text or "",
            sql=sql,
            score=r.score,
        ))

    logger.info(
        "find_fewshot: %d 候选 → %d 示例 (question=%r)",
        len(results), len(examples), question[:50],
    )
    return examples


def _parse_payload(text: str) -> Dict[str, Any]:
    """few-shot chunk text → {question, sql}(非 JSON/解析失败 → 空 dict)。"""
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        return {}


def format_fewshot_prompt(examples: List[FewShotExample]) -> str:
    """把 few-shot 示例格式化成 prompt 片段 (供 Agent SQL 生成注入)。

    对标 RAG-004: 注入历史审核 SQL 作参考。
    """
    if not examples:
        return ""
    lines = ["以下是相似问题的参考 SQL (已审核, 可借鉴写法):"]
    for i, ex in enumerate(examples, 1):
        lines.append(f"{i}. 问题: {ex.question}")
        lines.append(f"   SQL: {ex.sql}")
    return "\n".join(lines)


def index_fewshot_example(
    question: str,
    sql: str,
    embedder: ChatBIEmbedder,
    data_source_id: str,
    store: ChatBIVectorStore,
    db: PackRelationalDB,
    example_id: Optional[str] = None,
    conv_id: Optional[str] = None,
) -> None:
    """把审核通过的 Question-SQL Pair 写入 few-shot 向量库 (数据回流)。

    对标 RAG-004: 反馈审核通过 → 回流知识库 → 后续相似问题可召回作 few-shot。
    失败降级只记日志 (不阻塞审核流程)。

    Args:
        question: 用户原始问题 (作为向量化的文本 + 检索时的匹配键)
        sql: 审核通过的 SQL (检索召回后注入 prompt)
        embedder: ChatBIEmbedder
        data_source_id: 数据源 ID (scope 定位, 防跨数据源召回)
        store: ChatBIVectorStore (few-shot 分区)
        db: pack 关系库 (scope 签发 + 元数据权威行持久化)
        example_id: 唯一 ID (None 则用 question hash)
    """
    try:
        vecs = embedder.embed([question])
        vector = vecs[0]
    except Exception as e:
        logger.warning("fewshot 索引 embed 失败 (不阻塞): %s", e)
        return

    if not example_id:
        # ID 包含 data_source_id, 防止同问题跨数据源覆盖
        example_id = hashlib.md5(f"{data_source_id}:{question}".encode("utf-8")).hexdigest()

    try:
        scope = get_or_create_scope(db, data_source_id)
        # 向量侧:chunk_id = example_id;text 存 JSON(question/sql),
        # 取代源实现 metadata(SDK chunk 无元数据列)
        store.upsert_records(scope, [VectorRecord(
            id=example_id,
            vector=vector,
            metadata={"question": question, "sql": sql,
                      "data_source_id": data_source_id},
            text=json.dumps({"question": question, "sql": sql}, ensure_ascii=False),
        )], doc_id=DOC_FEWSHOT)
        # PG 侧:元数据权威行(管理/审计/多租户删除)
        save_fewshot_example(db, example_id, data_source_id, question, sql)
        logger.info("fewshot 示例已索引: %s (ds=%s)", question[:50], data_source_id[:8])
    except Exception as e:
        logger.warning("fewshot 索引 upsert 失败 (不阻塞): %s", e)
