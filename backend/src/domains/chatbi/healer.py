"""SQL 自愈 —— 错误分类 + 专项纠正 prompt + 熔断器 (AEE-002)。

移植来源: chat-bi backend/app/ai/sql_healer.py (265 行)。
适配: async llm_chat → 同步 llm 参数;config → 参数。

设计要点 (v1 教训 #32 保留):
  - 自愈 prompt 保留全部安全规则 (自愈无约束 → 能出 DROP)
  - SEC S6: 不传原始 DB 错误给 LLM (防泄露跨租户 schema),只传错误类别 + hint
  - 自愈后的 SQL 走同样三层校验 (不信任自愈结果)
  - 熔断器: 跨查询连续 N 次失败 → 停止自愈 (三态 closed/open/half-open)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from domains.chatbi.security.sql_validator import ValidationResult, validate_sql

logger = logging.getLogger(__name__)

# MySQL 错误码 → 类别 (PG 无数字码,靠文字推断类别由 LLM 处理)
_ERROR_CODE_MAP = {
    "1146": "TABLE_NOT_EXIST", "1051": "TABLE_NOT_EXIST",
    "1054": "COLUMN_NOT_EXIST", "1166": "COLUMN_NOT_EXIST",
    "1064": "SYNTAX_ERROR", "1149": "SYNTAX_ERROR",
    "1052": "AMBIGUOUS_COLUMN", "1060": "AMBIGUOUS_COLUMN",
    "1066": "DUPLICATE_TABLE_ALIAS",
}


class ErrorCategory(str, Enum):
    TABLE_NOT_EXIST = "TABLE_NOT_EXIST"
    COLUMN_NOT_EXIST = "COLUMN_NOT_EXIST"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    AMBIGUOUS_COLUMN = "AMBIGUOUS_COLUMN"
    UNKNOWN = "UNKNOWN"


def extract_error_code(error: str) -> str | None:
    """MySQL 风格 "(1146, msg)" → "1146";PG 错误返回 None。"""
    if not error:
        return None
    m = re.search(r"\((\d{4}),", error)
    return m.group(1) if m else None


class SelfHealCircuitBreaker:
    """跨查询连续失败熔断器(三态: closed/open/half-open, 对标 Claude Code §6.4)。"""

    def __init__(self, threshold: int = 3, cooldown_seconds: int = 60):
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._tripped_at: float = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._tripped_at = time.monotonic()
            logger.error("自愈熔断器触发: 连续 %d 次失败 (>= %d), 停止自愈",
                         self._consecutive_failures, self._threshold)

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
            self._tripped_at = 0.0

    def is_tripped(self) -> bool:
        if self._consecutive_failures < self._threshold:
            return False
        # open → cooldown 过后 half-open 放行一次试探
        if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown_seconds:
            logger.info("自愈熔断器进入 half-open, 允许试探一次")
            self._tripped_at = 0.0
            return False
        return True

    def reset(self) -> None:
        self._consecutive_failures = 0


# 进程级单例熔断器(跨查询共享;pack unload/测试用 reset)
_circuit_breaker: SelfHealCircuitBreaker | None = None


def get_circuit_breaker(threshold: int = 3) -> SelfHealCircuitBreaker:
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = SelfHealCircuitBreaker(threshold=threshold)
    return _circuit_breaker


def reset_circuit_breaker() -> None:
    global _circuit_breaker
    _circuit_breaker = None


@dataclass
class HealResult:
    success: bool = False
    sql: str = ""
    validation: ValidationResult = field(
        default_factory=lambda: ValidationResult(ok=False, reason="未自愈"))
    error: str | None = None
    rounds: int = 0


# 自愈安全规则 (v1 教训 #32: 必须保留全部安全规则)
_SECURITY_RULES = """严格规则 (违反则拒绝):
1. 只能生成 SELECT 语句, 禁止 INSERT/UPDATE/DELETE/DROP/ALTER 等写操作
2. 只能使用下方"允许的列"里的列名, 禁止臆造
3. 禁止危险函数: LOAD_FILE/SLEEP/BENCHMARK/INTO OUTFILE
4. 遵守 data_type 约束 (不对 VARCHAR 做 SUM)
5. 只返回 SQL, 不要解释
6. 为每个 SELECT 输出列添加 AS 中文别名 (schema 中有中文名的用中文名, 聚合列也要有中文别名)"""

_CATEGORY_HINTS = {
    ErrorCategory.COLUMN_NOT_EXIST: "错误原因: 列名不存在。请从'允许的列'里选正确的列名, 不要臆造。",
    ErrorCategory.TABLE_NOT_EXIST: "错误原因: 表不存在。请检查 schema 里可用的表名。",
    ErrorCategory.SYNTAX_ERROR: "错误原因: SQL 语法错误。请修正语法。",
    ErrorCategory.AMBIGUOUS_COLUMN: "错误原因: 列名歧义 (多张表有同名列)。请用'表名.列名'格式明确。",
    ErrorCategory.UNKNOWN: "错误原因: SQL 执行失败。请根据错误信息修正。",
}


def _categorize(error: str) -> tuple[ErrorCategory, str | None]:
    """错误文本 → (类别, 错误码|None)。PG 无数字码时按文字推断。"""
    code = extract_error_code(error)
    if code:
        cat = _ERROR_CODE_MAP.get(code)
        if cat:
            return ErrorCategory(cat), code
    low = (error or "").lower()
    if "relation" in low and "does not exist" in low or "no such table" in low:
        return ErrorCategory.TABLE_NOT_EXIST, code
    if "column" in low and "does not exist" in low or "no such column" in low:
        return ErrorCategory.COLUMN_NOT_EXIST, code
    if "syntax" in low:
        return ErrorCategory.SYNTAX_ERROR, code
    if "ambiguous" in low:
        return ErrorCategory.AMBIGUOUS_COLUMN, code
    return ErrorCategory.UNKNOWN, code


def extract_sql_text(content: str) -> str:
    """从 LLM 响应提取纯 SQL (去 markdown 包裹/尾分号)。"""
    if not content:
        return ""
    sql = content.strip()
    sql = re.sub(r"^```(?:sql|SQL)?\s*\n?", "", sql)
    sql = re.sub(r"\n?```\s*$", "", sql)
    return sql.strip().rstrip(";").strip()


def heal_sql(llm, sql: str, error: str, allowed_columns: set,
             schema_context: str, conv_id: str | None = None,
             circuit_breaker: SelfHealCircuitBreaker | None = None,
             error_detail: str | None = None) -> HealResult:
    """根据执行错误自愈 SQL (自愈结果过同样三层校验)。

    error_detail: 可信的错误详情(如结果自检的统计描述"2/2 列全 NULL"),
    直接进 prompt——仅限平台自产文本;外部 DB 错误仍走 S6 只传类别。"""
    cb = circuit_breaker or get_circuit_breaker()
    if cb.is_tripped():
        logger.warning("自愈熔断器已触发, 跳过自愈")
        return HealResult(error="自愈熔断器已触发 (连续失败过多)")

    category, code = _categorize(error)
    hint = _CATEGORY_HINTS.get(category, _CATEGORY_HINTS[ErrorCategory.UNKNOWN])

    # SEC S6: 仅传错误类别, 不传原始 DB 错误 (防泄露跨库 schema)
    error_for_prompt = f"[{category.value}] {hint}"
    if code:
        error_for_prompt = f"[错误码 {code}] {error_for_prompt}"
    if error_detail:
        error_for_prompt = f"{error_for_prompt}\n【错误详情】{error_detail}"

    prompt = (
        f"你是 BI SQL 修正器。下面这条 SQL 执行失败了, 请修正。\n\n"
        f"{_SECURITY_RULES}\n\n"
        f"【schema】{schema_context}\n"
        f"【允许的列】{', '.join(sorted(allowed_columns)) if allowed_columns else '(见 schema)'}\n\n"
        f"【失败的 SQL】{sql}\n"
        f"【错误信息】{error_for_prompt}\n"
        f"【纠正方向】{hint}\n\n"
        f"只返回修正后的 SQL:"
    )
    try:
        content, _ = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0, stage="chatbi.heal", conv_id=conv_id)
    except Exception as e:
        logger.warning("自愈 LLM 调用失败: %s", e)
        cb.record_failure()
        return HealResult(error=f"自愈 LLM 调用失败: {e}")

    healed = extract_sql_text(content)
    if not healed:
        cb.record_failure()
        return HealResult(error="自愈 LLM 未返回有效 SQL")

    validation = validate_sql(healed, allowed_columns)
    if not validation.ok:
        logger.warning("自愈 SQL 校验失败 (%s): %s",
                       validation.violated_layer, validation.reason)
        cb.record_failure()
        return HealResult(sql=healed, validation=validation,
                          error=f"自愈 SQL 校验失败: {validation.reason}")

    cb.record_success()
    logger.info("自愈成功 (category=%s): %s", category.value, healed[:80])
    return HealResult(success=True, sql=healed, validation=validation, rounds=1)
