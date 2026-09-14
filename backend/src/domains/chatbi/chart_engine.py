"""图表生成引擎 —— LLM 声明式 ECharts + JSON 自愈 + 规则推断降级 (AEE-008)。

移植来源: chat-bi backend/app/ai/chart_agent.py (660 行,T034/T035)。
适配: async llm_chat → 同步 llm 参数;parse_json_response 内联(引擎 chat_json
面向"请求即 JSON"场景,图表是"文本+JSON 提取"场景,保留原自愈链)。

降级链 (fail-closed, 有数据时总有 option):
  LLM 配置(chart_type/dim_col/measure_cols) → JSON 自愈(补括号)
  → inject_data 程序化注入全量数据 → 规则推断(KPI/饼/线/柱/表格)
关键设计: LLM 只选类型与列映射,数据由 inject_data 从完整结果集注入
(不受 LLM 只看 5 行摘要的限制);config 随结果返回供缓存复用。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ChartResult:
    ok: bool = False
    option: dict | None = None
    degraded: bool = False   # 是否降级(自愈/规则推断)
    error: str | None = None
    config: dict | None = None  # LLM 返回的 {chart_type, dim_col, measure_cols}(缓存复用)


# ── JSON 自愈 (补缺失右括号) ─────────────────────────────────

def heal_json(text: str) -> tuple[str, bool]:
    """修复截断的 JSON: 原样解析 → 截到最后完整位置 → 数括号差补右括号 → 重试。"""
    if not text:
        return text, False
    text = text.strip()
    try:
        json.loads(text)
        return text, True
    except json.JSONDecodeError:
        pass

    cleaned = text
    last_valid = max(cleaned.rfind(","), cleaned.rfind("]"),
                     cleaned.rfind("}"), cleaned.rfind('"'))
    if last_valid > 0 and last_valid < len(cleaned) - 1:
        cleaned = cleaned[: last_valid + 1]

    open_square = cleaned.count("[")
    close_square = cleaned.count("]")
    open_brace = cleaned.count("{")
    close_brace = cleaned.count("}")

    healed = cleaned + ("]" * (open_square - close_square)) + ("}" * (open_brace - close_brace))
    try:
        json.loads(healed)
        logger.info("JSON 自愈成功 (补了 %d 个括号)",
                    (open_square - close_square) + (open_brace - close_brace))
        return healed, True
    except json.JSONDecodeError:
        healed2 = re.sub(r",\s*$", "", healed)
        healed2 = (healed2 + ("]" * max(0, open_square - healed2.count("]")))
                   + ("}" * max(0, open_brace - healed2.count("}"))))
        try:
            json.loads(healed2)
            return healed2, True
        except json.JSONDecodeError:
            return healed, False


def _parse_json_response(content: str) -> dict | None:
    """从 LLM 文本提取 JSON(去 markdown 包裹/前后噪声)。"""
    if not content:
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # 截取首 { 到末 } 的片段
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


# ── 数据特征分析 ─────────────────────────────────────────────

_TIME_KEYWORDS = frozenset({
    "month", "date", "day", "year", "time", "quarter", "week",
    "hour", "minute", "timestamp", "datetime", "period",
    "月", "日", "年", "时间", "日期", "季度", "周", "小时", "时",
})
_PIE_MAX_SLICES = 10          # 超过此数饼图不可读(BI 可视化最佳实践)
_NUMERIC_COL_THRESHOLD = 0.8  # 非空值中数值占比 ≥ 此值才视为数值列
_DATAZOOM_THRESHOLD = 20      # 维度值超过此数显示 dataZoom


def _to_float(v) -> float | None:
    """数值类型 → float;其他 None。PG SUM/COUNT 返回 Decimal,必须显式处理。"""
    from decimal import Decimal
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    return None


def analyze_data_shape(columns: list, rows: list) -> dict:
    """分析数据特征(自动识别维度/数值列,不假设第一列=维度)。

    维度列: 第一个非数值列,全数值则取唯一值最少的列;
    table_recommended: 单列多行/维度>50/全文本/单行多数值列 → 不适合图表。
    """
    from decimal import Decimal
    row_count, col_count = len(rows), len(columns)
    empty = {"row_count": row_count, "col_count": 0, "dim_unique_count": 0,
             "numeric_col_count": 0, "numeric_cols": [], "dim_col": None,
             "first_col_is_time": False, "has_ratio_col": False,
             "single_value": False, "table_recommended": False, "summary": "0行0列"}
    if col_count == 0:
        return empty

    col_stats = []
    for ci, col in enumerate(columns):
        unique_vals, numeric_count, non_none = set(), 0, 0
        for r in rows:
            if r and ci < len(r) and r[ci] is not None:
                non_none += 1
                v = r[ci]
                unique_vals.add(str(v))
                if not isinstance(v, bool) and isinstance(v, (int, float, Decimal)):
                    numeric_count += 1
        is_numeric = non_none > 0 and numeric_count / non_none >= _NUMERIC_COL_THRESHOLD
        col_stats.append({"name": col, "unique_count": len(unique_vals),
                          "is_numeric": is_numeric, "non_none": non_none})

    numeric_cols = [s["name"] for s in col_stats if s["is_numeric"]]
    non_numeric_stats = [s for s in col_stats if not s["is_numeric"]]
    dim_col = (non_numeric_stats[0]["name"] if non_numeric_stats
               else (min(col_stats, key=lambda s: s["unique_count"])["name"] if col_stats else None))
    dim_unique_count = next((s["unique_count"] for s in col_stats
                             if s["name"] == dim_col), 0)

    dim_lower = (dim_col or "").lower()
    first_col_is_time = dim_col is not None and any(kw in dim_lower for kw in _TIME_KEYWORDS)
    ratio_keywords = {"ratio", "percent", "pct", "占比", "百分比", "率", "rate", "share", "proportion"}
    has_ratio_col = any(any(kw in c.lower() for kw in ratio_keywords) for c in numeric_cols)
    single_value = row_count == 1 and 0 < len(numeric_cols) <= 1
    table_recommended = (
        (col_count == 1 and row_count > 1)
        or (dim_unique_count > 50)
        or (len(numeric_cols) == 0 and row_count > 1)
        or (row_count == 1 and len(numeric_cols) > 1))

    shape = {"row_count": row_count, "col_count": col_count,
             "dim_unique_count": dim_unique_count,
             "numeric_col_count": len(numeric_cols), "numeric_cols": numeric_cols,
             "dim_col": dim_col, "first_col_is_time": first_col_is_time,
             "has_ratio_col": has_ratio_col, "single_value": single_value,
             "table_recommended": table_recommended}
    parts = [f"{row_count}行"]
    if dim_col and dim_unique_count:
        parts.append(f"维度列'{dim_col}'有{dim_unique_count}个唯一值")
    if numeric_cols:
        parts.append(f"{len(numeric_cols)}个数值列")
    if first_col_is_time:
        parts.append("维度列含时间语义")
    if has_ratio_col:
        parts.append("含占比列")
    shape["summary"] = ", ".join(parts)
    return shape


def _build_kpi_option(val: float, col_name: str) -> dict:
    """KPI 指标卡 ECharts option(gauge 无指针,大数字居中)。"""
    if val > 0:
        g_min, g_max = 0, val * 1.5
    elif val < 0:
        g_min, g_max = val * 1.5, 0
    else:
        g_min, g_max = 0, 1
    return {
        "chart_type": "kpi",
        "series": [{
            "type": "gauge", "startAngle": 0, "endAngle": 0,
            "min": g_min, "max": g_max,
            "pointer": {"show": False}, "progress": {"show": False},
            "axisLine": {"lineStyle": {"width": 0}},
            "axisTick": {"show": False}, "splitLine": {"show": False},
            "axisLabel": {"show": False},
            "detail": {"valueAnimation": True, "formatter": "{value}",
                       "fontSize": 36, "offsetCenter": [0, "0%"], "color": "inherit"},
            "title": {"show": True, "offsetCenter": [0, "60%"], "fontSize": 16},
            "data": [{"value": val, "name": col_name}],
        }],
    }


def infer_chart_by_rule(columns: list, rows: list) -> dict | None:
    """规则推断基础图表(LLM 失败时的降级, fail-closed)。

    单值 → KPI;不适合图表 → table;时间维度 → line;占比/分布(≤10切片)
    → pie;默认 bar。0 行/无列结构返回 None(前端显示"无结果")。
    """
    if not columns or not rows:
        return None

    shape = analyze_data_shape(columns, rows)

    if shape["single_value"]:
        val, col_name = 0, columns[0]
        for ci, c in enumerate(columns):
            v = _to_float(rows[0][ci]) if ci < len(rows[0]) else None
            if v is not None:
                val, col_name = v, c
                break
        return _build_kpi_option(val, col_name)

    if shape.get("table_recommended"):
        return {"chart_type": "table"}

    dim_col = shape.get("dim_col") or columns[0]
    col_index = {c: i for i, c in enumerate(columns)}
    dim_idx = col_index.get(dim_col, 0)
    numeric_cols = shape.get("numeric_cols", [])
    if not numeric_cols and len(columns) >= 2:
        numeric_cols = [columns[-1]]
    measure_indices = [col_index[c] for c in numeric_cols if c in col_index]
    if not measure_indices and len(columns) >= 2:
        measure_indices = [len(columns) - 1]

    # 维度名截断(50 字符): TEXT 列查出超长串会原样进 xAxis/pie name,
    # 万行×10KB 可使 option JSON 膨胀至数十 MB(前端渲染卡顿/传输爆炸)
    names = [str(r[dim_idx])[:50] if r and len(r) > dim_idx else "" for r in rows]
    values = [_to_float(r[measure_indices[0]]) if r and len(r) > measure_indices[0] else None
              for r in rows]
    values = [v if v is not None else 0 for v in values]

    chart_type = "bar"
    if shape["first_col_is_time"]:
        chart_type = "line"
    elif shape["has_ratio_col"] and shape["dim_unique_count"] <= _PIE_MAX_SLICES:
        chart_type = "pie"
    elif (shape["dim_unique_count"] <= _PIE_MAX_SLICES
          and shape["numeric_col_count"] == 1 and shape["row_count"] > 1):
        chart_type = "pie"

    logger.info("规则推断图表类型: %s (数据特征: %s)", chart_type, shape["summary"])

    if chart_type == "pie":
        return {
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "legend": {"bottom": 0, "type": "scroll"},
            "series": [{"type": "pie", "radius": ["35%", "65%"],
                        "label": {"show": True, "formatter": "{b}: {d}%"},
                        "data": [{"name": n, "value": v} for n, v in zip(names, values)]}],
        }

    series = []
    for m_idx in measure_indices:
        col_name = columns[m_idx] if m_idx < len(columns) else ""
        s_vals = [_to_float(r[m_idx]) if r and len(r) > m_idx else None for r in rows]
        series.append({"name": col_name, "type": chart_type,
                       "data": [v if v is not None else 0 for v in s_vals]})
    if not series:
        series = [{"type": chart_type, "data": values}]

    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "type": "scroll",
                   "data": [s["name"] for s in series if s.get("name")]},
        "grid": {"left": "3%", "right": "4%", "bottom": "12%", "containLabel": True},
        "xAxis": {"type": "category", "data": names,
                  "axisLabel": {"interval": 0,
                                "rotate": 30 if names and len(str(names[0])) > 4 else 0}},
        "yAxis": {"type": "value"},
        "series": series,
        "dataZoom": ([{"type": "slider", "start": 0, "end": 100}]
                     if len(names) > _DATAZOOM_THRESHOLD else []),
    }


# ── 图表生成(LLM 只选类型与列映射,数据程序化注入) ─────────────

_CHART_PROMPT = """你是 BI 数据可视化专家。根据查询结果的数据特征选择最合适的图表类型。

图表类型选择规则 (按优先级):
- 单个汇总值 (1行1列数值, 如"总销售额") → kpi 指标卡
- 不适合图表 (单列多行/维度唯一值>50/全文本列) → table 表格
- 趋势/时间序列 (维度列含时间语义) → line 折线图
- 占比/分布 (维度唯一值 ≤ 10 + 单数值列, 如各省份销售额) → pie 饼图
- 对比/排名 (多类别 + 数值, 或多数值列) → bar 柱状图
- 关联/相关性 (两个数值维度) → scatter 散点图

重要: 你只负责选择图表类型和指定数据列映射, 不要自己填充数据!
数据将由系统根据你指定的列映射从完整结果集自动填充。

你必须返回如下结构的 JSON:
- 对于 kpi 指标卡: {{"chart_type": "kpi", "measure_cols": ["数值列名"]}}
- 对于 table 表格: {{"chart_type": "table"}}
- 对于 line/bar/scatter 图: {{"chart_type": "bar|line|scatter",
  "dim_col": "维度列名 (通常是第一列, 类别/时间)", "measure_cols": ["数值列名1", "数值列名2"]}}
- 对于 pie 图: {{"chart_type": "pie", "dim_col": "类别列名", "measure_cols": ["单个数值列名"]}}

规则:
1. 只返回 JSON, 不要解释, 不要 markdown 包裹
2. dim_col 通常是分类或时间维度列
3. measure_cols 是要展示的数值列 (可多个, 但 pie 只能1个, kpi 只能1个)
4. 0 行结果正常返回结构即可

数据特征:
- 数据概况: {data_shape}
- 列: {columns}
- 前5行: {sample}

{hint}

只返回 JSON:"""


def inject_data(chart_config: dict, columns: list, rows: list) -> dict | None:
    """按 LLM 列映射从完整 rows 程序化构建 ECharts option(全量数据注入)。"""
    if not chart_config or not columns or not rows:
        return None

    chart_type = chart_config.get("chart_type", "bar")
    dim_col = chart_config.get("dim_col")
    measure_cols = chart_config.get("measure_cols") or []

    if chart_type == "table":
        return {"chart_type": "table"}

    col_index = {c: i for i, c in enumerate(columns)}

    if chart_type == "kpi":
        val, col_name = 0, columns[0] if columns else ""
        if measure_cols:
            m_idx = col_index.get(measure_cols[0])
            if m_idx is not None and rows and rows[0] and len(rows[0]) > m_idx:
                v = _to_float(rows[0][m_idx])
                val, col_name = (v if v is not None else 0), measure_cols[0]
        elif rows and rows[0]:
            for ci, c in enumerate(columns):
                v = _to_float(rows[0][ci]) if ci < len(rows[0]) else None
                if v is not None:
                    val, col_name = v, c
                    break
        return _build_kpi_option(val, col_name)

    dim_idx = col_index.get(dim_col, 0) if dim_col else 0
    measure_indices = [col_index[c] for c in measure_cols if c in col_index]
    if not measure_indices and len(columns) >= 2:
        measure_indices = [len(columns) - 1]

    names = []
    for r in rows:
        if r and len(r) > dim_idx:
            names.append(str(r[dim_idx])[:50] if r[dim_idx] is not None else "")
        else:
            names.append("")

    if chart_type == "pie":
        if not measure_indices:
            return None
        val_idx = measure_indices[0]
        data = [{"name": names[i] if i < len(names) else "",
                 "value": _to_float(r[val_idx]) or 0}
                for i, r in enumerate(rows) if r and len(r) > val_idx]
        return {
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "legend": {"bottom": 0, "type": "scroll"},
            "series": [{"type": "pie", "radius": ["35%", "65%"],
                        "label": {"show": True, "formatter": "{b}: {d}%"}, "data": data}],
        }

    series = []
    for m_idx in measure_indices:
        col_name = columns[m_idx] if m_idx < len(columns) else ""
        values = []
        for r in rows:
            v = _to_float(r[m_idx]) if r and len(r) > m_idx else None
            values.append(v if v is not None else 0)
        series.append({"name": col_name, "type": chart_type, "data": values})
    if not series:
        return None

    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "type": "scroll", "data": [s["name"] for s in series]},
        "grid": {"left": "3%", "right": "4%", "bottom": "12%", "containLabel": True},
        "xAxis": {"type": "category", "data": names,
                  "axisLabel": {"interval": 0,
                                "rotate": names and len(str(names[0])) > 4 and 30 or 0}},
        "yAxis": {"type": "value"},
        "series": series,
        "dataZoom": ([{"type": "slider", "start": 0, "end": 100}]
                     if len(names) > _DATAZOOM_THRESHOLD else []),
    }


def generate_chart(llm, question: str, columns: list, rows: list,
                   chart_type_hint: str | None = None,
                   conv_id: str | None = None) -> ChartResult:
    """生成 ECharts 图表 (LLM 配置 → JSON 自愈 → 程序化注入 → 规则降级)。"""
    # 0 行: 不画空图表,跳过 LLM (省 token + 避免空图表)
    if not rows:
        return ChartResult(ok=False, option=None, error="查询无结果, 不生成图表")

    sample = rows[:5]
    shape = analyze_data_shape(columns, rows)
    hint = f"用户期望图表类型: {chart_type_hint}" if chart_type_hint else ""
    prompt = _CHART_PROMPT.format(columns=columns, sample=sample,
                                  data_shape=shape["summary"], hint=hint)
    system_msg = "你是 BI 图表类型决策器, 只返回图表配置 JSON (chart_type/dim_col/measure_cols)。"

    try:
        content, _ = llm.chat(
            messages=[{"role": "system", "content": system_msg},
                      {"role": "user", "content": prompt}],
            temperature=0.0, stage="chatbi.chart", conv_id=conv_id)
    except Exception as e:
        logger.warning("图表生成 LLM 失败, 降级规则推断: %s", e)
        option = infer_chart_by_rule(columns, rows)
        return ChartResult(ok=option is not None, option=option, degraded=True, error=str(e))

    chart_config = _parse_json_response(content)
    if chart_config is None:
        healed, ok = heal_json(content)
        if ok:
            try:
                chart_config = json.loads(healed)
                logger.info("图表配置 JSON 自愈成功")
            except json.JSONDecodeError:
                pass

    if chart_config is not None and isinstance(chart_config, dict):
        option = inject_data(chart_config, columns, rows)
        if option is not None:
            return ChartResult(ok=True, option=option, config=chart_config)

    logger.warning("图表配置无效或数据注入失败, 降级规则推断")
    option = infer_chart_by_rule(columns, rows)
    return ChartResult(ok=option is not None, option=option, degraded=True,
                       error="图表配置解析失败, 规则推断降级")
