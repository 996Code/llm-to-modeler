"""SQL 三层校验 (AST + 危险函数 + 白名单列) —— 自 chat-bi T030 移植。

三层校验, 所有 SQL 执行前必须通过 (Fail-Closed, 对标 Claude Code §3.3):
  Layer 1 (AST): sqlglot parse, 拒绝非 SELECT (DROP/DELETE/UPDATE/INSERT/...)
  Layer 2 (危险函数): 拒绝 LOAD_FILE/PG_SLEEP/DBLINK 等文件/DoS/命令执行面
  Layer 3 (白名单列): SQL 列名必须在 schema 白名单集合内

v1 教训 (移植时保留):
  - #46: 必须用 AST 不能用字符串前缀 (isinstance(Select) 会被 CTE 写操作绕过)
  - #32: 自愈后的 SQL 走同样的三层校验 (自愈 prompt 无安全约束 → 能出 DROP)
  - #15: 校验不过 → 不执行, 直接 error
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# 危险函数黑名单: 文件读写 / 文件系统枚举 / DoS / 命令执行 / 跨库 / 连接管理
# 覆盖 MySQL + PostgreSQL 方言 (目标方言 PG, 但 LLM 可能输出任一方言语法)
_DANGEROUS_FUNCTIONS = frozenset({
    "LOAD_FILE", "PG_READ_FILE", "PG_READ_BINARY_FILE", "LO_IMPORT", "LO_EXPORT",
    "PG_LS_DIR", "PG_STAT_FILE", "PG_READ_DIR",
    "SLEEP", "PG_SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "PG_ADVISORY_LOCK",
    "SYSTEM", "EXEC", "DBLINK", "DBLINK_EXEC", "PG_EXECUTE_SERVER_PROGRAM",
    "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND",
})


def _collect_derivable_names(stmt: exp.Expression) -> set:
    """收集 SQL 内部所有可派生的合法标识符 (非真实表列)。

    这些标识符在 SQL 内部定义后可被引用, 不属于白名单但合法:
      - SELECT 别名 (COUNT(*) AS cnt → cnt 可在 ORDER BY/HAVING 引用)
      - CTE 名 + 输出列; 子查询派生表的列; 派生表别名
    原则: 系统性收集所有来源。遗漏 → 误拦合法 SQL; 多收 → 放行(白名单兜底)。
    """
    names: set = set()
    for node in stmt.find_all(exp.Alias):
        if node.alias:
            names.add(node.alias)
    for cte in stmt.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias)
        cte_cols = cte.args.get("alias")
        if cte_cols and hasattr(cte_cols, "columns"):
            for col in cte_cols.columns:
                if hasattr(col, "name") and col.name:
                    names.add(col.name)
    for sub in stmt.find_all(exp.Subquery):
        if sub.alias:
            names.add(sub.alias)
    return names


@dataclass
class ValidationResult:
    """校验结果。ok=True 通过; ok=False 拒绝 (reason + violated_layer)。"""
    ok: bool
    reason: str = ""
    violated_layer: str = ""  # "AST" / "dangerous_function" / "whitelist_column"


def validate_sql(sql, allowed_columns: set | None = None) -> ValidationResult:
    """三层校验 SQL (parse 失败/多语句/写操作默认拒绝)。"""
    if not sql or not sql.strip():
        return ValidationResult(ok=False, reason="空 SQL", violated_layer="AST")

    allowed_columns = allowed_columns or set()

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as e:
        logger.warning("SQL parse 失败 (语法错误): %s", e)
        return ValidationResult(ok=False, reason=f"SQL 语法错误, 无法解析: {e}",
                                violated_layer="AST")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return ValidationResult(
            ok=False, reason=f"禁止多语句拼接 (检测到 {len(statements)} 条语句)",
            violated_layer="AST")

    stmt = statements[0]
    _READONLY = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)
    if not isinstance(stmt, _READONLY):
        return ValidationResult(
            ok=False,
            reason=f"仅允许只读查询 (SELECT/UNION/INTERSECT/EXCEPT), 检测到 {type(stmt).__name__}",
            violated_layer="AST")

    # CTE 内部写操作绕过: WITH upd AS (UPDATE ... RETURNING *) SELECT ...
    _WRITE = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter)
    for node in stmt.find_all(_WRITE):
        return ValidationResult(
            ok=False, reason=f"CTE/子查询中包含写操作 ({type(node).__name__}), 已拒绝",
            violated_layer="AST")

    # Layer 2: 危险函数 (未注册函数是 Anonymous, 真名在 .name)
    for func in stmt.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            func_name = func.name.upper()
        else:
            func_name = func.sql_name().upper() if hasattr(func, "sql_name") else ""
        if func_name in _DANGEROUS_FUNCTIONS:
            return ValidationResult(ok=False, reason=f"禁止使用危险函数: {func_name}",
                                    violated_layer="dangerous_function")

    into = stmt.args.get("into") if hasattr(stmt, "args") else None
    if into is not None:
        return ValidationResult(ok=False, reason="禁止使用 INTO 子句 (可建表或写文件)",
                                violated_layer="dangerous_function")

    # Layer 3: 白名单列 (防 LLM 臆造列名)
    if allowed_columns:
        effective = _collect_derivable_names(stmt) | allowed_columns
        for col in stmt.find_all(exp.Column):
            col_name = col.name
            if not col_name or col_name == "*":
                continue
            if col_name not in effective:
                return ValidationResult(ok=False,
                                        reason=f"列 '{col_name}' 不在语义层白名单内",
                                        violated_layer="whitelist_column")

    return ValidationResult(ok=True)
