"""结果自检 —— 0行/异常数字/全NULL 检测(AEE-003)。

移植来源: chat-bi backend/app/ai/result_checker.py (148 行)。
适配: get_settings().sql_max_rows → 参数 max_rows。

检测维度:
  1. ZERO_ROWS: 0 行 (过滤过严;标记但不阻断)
  2. ALL_NULL: 多数列全 NULL (JOIN 方向错)
  3. CARTESIAN_PRODUCT: 行数异常多 (缺 JOIN 条件)
  4. SUSPICIOUS_ZERO: COUNT(*)=0 且非空结果 (可疑)

原则: 异常判断基于结果统计特征,不解析 SQL 文本;真正阻断的是
ALL_NULL / CARTESIAN (明确数据错误特征),0 行让用户自行判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResultIssue(str, Enum):
    ZERO_ROWS = "ZERO_ROWS"
    CARTESIAN_PRODUCT = "CARTESIAN_PRODUCT"
    ALL_NULL = "ALL_NULL"
    SUSPICIOUS_ZERO = "SUSPICIOUS_ZERO"


@dataclass
class CheckResult:
    ok: bool
    issue: ResultIssue | None = None
    reason: str = ""
    suggestion: str = ""


def _count_all_null_columns(rows: list) -> int:
    """统计全 NULL 的列数(0=无;>半数列全空 → JOIN 方向错)。"""
    if not rows:
        return 0
    n_cols = len(rows[0])
    if n_cols == 0:
        return 0
    return sum(
        1 for col_idx in range(n_cols)
        if all(row[col_idx] is None for row in rows)
    )


def _is_count_zero(rows: list, sql: str) -> bool:
    """COUNT(*)=0 的单行结果(WHERE 过滤后可疑)。"""
    if len(rows) != 1:
        return False
    if "COUNT" not in sql.upper():
        return False
    val = rows[0][-1] if rows[0] else None
    return val == 0


def check_result(rows: list, columns: list, sql: str,
                 max_rows: int = 10000) -> CheckResult:
    """自检执行结果(基于统计特征,不依赖 SQL 文本模式)。"""
    n_rows = len(rows)
    n_cols = len(columns)

    # 1. 0 行 — 标记但不阻断(两种语义无法用纯规则区分)
    if n_rows == 0:
        return CheckResult(
            ok=True, issue=ResultIssue.ZERO_ROWS,
            reason="查询返回 0 行, 可能过滤条件过严或数据中无匹配",
            suggestion="如非预期, 检查 WHERE 条件或换查询维度")

    # 2. 多数列全空(JOIN 完全没匹配上的统计特征)
    if n_cols >= 2:
        null_cols = _count_all_null_columns(rows)
        if null_cols > n_cols / 2:
            return CheckResult(
                ok=False, issue=ResultIssue.ALL_NULL,
                reason=f"结果 {null_cols}/{n_cols} 列全部为 NULL, 数据实质为空",
                suggestion="检查 JOIN 的表和外键方向, 或 WHERE 条件是否矛盾")

    # 3. 行数异常多(潜在笛卡尔积)——超过 max_rows 一半
    if n_rows > max_rows * 0.5:
        return CheckResult(
            ok=False, issue=ResultIssue.CARTESIAN_PRODUCT,
            reason=f"结果 {n_rows} 行, 异常多, 疑似笛卡尔积或缺少过滤",
            suggestion="检查是否缺少 JOIN ON 条件或 WHERE 过滤")

    # 4. COUNT(*)=0 单行 — 可疑但不阻断
    if _is_count_zero(rows, sql):
        return CheckResult(
            ok=True, issue=ResultIssue.SUSPICIOUS_ZERO,
            reason="COUNT 结果为 0, WHERE 过滤后可能无匹配数据",
            suggestion="确认过滤条件是否正确, 或数据是否符合预期")

    return CheckResult(ok=True)
