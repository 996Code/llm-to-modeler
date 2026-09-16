"""chatbi 指标反哺 —— 查询成功后校验 SQL 与指标关系的运行时完整闭环。

【模块定位】
ask_data 管线 visualize 步骤的指标反哺入口(源 T037/E1 反哺栈的收口件):
  1. SQL 命中已知指标 → co_occurrence + 1 (指标越用越可信);
  2. SQL 含新的聚合模式(未在语义层定义)且单表 → 写 metric_suggestion
     记忆(人工在语义层页面采纳的入口;多表 JOIN 不建议, 宁缺毋滥);
  3. 返回 metric_hits 供响应/审计展示([{table, metric, display_name,
     co_occurrence, source, type}])。

【设计】
  - co_occurrence 写语义层 content JSON(chatbi_semantic_models 的 is_current
    行)——原地更新不走版本化: co_occurrence 是统计信息而非语义定义(源
    _persist_co_occurrence 同款决策);SELECT ... FOR UPDATE 行锁防并发丢失
    更新, delta 模式递增。
  - 表集来源适配: 源从 AgentState.current_tables 取本轮涉及表;目标签名无
    state, 从 SQL 的 FROM/JOIN 解析表名并与语义层模型名求交——信息等价
    (都是"本轮查询实际涉及的表");解析不出表 → 返回空(源
    `not state.current_tables → []` 的宁缺毋滥同款)。
  - metric_suggestion 写记忆存储(domains.chatbi.memory 的
    ChatBIMemoryStore), name 规则/按名去重/内容结构 1:1 保留。
  - 命中匹配: SQL 聚合表达式(SUM(col) 等)与 metric.formula 的子串匹配,
    算法与正则原文保留(不做列名模式写死、不缝合记忆召回——源设计原则)。
  - 失败语义(fail-open 按源码): suggestion 写失败逐条捕获不阻断(源同款);
    co_occurrence 持久化失败向上抛出, 由调用方(ask_data visualize 步骤的
    try/except)兜底为 metric_hits=[]——与源"调用侧 except → 无命中"一致。
  - llm 参数: 调用方统一注入约定保留;源实现指标反哺不调 LLM, 参数不使用。

【移植来源】
  自 chat-bi backend/app/ 忠实移植, 函数映射:
    ai/recall.py persist_metric_feedback(mem_store, state, semantic_content,
      conv_id) → persist_metric_feedback(db, llm, sql, content,
      datasource_id, conv_id, *, question)
      (mem_store+state 并入 db+SQL 解析;返回值由"delta 更新清单"升级为
       完整闭环后的 metric_hits, 含 display_name/递增后 co_occurrence)
    api/chat.py _persist_co_occurrence(db, tenant_id, ds_id, updates)
      → _persist_co_occurrence(db, datasource_id, updates)
      (tenant 维度删除;AsyncSession→PackRelationalDB;返回 {metric: 新值}
       供 hits 组装——源该返回由调用方 _metric_hits 的 delta 模式承担)
    调用侧 _metric_hits 组装(chat.py/chat_stream.py) → persist_metric_feedback
      返回值(hit 键 table/metric/display_name/co_occurrence 对齐前端契约)
  适配仅限契约允许项: async→sync、llm 参数注入、存储换 PackRelationalDB、
  tenant 维度删除、state 参数并入(sql 直传 + SQL 解析表集)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from domains.chatbi.models import SemanticModelContent

logger = logging.getLogger(__name__)

# 聚合函数提取 (源 persist_metric_feedback 同款正则, 原文保留:
# 简化匹配 SUM(col), COUNT(...), AVG(col) 等)
AGG_PATTERN = re.compile(
    r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(\s*([^)]+)\s*\)",
    re.IGNORECASE,
)

# SQL 表名 token: 支持 "table"、"schema.table"、双引号/反引号包裹
_SQL_IDENT = r"[\"'`]?([a-zA-Z_][\w]*)[\"'`]?(?:\.[\"'`]?([a-zA-Z_][\w]*)[\"'`]?)?"

# FROM 段的终止边界(遇到子句关键字/括号即止;JOIN 表由 JOIN 分支捕获;
# "(" 边界使 FROM 子查询不吞掉其内部的 FROM, 内层表由独立匹配捕获)
_CLAUSE_BOUNDARY = (
    r"WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION|LEFT|RIGHT|"
    r"INNER|CROSS|FULL|OUTER|JOIN|ON|WINDOW|VALUES|\)|\("
)


def _extract_sql_tables(sql: str) -> list[str]:
    """从 SQL 提取 FROM/JOIN 涉及的表名 (去重保序, 小写敏感保持原样)。

    支持普通/逗号多表 FROM、schema 前缀、引号包裹、别名;子查询内的 FROM
    亦会捕获(其中间 token 可能混入别名噪音, 由后续"与语义层模型名求交"
    过滤, 宁缺毋滥)。
    """
    if not sql:
        return []
    tables: list[str] = []
    # FROM 段 (到下一个子句关键字为止, 段内逗号拆多表)
    for m in re.finditer(
        r"\bFROM\b(.*?)(?=" + _CLAUSE_BOUNDARY + r"|\Z)",
        sql, re.IGNORECASE | re.DOTALL,
    ):
        for part in m.group(1).split(","):
            mm = re.match(r"\s*" + _SQL_IDENT, part.strip())
            if mm:
                tables.append(mm.group(2) or mm.group(1))
    # JOIN 表
    for m in re.finditer(r"\bJOIN\s+" + _SQL_IDENT, sql, re.IGNORECASE):
        tables.append(m.group(2) or m.group(1))
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for t in tables:
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def _extract_sql_aggregations(sql: str) -> list[tuple[str, str]]:
    """从 SQL 提取聚合函数列表 [(func, col), ...] (源同款正则与形态)。"""
    aggs: list[tuple[str, str]] = []
    for match in AGG_PATTERN.finditer(sql or ""):
        aggs.append((match.group(1).upper(), match.group(2).strip()))
    return aggs


def _get_memory_store(db):
    """记忆存储获取 (函数内延迟导入, 便于测试替身替换)。"""
    from domains.chatbi.memory import get_memory_store
    return get_memory_store(db)


def persist_metric_feedback(
    db,
    llm,
    sql: str,
    content: SemanticModelContent,
    datasource_id: str,
    conv_id: Optional[str] = None,
    *,
    question: Optional[str] = None,
) -> list[dict]:
    """运行时指标反哺 — 查询成功后校验 SQL 与指标的关系 (完整闭环)。

    闭环逻辑 (源 persist_metric_feedback + _persist_co_occurrence + 调用侧
    _metric_hits 组装三段合一):
      1. SQL 命中已知 metric → co_occurrence += 1 (指标越用越可信,
         原子写语义层 content JSON);
      2. SQL 聚合公式未被已知 metric 覆盖 且 单表 → 写 metric_suggestion
         记忆 (人工校正入口/发现新指标);多表 JOIN 不建议, 宁缺毋滥;
      3. 返回 metric_hits (供响应 summary/审计/前端展示)。

    设计原则 (源原文):
      - 不缝合记忆系统的关键词召回 (指标信息走 schema_context, 不走 memory)
      - 不写死列名模式判断
      - 宁缺毋滥: 无法判断时跳过, 不写低质量 suggestion

    Args:
        db: pack 关系库 (co_occurrence 写 chatbi_semantic_models;
            suggestion 写 chatbi_agent_memories)
        llm: 引擎 LLMClient——注入约定保留;源实现不调 LLM, 参数不使用
        sql: 本轮生成的 SQL
        content: SemanticModelContent 语义层完整内容 (已知指标的来源)
        datasource_id: 数据源 ID (定位语义层 is_current 行)
        conv_id: 对话 ID (追溯来源, 写入 suggestion 的 conversation_id)
        question: 本轮用户问题 (可选;写入 suggestion 内容的"来源问题"行,
            对应源 state.question;未提供时省略该行)

    Returns:
        metric_hits 列表 [{table, metric, display_name, co_occurrence,
        source, type}];co_occurrence 为本次递增后的累计值 (无语义层行时
        回退为 旧值+delta, 与源 delta 模式展示语义一致)。
        无 SQL/无表集/无聚合 → [] (宁缺毋滥)。
    """
    if not sql:
        return []

    # 本轮涉及表集: 源取 AgentState.current_tables;目标从 SQL 解析。
    # sql_tables_raw = SQL 出现的全部表 (对应源的 current_tables, 不做语义层
    # 覆盖裁剪——多表 JOIN 判定必须用它, 与源"agent 给出 N 张表就是 N 表"一致);
    # current_tables = 与语义层模型名求交 (已知指标按源
    # `model.name not in state.current_tables` 口径圈定)。
    sql_tables_raw = _extract_sql_tables(sql)
    model_names: set[str] = set()
    if content is not None and hasattr(content, "models"):
        model_names = {m.name for m in content.models}
    current_tables = [t for t in sql_tables_raw if t in model_names]
    if not current_tables:
        return []

    # 收集当前查询涉及的表的所有已知指标
    known_metrics: dict[str, dict] = {}  # metric_name → {model, metric}
    if content is not None and hasattr(content, "models"):
        for model in content.models:
            if model.name not in current_tables:
                continue
            for m in model.metrics:
                known_metrics[m.name] = {"model": model.name, "metric": m}

    # 从 SQL 提取聚合函数 (简化匹配: SUM(col), COUNT(...), AVG(col) 等)
    sql_aggs = _extract_sql_aggregations(sql)
    if not sql_aggs:
        return []

    # 记录 co_occurrence 变更 (delta 模式, 由 _persist_co_occurrence 原子递增)
    co_updates: list[dict] = []

    # 1. SQL 命中已知 metric → co_occurrence += 1
    # 匹配逻辑: SQL 的聚合函数+列名与 metric 的 formula 有交集
    for metric_name, info in known_metrics.items():
        metric = info["metric"]
        formula_upper = (metric.formula or "").upper()
        matched = False
        for func, col in sql_aggs:
            agg_expr = f"{func}({col})".upper()
            # 同时匹配带/不带空格的版本
            if agg_expr in formula_upper or f"{func.upper()} ( {col.upper()} )" in formula_upper:
                matched = True
                break
        if matched:
            co_updates.append({
                "table_name": info["model"],
                "metric_name": metric_name,
                "delta": 1,
                "source": metric.source,
                "type": metric.type,
                "display_name": metric.display_name,
                "old_co": int(getattr(metric, "co_occurrence", 0) or 0),
            })
            logger.info("metric_feedback: 指标 %s 命中, delta=1", metric_name)

    # 2. 发现新指标模式: SQL 含聚合但不在已知指标中
    # 只对单表聚合生成 suggestion (多表 JOIN 的聚合太复杂, 容易误判);
    # 单表判定用 sql_tables_raw (SQL 实际出现的表数, 与源
    # `len(state.current_tables) == 1` 同语义;此分支下 current_tables
    # 必然等于 sql_tables_raw——known_metrics 非空要求该表在语义层内)
    if len(sql_tables_raw) == 1 and known_metrics:
        table_name = sql_tables_raw[0]
        try:
            store = _get_memory_store(db)
            # 检查是否已有同名 suggestion (防重复;按数据源收口——四审 P0:
            # 同名表的指标建议在不同库各自独立, 不能跨库互相抑制;
            # suggestion 记忆也必须带归属, 否则无法按库管理)
            existing_names = {
                m.get("name") for m in store.list_memories()
                if m.get("data_source_id") == datasource_id}
        except Exception as e:
            # 存储异常时跳过 suggestion (不阻断命中统计)
            store = None
            logger.warning("metric_feedback: 读取记忆存储失败, 跳过 suggestion: %s", e)
        if store is not None:
            for func, col in sql_aggs:
                agg_expr = f"{func}({col})"
                # 检查这个聚合是否已被已知指标覆盖
                covered = False
                for info in known_metrics.values():
                    if agg_expr.upper() in (info["metric"].formula or "").upper():
                        covered = True
                        break
                if covered:
                    continue
                # 写 metric_suggestion 记忆 (供人工在语义层页面采纳)
                try:
                    suggestion_name = f"metric-suggestion-{table_name}-{func.lower()}-{col.replace('.', '_')}"
                    if suggestion_name in existing_names:
                        continue
                    content_lines = [
                        "## 新指标建议",
                        "",
                        f"表: {table_name}",
                        f"聚合: {agg_expr}",
                        f"来源 SQL: {sql[:200]}",
                    ]
                    if question:
                        content_lines.append(f"来源问题: {question[:100]}")
                    content_lines.extend(["", "建议在语义层页面添加此指标定义。"])
                    store.save_memory(
                        name=suggestion_name,
                        description=f"表 {table_name} 的新指标建议: {agg_expr}",
                        content="\n".join(content_lines),
                        memory_type="metric_suggestion",
                        data_source_id=datasource_id,
                        extra_metadata={
                            "table": table_name,
                            "formula": agg_expr,
                            **({"conversation_id": conv_id} if conv_id else {}),
                        },
                    )
                    existing_names.add(suggestion_name)
                    logger.info(
                        "metric_feedback: 新指标建议 %s (表=%s)",
                        suggestion_name, table_name,
                    )
                except Exception as e:
                    logger.warning("metric_feedback: 写 suggestion 失败: %s", e)

    # 3. co_occurrence 原子持久化 (delta 模式, 不走版本化)
    new_values: dict[str, int] = {}
    if co_updates:
        new_values = _persist_co_occurrence(db, datasource_id, co_updates)

    # metric_hits (源调用侧 _metric_hits 组装的闭环内化;co_occurrence 为
    # 递增后累计值, 语义层行缺失时回退 旧值+delta)
    hits: list[dict] = []
    for u in co_updates:
        hits.append({
            "table": u["table_name"],
            "metric": u["metric_name"],
            "display_name": u["display_name"],
            "co_occurrence": new_values.get(u["metric_name"], u["old_co"] + u["delta"]),
            "source": u["source"],
            "type": u["type"],
        })
    return hits


def _persist_co_occurrence(db, datasource_id: str, updates: list[dict]) -> dict[str, int]:
    """将 co_occurrence 增量原子写入当前语义层 content JSON (不走版本化)。

    co_occurrence 是统计信息而非语义定义, 不触发 append-only 版本。

    使用 SELECT ... FOR UPDATE 锁行避免并发竞态, 在 JSON 中按 delta 递增
    (源 _persist_co_occurrence 1:1;源只 flush 由调用方统一 commit, 目标在
    with 块退出时提交——事务"成功提交/异常回滚"语义等价)。

    Returns:
        {metric_name: 递增后的 co_occurrence} (语义层行不存在 → {}。
        源此场景静默跳过持久化, hits 由调用侧按 delta 展示——目标由
        persist_metric_feedback 回退 旧值+delta 保持同一展示语义)。
    """
    if not updates:
        return {}
    new_values: dict[str, int] = {}
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, content FROM chatbi_semantic_models "
            "WHERE data_source_id = ? AND is_current = 1 FOR UPDATE",
            (datasource_id,),
        ).fetchone()
        if row is None:
            return {}
        raw = row["content"]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        for upd in updates:
            table_name = upd["table_name"]
            metric_name = upd["metric_name"]
            delta = upd.get("delta", 1)  # 增量, 默认 1
            # 在 content JSON 中找到对应表的对应指标, 原子递增 co_occurrence
            for model in data.get("models", []):
                if model.get("name") != table_name:
                    continue
                for m in model.get("metrics", []):
                    if m.get("name") == metric_name:
                        old_count = m.get("co_occurrence", 0) or 0
                        m["co_occurrence"] = old_count + delta
                        new_values[metric_name] = old_count + delta
                        break
                break
        conn.execute(
            "UPDATE chatbi_semantic_models SET content = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), row["id"]),
        )
    logger.info("co_occurrence 持久化: %d 条更新 (delta 模式)", len(updates))
    return new_values
