"""SQL 契约共享模块 —— 过滤 SQL 片段的机械校验与 XML 转义（纯函数，零 LLM）。

【模块定位】
从 generate_filter_sql 提取的单一事实源（Global Constraint：SQL 契约单事实源），
供 njmind_form 过滤 SQL 与后续 njmind_list pack 复用：
- check_sql_fragment：_step_check 全部校验逻辑的纯函数化
  （片段语义三契约 / 三库黑名单 / 宏白名单 / 占位符键空间 /
  相对年份禁字面量 / 列名白名单）。
- xml_escape_sql：落库前比较符实体化（运行时 SQL 包进 <select> 交 DOM 解析器）。

【契约依据】njmind-modeler 事实源，详见 generate_filter_sql 模块头。
【依赖】词表/白名单常量（SYSTEM_PARAMS/NJMD_MACROS/BPM_MACROS/
DB_SPECIFIC_BLACKLIST/sql_whitelist）复用 _script_common，不另立第二份。

【无目录降级】target_columns["empty"] 为真（跨表引用/上下文缺失）时，
白名单外列名不判失败——降级为提示文本追加进可选出参 notes，由调用方
并入 script_note（刻意设计，勿改成 errors）。
"""
import re

from domains.njmind_form.tools._script_common import (
    SYSTEM_PARAMS, NJMD_MACROS, BPM_MACROS, DB_SPECIFIC_BLACKLIST,
    sql_whitelist,
)

__all__ = [
    "check_sql_fragment", "xml_escape_sql", "SQL_WORDS",
    "SYSTEM_PARAMS", "NJMD_MACROS", "BPM_MACROS", "DB_SPECIFIC_BLACKLIST",
]

_RE_MACRO = re.compile(r"\b(njmd_\w+)\s*\(")
_RE_PLACEHOLDER = re.compile(r"#\{\s*([^}\s]+)\s*\}")
_RE_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(select|from|where|order\s+by|group\s+by|having|insert|update|delete"
    r"|drop|alter|create|truncate|union)\b", re.IGNORECASE)
# 表别名前缀：片段必须裸列名（外层 WHERE t.is_deleted=0 统一持有别名；
# MP 单表注入路径 ExcuteListWrapper 甚至无别名，带前缀直接 SQL 报错）
_RE_ALIAS_PREFIX = re.compile(r"\b(t\d*|ew)\s*\.\s*[A-Za-z_]")
# MyBatis 动态标签（DOM 解析器会把它当 XML 元素执行，必须拒绝）
_RE_MYBATIS_TAG = re.compile(
    r"<\s*(if|where|foreach|choose|when|otherwise|trim|set|bind)\b",
    re.IGNORECASE)
# 相对年份词 → 禁止年份字面量（'2026-…），强制 EXTRACT 动态写法
_RE_RELATIVE_YEAR = re.compile(r"(当年|本年|今年|去年|上年|明年|近\s*\d+\s*年)")
_RE_YEAR_LITERAL = re.compile(r"'\s*\d{4}\s*[-/]")
# EXTRACT(YEAR FROM …)：其 FROM 是函数语法不是查询关键字，检查前剥除
_RE_EXTRACT_FN = re.compile(r"EXTRACT\s*\([^()]*\)", re.IGNORECASE)

# SQL 词法白名单（校验时不算列名的词）
SQL_WORDS = {
    "and", "or", "not", "in", "is", "null", "like", "between", "exists",
    "case", "when", "then", "else", "end", "true", "false",
    "current_timestamp", "current_date", "current_time", "timestamp",
    "coalesce", "cast", "char", "varchar", "int", "decimal", "date",
    "extract", "year", "month", "day",
}

# 运行时 SQL 文本会被包进 <select>…</select> 交 DOM 解析器（ExtremeQuery），
# 平台对 SQL_TEXT 不做转义（兼容动态标签），所以比较符必须以实体形式落库。
_XML_UNESCAPED = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_RE_ENTITY = re.compile(r"&[A-Za-z]+;")


def xml_escape_sql(sql: str) -> str:
    """XML 转义比较符：& < > → 实体；单引号字面量与 #{占位符} 原样保留；
    已是实体（&lt; 等）的 & 不二次转义（幂等，支持迭代生成）。"""
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = sql.find("'", i + 1)
            j = n if j == -1 else j + 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == "#" and sql.startswith("#{", i):
            j = sql.find("}", i)
            j = n if j == -1 else j + 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == "&":
            m = _RE_ENTITY.match(sql, i)
            if m:
                out.append(m.group())
                i = m.end()
                continue
        out.append(_XML_UNESCAPED.get(ch, ch))
        i += 1
    return "".join(out)


def check_sql_fragment(
    sql: str,
    *,
    target_columns: dict,
    own_keys: set,
    is_sub_table: bool,
    user_text: str,
    notes: list = None,
) -> list:
    """机械校验 WHERE 片段（纯函数，不改 state）。

    Args:
        sql: 待校验的 WHERE 片段。
        target_columns: 目标列目录，形如 {"keys": set, "text": str,
            "empty": bool}——"empty" 为真表示上下文无字段清单（跨表引用/
            缺失），此时白名单外列名降级为 note 不判失败（刻意设计）。
        own_keys: 本表单字段 key 集合（#{} 占位符键空间之一）。
        is_sub_table: 子表场景（无 instance_* 列，bpm 宏禁用）。
        user_text: 用户需求原文（相对年份词判定依据，无需预剥标记）。
        notes: 可选出参（list[str]）：无目录降级分支的「请人工核对列名」
            提示追加于此，调用方并入 script_note；不传即丢弃。

    Returns:
        errors: list[str]，空列表 = 校验通过。
    """
    sql = sql or ""
    errors = []

    # 1) 片段语义：禁止完整 SQL 关键字 / MyBatis 动态标签 / 表别名前缀
    #    （EXTRACT(... FROM ...) 的 FROM 是函数语法，先剥除再查关键字）
    keyword_scan = _RE_EXTRACT_FN.sub(" ", sql)
    m = _FORBIDDEN_KEYWORDS.search(keyword_scan)
    if m:
        errors.append(f"只允许 WHERE 片段（布尔表达式），"
                      f"出现禁止关键字: {m.group()}")
    m = _RE_MYBATIS_TAG.search(sql)
    if m:
        errors.append(f"禁止 MyBatis 动态标签 <{m.group(1)}>；"
                      f"条件请直接写为布尔表达式")
    m = _RE_ALIAS_PREFIX.search(sql)
    if m:
        errors.append(f"列名必须裸写（禁止 {m.group(1)}. 表别名前缀）；"
                      f"别名由运行时外层查询统一添加")

    # 2) 三库专有写法黑名单（大小写不敏感）
    lowered = sql.lower()
    for bad in DB_SPECIFIC_BLACKLIST:
        if bad in lowered:
            errors.append(f"三库兼容：禁止专有写法 {bad}"
                          f"（用 ANSI 标准等价物）")

    # 3) 宏白名单；bpm 宏在子表场景禁用
    for name in {g.lower() for g in _RE_MACRO.findall(sql)}:
        if name not in NJMD_MACROS:
            errors.append(f"未知宏函数: {name}"
                          f"（仅支持 {'/'.join(NJMD_MACROS)}）")
        elif name in BPM_MACROS and is_sub_table:
            errors.append(f"子表场景无流程列，宏 {name} 不可用")

    # 4) 占位符键空间：本表字段 key 或系统参数
    own = set(own_keys or set())
    for ph in _RE_PLACEHOLDER.findall(sql):
        if ph not in own and ph not in SYSTEM_PARAMS:
            errors.append(f"占位符 #{{{ph}}} 不是本表单字段或系统参数")

    # 5) 相对年份禁字面量：需求说当年/去年等 → 必须 EXTRACT 动态写法
    if (_RE_RELATIVE_YEAR.search(user_text or "")
            and _RE_YEAR_LITERAL.search(sql)):
        errors.append(
            "相对年份（当年/去年等）禁止硬编码年份字面量，请改用 "
            "EXTRACT(YEAR FROM 列) = EXTRACT(YEAR FROM CURRENT_TIMESTAMP)"
            "（去年 -1 / 明年 +1）")

    # 6) 列名白名单：目标表字段（上下文目录）∪ 系统列（主/子表分流）
    allowed = set((target_columns or {}).get("keys") or set())
    allowed |= sql_whitelist(bool(is_sub_table))
    has_catalog = not bool((target_columns or {}).get("empty"))

    scrubbed = _RE_PLACEHOLDER.sub(" ", sql)
    scrubbed = _RE_ENTITY.sub(" ", scrubbed)  # &lt; 等实体不算标识符
    scrubbed = _RE_EXTRACT_FN.sub(" ", scrubbed)
    scrubbed = re.sub(r"'[^']*'", " ", scrubbed)
    scrubbed = re.sub(
        r"(?:TIMESTAMP|DATE|TIME)\s+'[^']*'", " ", scrubbed, flags=re.IGNORECASE)
    scrubbed = _RE_MACRO.sub(" ", scrubbed)
    scrubbed = re.sub(r"\b\d+(\.\d+)?\b", " ", scrubbed)
    idents = {w for w in _RE_IDENT.findall(scrubbed)
              if w.lower() not in SQL_WORDS}
    unknown_cols = {w for w in idents if w not in allowed}
    if unknown_cols:
        if has_catalog:
            errors.append(f"SQL 使用了白名单外的列名: "
                          f"{', '.join(sorted(unknown_cols))}")
        else:
            # 无目标清单（跨表引用/上下文缺失）：不判失败，note 提示核对
            if notes is not None:
                notes.append(
                    f"请人工核对列名是否存在于目标表: "
                    f"{', '.join(sorted(unknown_cols))}")

    return errors
