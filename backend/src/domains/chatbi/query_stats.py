"""chatbi 查询质量统计 —— 每次问数一行, BI 维度可观测的数据底座。

对标源 observability.py 的 datasource-metrics / slow-queries, 并按复核
报告 P1 扩展为完整 BI 质量指标:
  - 基础: 查询数 / 平均耗时 / 错误率 / 慢查询数(阈值可配);
  - 质量: 自愈轮次命中 / 检索降级 / 图表降级 / 主动澄清(ask-user) 次数。

设计:
  - 写入点在 ask_data finalize(成功/失败/澄清三路径都记), fail-open
    不阻塞查询主流程;
  - 慢查询判定不落列——聚合时按阈值计算(阈值改了历史口径跟着重算);
  - 依赖健康(health/detail)在 api 层组装, 本模块只管统计。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_SLOW_QUERY_MS = 10_000  # 源 config.sql_slow_query_threshold 默认 10s


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


QUERY_STATS_DDL = [
    """CREATE TABLE IF NOT EXISTS chatbi_query_stats (
        id TEXT PRIMARY KEY,
        data_source_id TEXT NOT NULL,
        user_id TEXT,
        conv_id TEXT,
        question TEXT,
        sql_text TEXT,
        duration_ms INTEGER,           -- SQL 执行耗时(不含 LLM 环节)
        row_count INTEGER,
        status TEXT NOT NULL,          -- ok / error / ask
        error_message TEXT,
        heal_rounds INTEGER DEFAULT 0, -- SQL 自愈轮次(0=一次成功)
        retrieval_degraded INTEGER DEFAULT 0,
        chart_degraded INTEGER DEFAULT 0,
        asked_user INTEGER DEFAULT 0,  -- 主动澄清(挂起追问)
        metric_hits INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_qs_ds_time ON chatbi_query_stats(data_source_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qs_duration ON chatbi_query_stats(duration_ms DESC)",
]


def record_query(db, **fields: Any) -> Optional[str]:
    """写入一条查询统计(fail-open: 任何异常只 warning, 返回 None)。

    必填: data_source_id / status;其余字段见 DDL 列名(蛇峰即列名)。
    """
    cols = ("data_source_id", "user_id", "conv_id", "question", "sql_text",
            "duration_ms", "row_count", "status", "error_message",
            "heal_rounds", "retrieval_degraded", "chart_degraded",
            "asked_user", "metric_hits")
    row = {c: fields.get(c) for c in cols}
    if not row.get("data_source_id") or not row.get("status"):
        return None
    row["id"] = str(uuid.uuid4())
    row["created_at"] = _now()
    try:
        with db.connect() as conn:
            conn.execute(
                f"INSERT INTO chatbi_query_stats ({', '.join(row)}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return row["id"]
    except Exception as e:
        logger.warning("查询统计写入失败(不阻塞): %s", e)
        return None


def _since_clause(days: Optional[int]) -> tuple[str, list]:
    """时间窗口子句(days>0 → 近 N 天; None/0 = 全部历史)。返回 (sql, params)。"""
    if not days or days <= 0:
        return "", []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return "created_at >= ?", [cutoff]


def _agg_select(ds_filter: bool, window: str) -> str:
    where_parts = []
    if ds_filter:
        where_parts.append("data_source_id = ?")
    if window:
        where_parts.append(window)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    return f"""
        SELECT data_source_id,
               COUNT(*) AS query_count,
               AVG(duration_ms) AS avg_ms,
               MAX(COALESCE(duration_ms, 0)) AS max_ms,
               SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
               SUM(CASE WHEN COALESCE(duration_ms, 0) > ? THEN 1 ELSE 0 END) AS slow_count,
               SUM(CASE WHEN COALESCE(heal_rounds, 0) > 0 THEN 1 ELSE 0 END) AS heal_count,
               SUM(COALESCE(retrieval_degraded, 0)) AS retrieval_degraded_count,
               SUM(COALESCE(chart_degraded, 0)) AS chart_degraded_count,
               SUM(COALESCE(asked_user, 0)) AS ask_user_count
          FROM chatbi_query_stats
          {where}
         GROUP BY data_source_id"""


def datasource_metrics(db, slow_ms: int = DEFAULT_SLOW_QUERY_MS,
                       data_source_id: Optional[str] = None,
                       days: Optional[int] = None) -> list[dict]:
    """按数据源聚合查询质量指标(对标源 GET /datasource-metrics + P1 扩展)。

    days: 时间窗口(近 N 天;None/0 = 全部历史)——原系统口径为近 24h,
    这里做成参数由前端选择(24h/7天/30天/全部)。
    """
    window_sql, window_params = _since_clause(days)
    sql = _agg_select(bool(data_source_id), window_sql)
    # 占位符顺序对齐 SQL: slow_ms(SELECT 内) → data_source_id → created_at
    # (四审 P1 修复: 此前 cutoff 与 ds 错位, 选时间窗口后汇总恒为空)
    params: list = [slow_ms]
    if data_source_id:
        params.append(data_source_id)
    params.extend(window_params)
    params = tuple(params)
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        total = int(r["query_count"])
        errors = int(r["error_count"])
        avg = float(r["avg_ms"]) if r["avg_ms"] is not None else 0.0
        out.append({
            "data_source_id": r["data_source_id"],
            "query_count": total,
            "avg_ms": round(avg),
            "max_ms": int(r["max_ms"] or 0),
            "error_count": errors,
            "error_rate": round(errors / total * 100, 1) if total else 0.0,
            "slow_count": int(r["slow_count"]),
            "heal_count": int(r["heal_count"]),
            "retrieval_degraded_count": int(r["retrieval_degraded_count"]),
            "chart_degraded_count": int(r["chart_degraded_count"]),
            "ask_user_count": int(r["ask_user_count"]),
        })
    return out


def slow_queries(db, data_source_id: str, slow_ms: int = DEFAULT_SLOW_QUERY_MS,
                 limit: int = 20, days: Optional[int] = None) -> list[dict]:
    """慢查询明细(超阈值, 按耗时倒序;对标源 GET /slow-queries)。"""
    window_sql, window_params = _since_clause(days)
    sql = f"""SELECT id, data_source_id, user_id, question, sql_text,
                      duration_ms, row_count, status, error_message,
                      heal_rounds, created_at
                 FROM chatbi_query_stats
                WHERE data_source_id = ? AND COALESCE(duration_ms, 0) > ?
                {'AND ' + window_sql if window_sql else ''}
                ORDER BY duration_ms DESC LIMIT ?"""
    with db.connect() as conn:
        rows = conn.execute(
            sql, (data_source_id, slow_ms, *window_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_stats(db, retention_days: int) -> int:
    """清理超保留期的统计行(0/负 = 关闭);返回删除条数。

    由元数据刷新守护线程每轮调用(设置 query_stats_retention_days,
    缺省 90 天)——统计表无归档需求, 到期即删。
    """
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_query_stats WHERE created_at < ?", (cutoff,))
        return getattr(cur, "rowcount", 0) or 0


def delete_stats_for_datasource(db, data_source_id: str) -> int:
    """数据源删除时的级联清理(与保存查询/看板/记忆同规则: 一并删除)。"""
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_query_stats WHERE data_source_id = ?",
            (data_source_id,))
        return getattr(cur, "rowcount", 0) or 0
