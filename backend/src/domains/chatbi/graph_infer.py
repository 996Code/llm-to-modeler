"""
知识图谱推断与演化 (T016/T017) —— LLM 关系推断 + confidence 演化 + 语义层同步。

【模块定位】
  chatbi 插件知识图谱栈的"软知识"生产件:
  - 推断: name_pattern (本地) + ai_inferred (LLM) 产出**新**关系建议, 不自动写回
  - 演化: 历史 SQL 频繁 JOIN 挖掘 / 用户反馈信号 调整关系 confidence
  - 同步: linkage 记忆共现 → confidence boost / 新表对发现 → 写回语义层
    (chatbi_semantic_models, 乐观锁 append-only 新版本)
  调用方统一从包入口 domains.chatbi.schema_graph 导入本模块符号。

【设计要点】(沿用 chat-bi 5 大设计原则)
  - source + confidence 双标注: manual > foreign_key > ai_inferred > name_pattern,
    confidence 优先级影响人工复核顺序 (SEM-001 验收标准)
  - 只返回建议，不自动写回 (推断部分): 知识图谱是"软知识"，需人工审核后才进语义层
    (避免错误关系污染下游 SQL 生成 — v1 教训 #46 SQL 校验)
  - fail-closed: LLM 失败 → 降级为 name_pattern，不抛异常 (对标 infer_column_chinese)
  - 可演化: mine_implicit_relationships / apply_feedback_signals 调整 confidence
  - LLM 客户端显式注入: chat-bi 用全局 llm_chat 单例; 本仓库按移植契约改为
    函数参数 llm (鸭子类型, 对齐 src/llm/client.py 的 LLMClient.chat 签名:
    .chat(messages=..., temperature=..., stage=...) -> str), 未注入时降级。

【移植来源】
  chat-bi backend/app/services/knowledge_graph.py 全文 822 行忠实移植:
    - 常量 (NAME_PATTERN_CONFIDENCE / AI_INFERRED_CONFIDENCE / ON 黑名单 /
      FREQUENT_JOIN_* / CORRECTION_PENALTY / PRAISE_BOOST / MIN/MAX_CONFIDENCE) ← L35-57
    - _strip_table_prefix / _infer_relationships_by_name                     ← L62-126
    - _infer_relationships_batch_by_llm (async → sync, llm 参数注入)          ← L129-242
    - infer_knowledge_graph                                                  ← L245-309
    - mine_implicit_relationships / apply_feedback_signals (T017)             ← L316-421
    - VersionConflictError                                                   ← L426-439
    - linkage_memories_to_cooccurrence                                       ← L442-465
    - _compute_confidence_updates / _discover_new_pairs                      ← L468-546
    - apply_confidence_updates (AsyncSession → 平台 PgEngine 同步; 去租户)     ← L549-718
    - sync_linkage_to_graph (去租户; 配置改显式参数)                           ← L721-822
  另移植 _parse_json_response ← chat-bi backend/app/core/llm_json.py
  parse_json_response (L42-114, 推断结果解析依赖, 语义 1:1)。
  适配项（移植契约允许）：async→sync；llm 显式参数（node→stage 命名
  chatbi.graph.*）；配置 get_settings() → 带默认值常量/参数；租户维度删除
  （DB 写入走平台 services.db 的 chatbi_semantic_models 表）；向量索引重建
  改为调用方注入 rebuild_index 回调（检索栈归属其他移植件，未注入时按源
  逻辑"降级不阻塞"跳过）。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from domains.chatbi.graph_core import (
    GRAPH_FEEDBACK_DISCOVER_NEW_PAIRS,
    GRAPH_LINKAGE_CONFIDENCE_BOOST,
    GRAPH_LINKAGE_CO_OCCURRENCE_THRESHOLD,
    GRAPH_LINKAGE_NEW_PAIR_THRESHOLD,
)
from domains.chatbi.models import (
    Model,
    Relationship,
    SemanticModelContent,
)

logger = logging.getLogger(__name__)


# ── 常量 ──────────────────────────────────────────────────────

# name_pattern 推断的置信度 (低于 FK=1.0 和 ai_inferred=0.7)
NAME_PATTERN_CONFIDENCE = 0.6
AI_INFERRED_CONFIDENCE = 0.7

# ON 子句校验: 拒绝 LLM 幻觉输出 (非安全层 — ON 是元数据, 不直接执行;
# 下游 validate_sql() 已有三层防护。此处只过滤明显非法内容)
# 字符级黑名单 (分号/引号, 不存在合法 ON 子句需要这些)
_ON_BLOCKED_CHARS = frozenset([";", "'", '"'])
# 关键词黑名单 (词边界匹配, 防误杀 selected_items/updated_at/deleted_at 等合法列名)
_ON_BLOCKED_WORDS = frozenset(["SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "EXEC", "EXECUTE"])
_ON_WORD_RE = re.compile(r"\b(?:" + "|".join(_ON_BLOCKED_WORDS) + r")\b")

# T017: 频繁 JOIN 阈值（出现 >= 此次数才视为"频繁"，提升 confidence）
FREQUENT_JOIN_THRESHOLD = 3
FREQUENT_JOIN_BOOST = 0.1  # 每次 +0.1，封顶 0.95

# T017: 反馈信号
CORRECTION_PENALTY = 0.15   # 被纠正 → confidence -0.15
PRAISE_BOOST = 0.05         # 被点赞 → confidence +0.05
MIN_CONFIDENCE = 0.1
MAX_CONFIDENCE = 0.95


# ── T016: 推断 ────────────────────────────────────────────────

def _strip_table_prefix(name: str) -> str:
    """去掉常见表前缀，便于 xxx_id → xxx 匹配。

    biz_users → users, t_order → order, dim_date → date
    """
    return re.sub(r"^(biz_|t_|dim_|fact_|fct_)", "", name)


def _infer_relationships_by_name(
    model: Model,
    all_model_names: list[str],
) -> list[Relationship]:
    """命名模式推断: xxx_id → <xxx> 表 (confidence=0.6, source=name_pattern)。

    匹配逻辑:
      1. 找 model 里所有 *_id 结尾的列 (排除自身主键 id)
      2. xxx_id → 找表名为 <prefix>xxx 或 xxx 的目标表
         (biz_orders.user_id → biz_users: 去前缀后 users == user 的复数匹配)
      3. 目标表必须真实存在 (all_model_names)，否则不推断 (避免幻觉)

    宁缺毋滥 (v1 教训 #29): 找不到就跳过，不硬猜。
    """
    if not all_model_names:
        return []

    # 预处理: {去前缀名: 原始表名}，支持单复数匹配
    # e.g. {"users": "biz_users", "user": "biz_users", "products": "biz_products"}
    target_lookup: dict[str, str] = {}
    for table_name in all_model_names:
        if table_name == model.name:
            continue  # 不自引用
        bare = _strip_table_prefix(table_name)
        target_lookup[bare] = table_name
        # 去掉复数 s，让 user_id 也能匹配 users 表
        if bare.endswith("s"):
            target_lookup[bare[:-1]] = table_name

    rels: list[Relationship] = []
    seen_targets: set[str] = set()

    for col in model.columns:
        name = col.name
        if not name.endswith("_id") or name == "id":
            continue

        # user_id → user, category_id → category
        stem = name[:-3]  # 去掉 _id 后缀
        target_table = target_lookup.get(stem)
        if not target_table:
            continue
        if target_table in seen_targets:
            continue

        rels.append(Relationship(
            name=f"{model.name}_to_{target_table}",
            target_model=target_table,
            join_type="LEFT",
            on=f"{model.name}.{name} = {target_table}.id",
            type="N:1",  # xxx_id 指向主键，通常是 N:1
            source="name_pattern",
            confidence=NAME_PATTERN_CONFIDENCE,
        ))
        seen_targets.add(target_table)

    return rels


def _infer_relationships_batch_by_llm(
    models: list[Model],
    all_model_names: list[str],
    llm,  # LLMClient (鸭子类型: .chat(messages=..., temperature=..., stage=...) -> str)
) -> list[Relationship]:
    """LLM 批量推断表间关系 (confidence=0.7, source=ai_inferred)。

    策略: 优先一次性全量发送 (200K 上下文足够容纳 121 表的列名)；
    若表数 > 200 则分批，每批 100 张表，但始终在 prompt 中携带全部表名
    列表 (表名列表很小，~500 tokens)，确保 LLM 能看到跨批的表进行关联，
    结果按 source_model 天然去重合并，不会断层。

    保留全列信息 (LLM 需要完整列上下文才能发现 name_pattern 漏掉的
    语义关联, 如 order_no → orders.order_no 等非 _id 关系)。
    失败/异常/非法 JSON → 返回空 list (fail-closed 降级)。

    移植说明: 源实现为 async llm_chat (返回 (content, meta) 二元组) +
    parse_json_response; 本仓库 LLMClient.chat 为同步且直接返回文本 str
    (见 src/llm/client.py), 故改为同步调用 + 本模块 _parse_json_response,
    stage 命名遵循引擎约定 chatbi.graph.* (供 call_logs 链路追踪)。
    """
    # 只对有 _id 列的表调 LLM (这些表最可能有外键关系)
    candidates = [m for m in models if any(c.name.endswith("_id") for c in m.columns)]
    if not candidates:
        return []

    all_names_set = set(all_model_names)

    # 全量优先: 200 表以内一次性发送 (输入 ~5K tokens, 输出 ~8K tokens, 200K 上下文绰绰有余)
    batch_size = 200 if len(candidates) <= 200 else 100
    all_rels: list[Relationship] = []

    # 全部表名列表 (很小，每批都带上，确保 LLM 能跨批关联)
    all_names_str = ", ".join(all_model_names[:500])

    for batch_start in range(0, len(candidates), batch_size):
        batch = candidates[batch_start:batch_start + batch_size]

        # 保留全列: LLM 需要完整上下文发现语义关联 (非 _id 列也有价值)
        tables_desc = []
        for m in batch:
            col_desc = ", ".join(c.name for c in m.columns)
            tables_desc.append(f"{m.name}: [{col_desc}]")

        prompt = (
            "你是数据库关系推断助手。以下是多张表的列信息，"
            "请判断它们之间的关联关系"
            "（基于列名语义，特别是 xxx_id 列指向其他表主键的关联）。\n\n"
            "所有表名: " + all_names_str + "\n\n"
            "需要推断的表:\n"
            + "\n".join(tables_desc)
            + "\n\n为每张表推断它与其他表的关联关系。"
            "只返回 JSON 数组，每项: "
            '{"source_model": "表名", "target_model": "表名", '
            '"on": "ON条件", "type": "N:1|1:N|1:1|N:N"}。\n'
            "没有关联就返回空数组 []。不要解释。"
        )

        try:
            # 不设单批超时 (源注释: asyncio.wait_for 会断开 LLM 连接中断生成,
            # 靠外层 infer_knowledge_graph 整体兜底); 同步调用天然阻塞至返回
            resp = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                stage="chatbi.graph.infer_relationships",
            )
            # 宿主契约 .chat → (content, meta) tuple(此前 tuple 直传
            # _parse_json_response, .strip() 必炸 → 关系推断永远降级为空)
            content = resp[0] if isinstance(resp, (tuple, list)) else resp
            raw_list = _parse_json_response(content)
            if raw_list is None or not isinstance(raw_list, list):
                logger.warning(
                    "_infer_relationships_batch_by_llm: LLM 返回非法 JSON, 降级为空"
                )
                continue
        except Exception as e:
            logger.warning(
                "_infer_relationships_batch_by_llm: LLM 调用失败, 降级为空: %s", e
            )
            continue

        # 解析结果
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            source = item.get("source_model", "")
            target = item.get("target_model")
            # 校验 source 和 target 都在表名集合里
            if not source or not target:
                continue
            if source not in all_names_set or target not in all_names_set:
                continue  # 过滤幻觉：表名不存在
            if source == target:
                continue  # 自引用跳过
            on_clause = item.get("on", "").strip()
            if not on_clause:
                continue
            # 字符级检查 (分号/引号) + 关键词词边界检查 (防误杀合法列名)
            _on_upper = on_clause.upper()
            if any(c in _on_upper for c in _ON_BLOCKED_CHARS) or _ON_WORD_RE.search(_on_upper):
                logger.warning("on 子句含非法内容, 跳过: %s", on_clause)
                continue
            try:
                card = item.get("type", "N:1")
                if card not in ("N:1", "1:N", "1:1", "N:N"):
                    card = "N:1"
                all_rels.append(Relationship(
                    name=f"{source}_to_{target}",
                    target_model=target,
                    join_type="LEFT",
                    on=on_clause,
                    type=card,
                    source="ai_inferred",
                    confidence=AI_INFERRED_CONFIDENCE,
                ))
            except Exception:
                continue  # 单条非法不阻塞整体

    return all_rels


def infer_knowledge_graph(
    content: SemanticModelContent,
    use_llm: bool = True,
    llm=None,  # LLMClient (use_llm=True 时必传; None → fail-closed 降级为仅 name_pattern)
) -> list[Relationship]:
    """聚合 name_pattern + ai_inferred，产出**新**关系建议（不写回）。

    Args:
        content: 语义层内容（**不会被修改**）
        use_llm: 是否启用 LLM 推断（False 则只用 name_pattern，测试用）
        llm: LLM 客户端 (移植契约: 显式注入, 对齐 LLMClient 鸭子类型;
            use_llm=True 且 llm=None 时降级为仅 name_pattern 并告警)

    Returns:
        新关系建议列表。已存在的 FK/name_pattern/手动关系会被去重。
        去重 key: (model_name, target_model) — 同一表对只保留最高置信度建议。

    设计:
      - 不修改 content (人工审核后才写回，T015 编辑器负责)
      - LLM 失败降级为只有 name_pattern (fail-closed)
    """
    if not content.models:
        return []

    all_names = [m.name for m in content.models]

    # 收集已有关系（用于去重）
    existing: dict[tuple[str, str], float] = {}  # (from, to) -> confidence
    for m in content.models:
        for r in m.relationships:
            key = (m.name, r.target_model)
            existing[key] = max(existing.get(key, 0.0), r.confidence)

    suggestions: list[Relationship] = []
    for model in content.models:
        # name_pattern 总是跑（纯本地，无副作用）
        for r in _infer_relationships_by_name(model, all_names):
            suggestions.append(r)

    # ai_inferred: 一次性批量推断 (利用 200K 上下文, 1 次调用替代 N 次)
    if use_llm:
        if llm is None:
            # LLM 客户端未注入: fail-closed 降级, 不中断 (推断只是建议, 不值得为此抛错)
            logger.warning(
                "infer_knowledge_graph: use_llm=True 但未注入 llm, 降级为仅 name_pattern"
            )
        else:
            try:
                batch_rels = _infer_relationships_batch_by_llm(
                    content.models, all_names, llm,
                )
                suggestions.extend(batch_rels)
            except Exception as e:
                logger.warning("infer_knowledge_graph: LLM 批量推断失败, 降级: %s", e)

    # 去重：已存在的跳过；同表对只保留最高 confidence
    best: dict[tuple[str, str], Relationship] = {}
    for r in suggestions:
        # 推断建议附带的 model_name 信息（Relationship 本身不带 from）
        # 通过 name 字段 "<from>_to_<to>" 解析
        from_table = r.name.split("_to_")[0] if "_to_" in r.name else ""
        key = (from_table, r.target_model)
        if not from_table:
            continue

        # 已有更高/同等置信度的关系 → 跳过
        if key in existing:
            continue

        # 同表对多条建议 → 保留最高 confidence
        if key not in best or r.confidence > best[key].confidence:
            best[key] = r

    return list(best.values())


# ── T017: 演化（算法先行，e2e 留 Phase 6）─────────────────────
# NOTE: 沿用源仓库标记——以下两个函数目前无生产调用方, 仅有单元测试。

def mine_implicit_relationships(
    query_history: list[str],
    existing_relationships: list[Relationship],
) -> dict[tuple[str, str], float]:
    """挖掘历史查询中的频繁 JOIN 表对 → confidence 提升。

    对标 SEM-003 演化: 用户实际查询行为是知识的"信号源"。

    Args:
        query_history: SavedQuery.sql_text 列表
        existing_relationships: 当前语义层关系（用于定位要 boost 的表对）

    Returns:
        {(from_table, to_table): new_confidence} 变更建议。
        只返回有提升的表对（频繁 JOIN >= FREQUENT_JOIN_THRESHOLD 次）。

    实现:
      1. 解析每条 SQL 里的表名（简单正则，FROM/JOIN 后的标识符）
      2. 统计同一条 SQL 里出现的表对共现次数
      3. >= 阈值 → 该表对 confidence += FREQUENT_JOIN_BOOST (封顶 MAX)
    """
    if not query_history or not existing_relationships:
        return {}

    # 已知关系表对 → 当前 confidence
    known_pairs: dict[tuple[str, str], float] = {}
    for r in existing_relationships:
        from_table = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if from_table:
            known_pairs[(from_table, r.target_model)] = r.confidence

    if not known_pairs:
        return {}

    # 统计表对在历史查询中的共现
    cooccur: Counter[tuple[str, str]] = Counter()
    table_re = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
    for sql in query_history:
        tables_in_sql = set(table_re.findall(sql))
        for (t1, t2) in known_pairs:
            if t1 in tables_in_sql and t2 in tables_in_sql:
                cooccur[(t1, t2)] += 1

    # 频繁 → 提升
    suggestions: dict[tuple[str, str], float] = {}
    for pair, count in cooccur.items():
        if count < FREQUENT_JOIN_THRESHOLD:
            continue
        current = known_pairs.get(pair, 0.0)
        boosted = min(
            current + FREQUENT_JOIN_BOOST * count,
            MAX_CONFIDENCE,
        )
        if boosted > current:
            suggestions[pair] = boosted
    return suggestions


def apply_feedback_signals(
    corrections: list[tuple[str, str]],
    praises: list[tuple[str, str]],
    existing_relationships: list[Relationship],
) -> dict[tuple[str, str], float]:
    """根据用户反馈调整关系 confidence（算法先行，e2e 留 Phase 6）。

    对标 SEM-003 演化 + 三态审核 (v1 教训 #41):
      - corrections: 用户纠正过的表对 → confidence 下降
      - praises: 用户点赞的表对 → confidence 提升

    Args:
        corrections: [(from, to), ...] 被纠正的表对
        praises: [(from, to), ...] 被点赞的表对
        existing_relationships: 当前语义层关系

    Returns:
        {(from, to): new_confidence} 变更建议。
    """
    if not existing_relationships:
        return {}

    known: dict[tuple[str, str], float] = {}
    for r in existing_relationships:
        from_table = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if from_table:
            known[(from_table, r.target_model)] = r.confidence

    suggestions: dict[tuple[str, str], float] = {}

    # 纠正 → 下降
    for pair in corrections:
        if pair in known:
            new_conf = max(known[pair] - CORRECTION_PENALTY, MIN_CONFIDENCE)
            if new_conf != known[pair]:
                suggestions[pair] = new_conf

    # 点赞 → 提升（不覆盖纠正结果，纠正优先级更高）
    for pair in praises:
        if pair not in known:
            continue
        if pair in suggestions:
            continue  # 已被纠正，跳过提升
        new_conf = min(known[pair] + PRAISE_BOOST, MAX_CONFIDENCE)
        if new_conf != known[pair]:
            suggestions[pair] = new_conf

    return suggestions


# ── E1 Wave 3: 图谱 confidence 更新 (linkage → 图谱同步) ──────

class VersionConflictError(Exception):
    """乐观锁冲突: 语义层版本在计算期间被其他操作修改。

    Fail-Closed: 不静默吞错, 抛出异常让调用方决定如何处理 (重试/放弃)。
    """

    def __init__(self, expected_version: int, current_version: int, pending_updates: dict):
        self.expected_version = expected_version
        self.current_version = current_version
        self.pending_updates = pending_updates  # {(from, to): new_confidence}
        super().__init__(
            f"版本冲突: 期望 {expected_version}, 当前 {current_version}, "
            f"待更新 {len(pending_updates)} 个表对"
        )


def linkage_memories_to_cooccurrence(mem_store) -> dict[tuple[str, str], int]:
    """从 linkage 记忆 frontmatter 轻聚合表对共现次数。

    遍历所有 type=linkage 的记忆, 读取 co_occurrence 和 tables,
    返回 {(table_a, table_b): co_occurrence} 字典序表对映射。

    Args:
        mem_store: 记忆存储实例 (鸭子类型: 有 list_memories() -> list[dict];
            chat-bi 为 AgentMemoryStore, 由记忆栈移植件提供)

    Returns:
        {(table_a, table_b): co_occurrence} — 字典序表对 → 共现次数
    """
    result: dict[tuple[str, str], int] = {}
    for m in mem_store.list_memories():
        if m.get("type") != "linkage":
            continue
        co = m.get("co_occurrence")
        tables = m.get("tables")
        if not co or not tables or len(tables) != 2:
            continue
        pair = tuple(sorted(tables))
        # 同一表对可能有多条记忆 (理论上不会, 但防御性取 max)
        result[pair] = max(result.get(pair, 0), co)
    return result


def _compute_confidence_updates(
    cooccurrence: dict[tuple[str, str], int],
    existing_relationships: list[Relationship],
    co_occurrence_threshold: int,
    confidence_boost: float,
) -> dict[tuple[str, str], float]:
    """计算已知关系的 confidence boost (不修改任何数据)。

    对共现次数 >= co_occurrence_threshold 的已知表对,
    confidence += confidence_boost * (co_occurrence - threshold + 1),
    封顶 MAX_CONFIDENCE。

    Args:
        cooccurrence: linkage 轻聚合结果
        existing_relationships: 当前语义层关系
        co_occurrence_threshold: 共现阈值 (chat-bi: graph_linkage_co_occurrence_threshold)
        confidence_boost: 每次增量 (chat-bi: graph_linkage_confidence_boost)

    Returns:
        {(from, to): new_confidence} 仅包含有提升的表对
    """
    # 已知关系 → 当前 confidence
    known: dict[tuple[str, str], float] = {}
    for r in existing_relationships:
        from_table = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if from_table:
            known[(from_table, r.target_model)] = r.confidence

    updates: dict[tuple[str, str], float] = {}
    for pair, co in cooccurrence.items():
        if co < co_occurrence_threshold:
            continue
        if pair not in known:
            continue  # 未知表对由新表对发现逻辑处理
        # boost = confidence_boost * 超出阈值的次数 (至少 1 次 boost)
        boost_count = co - co_occurrence_threshold + 1
        new_conf = min(known[pair] + confidence_boost * boost_count, MAX_CONFIDENCE)
        if new_conf > known[pair]:
            updates[pair] = new_conf

    return updates


def _discover_new_pairs(
    cooccurrence: dict[tuple[str, str], int],
    existing_relationships: list[Relationship],
    new_pair_threshold: int,
) -> list[tuple[tuple[str, str], float]]:
    """发现共现频繁但不在现有关系中的新表对。

    对共现 >= new_pair_threshold 且不在 existing_relationships 中的表对,
    建议以 confidence=0.5, source="implicit_mining" 加入。

    Args:
        cooccurrence: linkage 轻聚合结果
        existing_relationships: 当前语义层关系
        new_pair_threshold: 新表对发现阈值 (比 boost 更严)

    Returns:
        [((from, to), confidence), ...] 新发现的表对列表
    """
    # 已知关系表对集合 (双向)
    known_pairs: set[tuple[str, str]] = set()
    for r in existing_relationships:
        from_table = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if from_table:
            known_pairs.add((from_table, r.target_model))
            known_pairs.add((r.target_model, from_table))  # 双向

    new_pairs: list[tuple[tuple[str, str], float]] = []
    for pair, co in cooccurrence.items():
        if co < new_pair_threshold:
            continue
        # 检查双向是否都不在已知关系中
        if pair in known_pairs or (pair[1], pair[0]) in known_pairs:
            continue
        new_pairs.append((pair, 0.5))  # 初始 confidence 0.5

    return new_pairs


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串 (chatbi_semantic_models.created_at 存 TEXT,
    与平台存储方言铁律一致: 时间戳沿用 ISO 字符串存 TEXT)。"""
    return datetime.now(timezone.utc).isoformat()


def apply_confidence_updates(
    db,  # 平台 PgEngine (或任何提供 connect() 上下文管理器的连接引擎, 见 services.db)
    data_source_id: str,
    updates: dict[tuple[str, str], float],
    new_pairs: list[tuple[tuple[str, str], float]] | None = None,
    expected_version: int | None = None,
    rebuild_index: Callable[..., Any] | None = None,
) -> int:
    """将 confidence 更新写入语义层 (乐观锁, append-only 新版本)。

    流程:
      1. 读取当前 is_current=1 的 chatbi_semantic_models 行
      2. 校验 expected_version (乐观锁, 防并发冲突)
      3. 深拷贝 content, 更新已知关系的 confidence
      4. 新表对发现: 加入新 Relationship (source=implicit_mining)
      5. 旧版本 is_current=0, 插入新版本
      6. 重建向量索引 (降级不阻塞; 由调用方注入 rebuild_index 回调)

    移植说明: 源实现为 AsyncSession + SQLAlchemy ORM (含 tenant_filter 租户过滤
    与全局向量索引 rebuild_index); 本仓库按移植契约改为同步平台引擎
    (services.db, `?` 占位符) 直写 chatbi_semantic_models (见 models.py DDL,
    无租户列), 租户维度删除; rebuild_index 改为显式回调注入 (检索栈归属
    其他移植件), 未注入时跳过——与源"失败降级不阻塞 RAG 检索"语义一致。

    Args:
        db: 平台 PgEngine (with db.connect() as conn 事务语义: 成功提交/异常回滚)
        data_source_id: 数据源 ID
        updates: {(from, to): new_confidence} 已知关系的 confidence 更新
        new_pairs: [((from, to), confidence)] 新发现的表对 (None=不发现)
        expected_version: 乐观锁期望版本号 (None=不校验)
        rebuild_index: 向量索引重建回调 (关键字调用: content=..., data_source_id=...;
            None=跳过重建, 由检索栈接线方注入)

    Returns:
        新版本号

    Raises:
        VersionConflictError: 乐观锁冲突
        ValueError: 无当前版本 / 无更新内容
    """
    import copy

    with db.connect() as conn:
        # 1. 读取当前版本 (同 data_source_id 正常仅 1 行 is_current=1;
        #    防御性取第一行——异常多 current 行由第 5 步统一压为 0)
        rows = conn.execute(
            "SELECT id, version, content FROM chatbi_semantic_models "
            "WHERE data_source_id = ? AND is_current = 1",
            (data_source_id,),
        ).fetchall()

        if not rows:
            raise ValueError(f"数据源 {data_source_id} 无当前语义层版本")
        current = rows[0]

        # 2. 乐观锁校验
        if expected_version is not None and current["version"] != expected_version:
            raise VersionConflictError(
                expected_version=expected_version,
                current_version=current["version"],
                pending_updates=updates,
            )

        # 3. 深拷贝 content → 修改 (content 列为 TEXT, 反序列化为 dict;
        #    兼容已反序列化的 dict 形态)
        raw_content = current["content"]
        content_dict = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        new_content = copy.deepcopy(content_dict)
        models = new_content.get("models", [])
        changed = False

        # 更新已知关系的 confidence
        for model_dict in models:
            model_name = model_dict.get("name", "")
            rels = model_dict.get("relationships", [])
            for rel in rels:
                target = rel.get("target_model", "")
                pair = (model_name, target)
                if pair in updates:
                    new_conf = updates[pair]
                    if rel.get("confidence", 0) != new_conf:
                        rel["confidence"] = new_conf
                        # source 保持不变 (manual > foreign_key > ai_inferred > name_pattern)
                        # 不覆盖 source, 只提升 confidence
                        changed = True

        # 4. 新表对发现: 加入新 Relationship
        if new_pairs:
            # 构建表名 → model_dict 映射
            model_by_name: dict[str, dict] = {m.get("name", ""): m for m in models}
            for (from_table, to_table), confidence in new_pairs:
                from_model = model_by_name.get(from_table)
                if from_model is None:
                    # from_table 不在语义层中 (理论上不应发生, 跳过)
                    logger.warning("新表对发现: %s 不在语义层中, 跳过", from_table)
                    continue
                # 检查是否已存在 (防御性)
                existing_targets = {r.get("target_model") for r in from_model.get("relationships", [])}
                if to_table in existing_targets:
                    continue
                # 加入新关系
                from_model.setdefault("relationships", []).append({
                    "name": f"{from_table}_to_{to_table}",
                    "target_model": to_table,
                    "join_type": "LEFT",
                    "on": f"{from_table}.id = {to_table}.{_strip_table_prefix(from_table)}_id",
                    "type": "N:1",
                    "source": "implicit_mining",
                    "confidence": confidence,
                })
                changed = True
                logger.info(
                    "新表对发现: %s → %s (confidence=%.2f, source=implicit_mining)",
                    from_table, to_table, confidence,
                )

        if not changed:
            raise ValueError("无有效更新内容 (所有表对 confidence 未变或表不在语义层中)")

        # 5. 旧版本 is_current=0
        conn.execute(
            "UPDATE chatbi_semantic_models SET is_current = 0 "
            "WHERE data_source_id = ? AND is_current = 1",
            (data_source_id,),
        )

        # 新版本号 = 全局 max + 1
        max_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS max_v FROM chatbi_semantic_models "
            "WHERE data_source_id = ?",
            (data_source_id,),
        ).fetchone()
        new_version = (max_row["max_v"] or 0) + 1

        conn.execute(
            "INSERT INTO chatbi_semantic_models "
            "(id, data_source_id, version, content, is_current, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                uuid.uuid4().hex,
                data_source_id,
                new_version,
                json.dumps(new_content, ensure_ascii=False),
                _now_iso(),
            ),
        )
    # with 块退出即提交 (成功提交/异常回滚, 见 services.db 事务语义)

    # 6. 重建向量索引 (降级不阻塞; 回调由调用方注入——检索栈归属其他移植件)
    if rebuild_index is not None:
        try:
            content_obj = SemanticModelContent(**new_content)
            rebuild_index(content=content_obj, data_source_id=data_source_id)
        except Exception as e:
            logger.warning("图谱更新后重建索引失败, RAG 检索将降级: %s", e)
    else:
        logger.debug("图谱更新后未注入 rebuild_index, 跳过向量索引重建")

    logger.info(
        "图谱 confidence 更新完成: ds=%s v%d→v%d, %d 个表对 boost, %d 个新表对",
        data_source_id, current["version"], new_version,
        len(updates), len(new_pairs or []),
    )
    return new_version


def sync_linkage_to_graph(
    db,  # 平台 PgEngine (或任何提供 connect() 上下文管理器的连接引擎)
    mem_store,  # 记忆存储 (鸭子类型: list_memories() -> list[dict])
    data_source_id: str,
    expected_version: int | None = None,
    *,
    rebuild_index: Callable[..., Any] | None = None,
    co_occurrence_threshold: int = GRAPH_LINKAGE_CO_OCCURRENCE_THRESHOLD,
    confidence_boost: float = GRAPH_LINKAGE_CONFIDENCE_BOOST,
    new_pair_threshold: int = GRAPH_LINKAGE_NEW_PAIR_THRESHOLD,
    discover_new_pairs: bool = GRAPH_FEEDBACK_DISCOVER_NEW_PAIRS,
) -> dict:
    """从 linkage 记忆同步到知识图谱 (整理后调用)。

    流程:
      1. 从 linkage 记忆轻聚合 co_occurrence
      2. 读取当前语义层关系
      3. 计算已知关系的 confidence boost
      4. 新表对发现 (若开关开启)
      5. 调 apply_confidence_updates 写入 (乐观锁)

    移植说明: 源实现读 chat-bi 全局配置 (graph_linkage_* / graph_feedback_*)
    与租户参数; 本仓库按契约改为显式参数注入 (默认值常量见 graph_core),
    租户维度删除, DB 访问改为平台同步引擎。

    Args:
        db: 平台 PgEngine
        mem_store: 记忆存储实例 (鸭子类型)
        data_source_id: 数据源 ID
        expected_version: 乐观锁期望版本号
        rebuild_index: 向量索引重建回调 (透传 apply_confidence_updates)
        co_occurrence_threshold: 共现阈值 (chat-bi: graph_linkage_co_occurrence_threshold=3)
        confidence_boost: 每次 boost 增量 (chat-bi: graph_linkage_confidence_boost=0.1)
        new_pair_threshold: 新表对发现阈值 (chat-bi: graph_linkage_new_pair_threshold=5)
        discover_new_pairs: 新表对发现开关 (chat-bi: graph_feedback_discover_new_pairs=True)

    Returns:
        {"new_version": int, "boosted_pairs": int, "new_pairs": int}

    Raises:
        VersionConflictError: 乐观锁冲突
        ValueError: 无当前版本 / 无更新内容
    """
    # 1. 轻聚合
    cooccurrence = linkage_memories_to_cooccurrence(mem_store)
    if not cooccurrence:
        return {"new_version": None, "boosted_pairs": 0, "new_pairs": 0, "detail": "无 linkage 记忆"}

    # 2. 读取当前语义层关系
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, version, content FROM chatbi_semantic_models "
            "WHERE data_source_id = ? AND is_current = 1",
            (data_source_id,),
        ).fetchall()

    if not rows:
        return {"new_version": None, "boosted_pairs": 0, "new_pairs": 0, "detail": "无当前语义层版本"}

    raw_content = rows[0]["content"]
    content = SemanticModelContent(
        **(json.loads(raw_content) if isinstance(raw_content, str) else raw_content)
    )
    all_relationships: list[Relationship] = []
    for m in content.models:
        all_relationships.extend(m.relationships)

    # 3. 计算 confidence boost
    updates = _compute_confidence_updates(
        cooccurrence=cooccurrence,
        existing_relationships=all_relationships,
        co_occurrence_threshold=co_occurrence_threshold,
        confidence_boost=confidence_boost,
    )

    # 4. 新表对发现
    new_pairs = None
    if discover_new_pairs:
        new_pairs = _discover_new_pairs(
            cooccurrence=cooccurrence,
            existing_relationships=all_relationships,
            new_pair_threshold=new_pair_threshold,
        )

    # 无更新则跳过
    if not updates and not new_pairs:
        return {
            "new_version": None,
            "boosted_pairs": 0,
            "new_pairs": 0,
            "detail": "无达阈值的表对, 无需更新",
        }

    # 5. 写入 (乐观锁)
    new_version = apply_confidence_updates(
        db=db,
        data_source_id=data_source_id,
        updates=updates,
        new_pairs=new_pairs,
        expected_version=expected_version,
        rebuild_index=rebuild_index,
    )

    return {
        "new_version": new_version,
        "boosted_pairs": len(updates),
        "new_pairs": len(new_pairs or []),
    }


# ── LLM JSON 响应解析 (推断结果的容错解析依赖) ────────────────

def _parse_json_response(content: str | None) -> Any | None:
    """从 LLM 响应里提取 JSON (容错 markdown 包裹/前后文字)。

    Args:
        content: LLM 原始返回文本

    Returns:
        解析后的 Python 对象 (dict/list), 或 None (无法解析)

    解析策略 (剥洋葱):
      1. 直接 json.loads (纯 JSON, 最快路径)
      2. 去 markdown 代码块包裹后解析 (```json ... ```)
      3. 提取第一个 { 到最后一个 } 的 JSON 对象
      4. 提取第一个 [ 到最后一个 ] 的 JSON 数组
      5. 全部失败 → 返回 None (调用方降级)

    为什么返回 None 而非抛异常:
      - 调用方有降级策略 (如重试、使用默认值)
      - None 比异常更容易处理链式降级
    """
    if not content:
        return None

    text = content.strip()

    # 1. 先尝试直接解析 (纯 JSON)
    # 这是最常见的场景, 且是最快的路径
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 去 markdown 代码块包裹 (```json ... ``` 或 ``` ... ```)
    # LLM 经常用 markdown 代码块包裹 JSON 输出
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. 提取 JSON 对象 (第一个 { 到最后一个 })
    # 用于 LLM 在 JSON 前后加了文字描述的场景
    # re.DOTALL 让 . 匹配换行符, 支持跨行 JSON
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass

    # 4. 提取 JSON 数组 (第一个 [ 到最后一个 ])
    # 少数 LLM 返回 JSON 数组的场景
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass

    # 5. 全部失败 → 返回 None
    return None
