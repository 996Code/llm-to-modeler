"""chatbi 语义层栈 —— 信息模式内省 → LLM 富化 → 指标推断 → 示例问题 → 版本落库。

【模块定位】
数据源扫描的完整语义层流水线与版本管理,chatbi 插件的"地基":
  - scan_datasource: 连业务库(psycopg3/pymysql 直连)内省表/列/注释/主键/外键,
    组装 SemanticModelContent;LLM 给无注释列补中文展示名;规则+LLM 两阶段推断
    业务指标;生成示例问题(带降级链);版本落库(chatbi_semantic_models)。
  - load_current_content / load_content / save_content / diff_versions / rollback /
    delete_by_datasource: 语义层的版本读写面(api.py 的语义端点全部依赖)。
  - validate_metric_formula: 指标公式注入防御校验(F9,导出给 API 层)。
  - generate_sample_questions: 示例问题生成(LLM → 规则降级 → 硬编码兜底)。

【设计】
  - 零功能阉割移植:源仓库的扫描全流程、降级链、防御正则逐点保留;
    适配点仅限宿主环境差异(见"移植来源"):
      async → 同步;SQLAlchemy 引擎池 → 直连内省(psycopg3/pymysql,扫描完即关);
      全局 llm_client → 注入 llm 参数(.chat 返回 (content, meta) 或 str,
      .chat_json 返回 dict,均兼容);租户/审计 → 宿主身份,不在此模块。
  - 宁缺毋滥:LLM 任一环节失败都降级不阻塞扫描(退化列名/仅规则指标/规则问题);
    指标逐条过 Pydantic 校验,坏条目丢弃(composite 缺 factor 直接判无效)。
  - 版本管理 append-only:save_content 永远落新版本并翻转 is_current;
    内容与当前版本完全一致时幂等返回原版本号(不产空版本,自动刷新依赖此语义);
    rollback = 把历史版本内容再落成新版本(不改写历史)。
  - 进度回写:progress_cb(pct, stage) 由调用方(tasks.py)落 chatbi_data_sources
    的 scan_progress/scan_stage;回调异常只告警不阻塞扫描。

【移植来源】
  - scan/enrich/metrics/questions/版本管理 ← chat-bi backend/app/services/semantic_scanner.py
    (614 行) + backend/app/api/data_sources.py 的 _run_scan_background 扫描流水线
    (阶段进度/异常降级语义) + backend/app/api/semantic_models.py 的回滚/diff 端点
  - diff ← chat-bi backend/app/services/semantic_diff.py(132 行,纯函数)
  - 示例问题 ← chat-bi backend/app/ai/question_generator.py(降级链保留)
  - 结构契约 ← domains/chatbi/models.py(SemanticModelContent 等,含 CHATBI_DDL)
  - 删减(契约允许):SQLAlchemy 引擎池(不移植,直连内省替代);知识图谱 LLM 推断
    与向量索引重建不在本栈(graph_infer/indexing 为独立模块,由任务编排层组合);
    asyncio 并发信号量 → 顺序循环;asyncio.wait_for 阶段超时 → 依赖 LLM 客户端
    自身超时;审计日志 → 宿主任务层。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from domains.chatbi.models import (
    _DDL_DML_KEYWORDS,
    _METRIC_DANGEROUS,
    Column,
    Metric,
    Model,
    Relationship,
    SemanticModelContent,
)

logger = logging.getLogger(__name__)


# ── 常量(移植自源配置,含宿主平台表适配) ─────────────────────────

# 系统表名单:富化/指标推断/示例问题时跳过(用户不会查,省 LLM 调用)。
# 前半是 chat-bi 源配置默认名单(移植保留);后半是本宿主 llm-to-modler 的
# 平台元数据表(适配:业务数据源若指向平台同库,插件表不参与语义富化)。
_SYSTEM_TABLES = frozenset({
    # chat-bi 源配置 system_tables 默认名单
    "tenants", "users", "data_sources", "semantic_models", "conversations",
    "saved_queries", "audit_logs", "dashboards", "dashboard_widgets",
    # llm-to-modler 宿主平台表(适配)
    "tasks", "task_logs", "call_logs", "events", "session_meta",
    "session_pack_state", "pack_settings",
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
})

# 内省时排除的 schema:PG 系统 schema + 平台插件 pack schema(一体机/测试环境下
# 业务库与平台库同机时,插件元数据表不是业务表)。MySQL 侧另有系统库名单。
_PG_EXCLUDED_SCHEMAS = ("pg_catalog", "information_schema", "chatbi", "knowledge_graph")
_MYSQL_EXCLUDED_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys",
                           "chatbi", "knowledge_graph")


def _now() -> str:
    """ISO TEXT 时间戳(与平台 Store 的存储规约一致)。"""
    return datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════════════
# 业务库内省(psycopg3 / pymysql 直连;替代 SQLAlchemy inspect)
# ════════════════════════════════════════════════════════════════

def _connect_business(connect_info: dict):
    """按 connect_info 直连业务库(不建引擎池,扫描完由调用方关闭)。

    connect_info 契约: {host, port, user, password, database, db_type}
    (兼容 username 别名;port 缺省 5432)。
    """
    db_type = (connect_info.get("db_type") or "postgresql").lower()
    user = connect_info.get("user") or connect_info.get("username") or ""
    port = int(connect_info.get("port") or 5432)
    if db_type == "postgresql":
        import psycopg
        return psycopg.connect(
            host=connect_info.get("host") or "127.0.0.1", port=port, user=user,
            password=connect_info.get("password") or "",
            dbname=connect_info.get("database") or "",
            connect_timeout=10, autocommit=True)
    if db_type == "mysql":
        import pymysql
        return pymysql.connect(
            host=connect_info.get("host") or "127.0.0.1", port=port, user=user,
            password=connect_info.get("password") or "",
            database=connect_info.get("database") or "",
            connect_timeout=10, charset="utf8mb4")
    raise ValueError(f"不支持的数据库类型: {db_type}")


def _pg_format_type(data_type, char_len, num_prec, num_scale) -> str:
    """information_schema 类型 → 紧凑 SQL 类型串(风格对齐源实现 str(col.type))。

    例: character varying+n → VARCHAR(n);numeric(10,2) → NUMERIC(10,2);
    timestamp without time zone → TIMESTAMP。semantic_type 推断与指标规则
    都按大写类型串匹配(VARCHAR/NUMERIC/INTEGER/...)。
    """
    t = (data_type or "").upper()
    if t == "CHARACTER VARYING":
        return f"VARCHAR({char_len})" if char_len else "VARCHAR"
    if t == "CHARACTER":
        return f"CHAR({char_len})" if char_len else "CHAR"
    if t == "TIMESTAMP WITHOUT TIME ZONE":
        return "TIMESTAMP"
    if t == "TIMESTAMP WITH TIME ZONE":
        return "TIMESTAMPTZ"
    if t == "TIME WITHOUT TIME ZONE":
        return "TIME"
    if t == "TIME WITH TIME ZONE":
        return "TIMETZ"
    if t in ("NUMERIC", "DECIMAL") and num_prec is not None:
        scale = num_scale if num_scale is not None else 0
        return f"{t}({num_prec},{scale})"
    return t


def _pg_introspect(conn) -> dict:
    """PG 内省:表/列/注释/主键/外键(information_schema + pg_catalog 注释)。

    返回 {tables: {表名: {comment, columns: [{name, data_type, comment}]}},
          primary_keys: {(表, 列)}, foreign_keys: [{table, column, ref_table, ref_column}]}
    """
    # 1. 参与扫描的 schema:排除系统 schema 与平台插件 schema(见模块常量注释)
    # 注: psycopg3 带参执行会扫描 SQL 里的 '%',LIKE 通配符改用 substr 前缀比较
    schemas = [r[0] for r in conn.execute(
        "SELECT nspname FROM pg_catalog.pg_namespace "
        "WHERE nspname <> 'information_schema' "
        "AND substr(nspname, 1, 3) <> 'pg_' "
        "AND nspname <> ALL(%s)", (list(_PG_EXCLUDED_SCHEMAS),)).fetchall()]
    result = {"tables": {}, "primary_keys": set(), "foreign_keys": []}
    if not schemas:
        return result

    # 2. 列清单(联 pg_description 拿列注释;objsubid=0 是表注释,>0 才是列注释)
    for r in conn.execute(
        "SELECT cols.table_name, cols.column_name, cols.data_type, "
        "cols.character_maximum_length, cols.numeric_precision, cols.numeric_scale, "
        "COALESCE(dsc.description, '') AS comment "
        "FROM information_schema.columns cols "
        "JOIN pg_catalog.pg_class cls ON cls.relname = cols.table_name "
        "JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace "
        " AND ns.nspname = cols.table_schema "
        "LEFT JOIN pg_catalog.pg_description dsc "
        " ON dsc.objoid = cls.oid AND dsc.objsubid = cols.ordinal_position "
        "WHERE cols.table_schema = ANY(%s) "
        "AND cls.relkind IN ('r', 'p') "
        "ORDER BY cols.table_name, cols.ordinal_position", (schemas,)):
        table = r[0]
        result["tables"].setdefault(table, {"comment": "", "columns": []})
        result["tables"][table]["columns"].append({
            "name": r[1],
            "data_type": _pg_format_type(r[2], r[3], r[4], r[5]),
            "comment": r[6] or "",
        })

    # 3. 表注释(pg_class relkind r=普通表 / p=分区表)
    for r in conn.execute(
        "SELECT c.relname, COALESCE(obj_description(c.oid, 'pg_class'), '') "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p')", (schemas,)):
        if r[0] in result["tables"]:
            result["tables"][r[0]]["comment"] = r[1] or ""

    # 4. 主键(semantic_type=key 判定用)
    for r in conn.execute(
        "SELECT tc.table_name, kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON kcu.constraint_name = tc.constraint_name "
        " AND kcu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = ANY(%s)", (schemas,)):
        result["primary_keys"].add((r[0], r[1]))

    # 5. 外键(constraint_column_usage 对 FK 展示的是被引用表/列)
    for r in conn.execute(
        "SELECT tc.table_name, kcu.column_name, "
        "ccu.table_name AS ref_table, ccu.column_name AS ref_column "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON kcu.constraint_name = tc.constraint_name "
        " AND kcu.table_schema = tc.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name = tc.constraint_name "
        " AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = ANY(%s)", (schemas,)):
        result["foreign_keys"].append(
            {"table": r[0], "column": r[1], "ref_table": r[2], "ref_column": r[3]})
    return result


def _mysql_introspect(conn, database: str) -> dict:
    """MySQL 内省:与 PG 版对齐(information_schema,按库名过滤系统库)。

    pymysql 无 Connection.execute(只有 query+cursor)——统一走 cursor,
    返回 tuple 行(PG 路径 psycopg 默认 tuple 行, 两侧下标访问一致)。
    """
    result = {"tables": {}, "primary_keys": set(), "foreign_keys": []}
    if not database:
        return result

    def _rows(sql, params):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # 1. 列清单(column_type 自带长度/精度,如 decimal(10,2))
    for r in _rows(
        "SELECT table_name, column_name, UPPER(column_type), "
        "COALESCE(column_comment, '') "
        "FROM information_schema.columns cols "
        "JOIN information_schema.tables t ON t.table_schema = cols.table_schema "
        "AND t.table_name = cols.table_name AND t.table_type = 'BASE TABLE' "
        "WHERE cols.table_schema = %s "
        "ORDER BY table_name, ordinal_position", (database,)):
        table = r[0]
        result["tables"].setdefault(table, {"comment": "", "columns": []})
        result["tables"][table]["columns"].append(
            {"name": r[1], "data_type": r[2] or "", "comment": r[3] or ""})

    # 2. 表注释
    for r in _rows(
        "SELECT table_name, COALESCE(table_comment, '') "
        "FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE'", (database,)):
        if r[0] in result["tables"]:
            result["tables"][r[0]]["comment"] = r[1] or ""

    # 3. 主键
    for r in _rows(
        "SELECT table_name, column_name FROM information_schema.key_column_usage "
        "WHERE table_schema = %s AND constraint_name = 'PRIMARY'", (database,)):
        result["primary_keys"].add((r[0], r[1]))

    # 4. 外键(referenced_table_name 非空即 FK 行)
    for r in _rows(
        "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
        "FROM information_schema.key_column_usage "
        "WHERE table_schema = %s AND referenced_table_name IS NOT NULL", (database,)):
        result["foreign_keys"].append(
            {"table": r[0], "column": r[1], "ref_table": r[2], "ref_column": r[3]})
    return result


# ════════════════════════════════════════════════════════════════
# 结构组装(移植 semantic_scanner.scan_data_source/_scan_table)
# ════════════════════════════════════════════════════════════════

def _infer_semantic_type(col_name: str, data_type: str, is_primary_key: bool) -> str | None:
    """推断列的语义角色: measure/dimension/key(移植 _infer_semantic_type)。

    主键 → key;数值类型 → measure(纯 xxx_id 外键整型更像 key);
    其余 → dimension。
    """
    if is_primary_key:
        return "key"
    type_str = (data_type or "").upper()
    if any(t in type_str for t in ("DECIMAL", "NUMERIC", "FLOAT", "DOUBLE",
                                   "INT", "SERIAL", "MONEY")):
        # 纯外键整型更像 key 而非 measure
        if col_name.endswith("_id") and "SERIAL" not in type_str:
            return "key"
        return "measure"
    return "dimension"


def _build_content(introspected: dict) -> SemanticModelContent:
    """内省结果 → SemanticModelContent(移植 _scan_table 的列/表组装规则)。

    source/confidence 标注规则(SEM-001):
      有注释 → manual / 1.0;无注释退化列名 → auto_inferred / 0.5(待 LLM 富化)。
    """
    primary_keys = introspected.get("primary_keys") or set()
    models: list[Model] = []
    for table_name, tinfo in introspected["tables"].items():
        # 系统表排除(移植源 config.system_tables 机制: 扫描阶段即排除,
        # 平台表/系统表不得进入语义层——否则 LLM 精筛/图谱全被污染)。
        # chatbi_ 前缀 = pack 元数据表命名约定(chatbi_data_sources 等),
        # 业务库与平台同机时同样排除。
        if table_name in _SYSTEM_TABLES or table_name.startswith("chatbi_"):
            continue
        columns: list[Column] = []
        for c in tinfo["columns"]:
            comment = (c.get("comment") or "").strip()
            if comment:
                display_name, c_source, c_conf = comment, "manual", 1.0
            else:
                # 退化: 列名即展示名,等 LLM 富化阶段升级
                display_name, c_source, c_conf = c["name"], "auto_inferred", 0.5
            columns.append(Column(
                name=c["name"],
                display_name=display_name,
                data_type=c["data_type"],
                semantic_type=_infer_semantic_type(
                    c["name"], c["data_type"], (table_name, c["name"]) in primary_keys),
                description=comment or None,
                source=c_source,
                confidence=c_conf,
            ))

        # 表 display_name: 优先注释,否则表名
        t_comment = (tinfo.get("comment") or "").strip()
        if t_comment:
            t_display, t_source, t_conf = t_comment, "manual", 1.0
        else:
            t_display, t_source, t_conf = table_name, "auto_inferred", 0.5

        models.append(Model(
            name=table_name,
            display_name=t_display,
            description=t_comment or None,
            source=t_source,
            confidence=t_conf,
            columns=columns,
            relationships=[],   # 外键关系由 _apply_foreign_keys 补
            metrics=[],         # 指标留给 enrich_metrics(规则 + LLM 两阶段)
            calculated_fields=[],
        ))
    return SemanticModelContent(version=1, models=models)


def _apply_foreign_keys(content: SemanticModelContent, foreign_keys: list) -> None:
    """外键 → Relationship(source=foreign_key, confidence=1.0, 基数 N:1)。

    移植 _scan_relationships/_infer_cardinality;information_schema 每列一行,
    复合外键去重为一条关系(取首列,与源行为一致)。
    """
    by_name = {m.name: m for m in content.models}
    seen: set[tuple[str, str]] = set()   # (表, 被引用表) 复合外键去重
    for fk in foreign_keys:
        table, ref_table = fk["table"], fk["ref_table"]
        if not fk.get("column") or not ref_table or not fk.get("ref_column"):
            continue
        model = by_name.get(table)
        if model is None or (table, ref_table) in seen:
            continue
        seen.add((table, ref_table))
        model.relationships.append(Relationship(
            name=f"{table}_to_{ref_table}",
            target_model=ref_table,
            join_type="LEFT",
            on=f"{table}.{fk['column']} = {ref_table}.{fk['ref_column']}",
            type="N:1",   # 外键侧默认 N:1(多行指向被引用表主键)
            source="foreign_key",
            confidence=1.0,
        ))


# ════════════════════════════════════════════════════════════════
# LLM 适配(同步;chat 返回 (content, meta) 或 str 均兼容)
# ════════════════════════════════════════════════════════════════

def _chat_text(llm, *, messages: list, temperature=None, stage: str | None = None,
               conv_id: str | None = None) -> str:
    """调 llm.chat 并归一为文本。

    宿主 LLM 契约: .chat(messages, temperature, stage, conv_id) → (content, meta);
    个别模块(检索/图谱)按 str 消费 —— 这里两种形态都接受,避免形状耦合。
    """
    resp = llm.chat(messages=messages, temperature=temperature,
                    stage=stage, conv_id=conv_id)
    if isinstance(resp, (tuple, list)):
        return (resp[0] or "") if resp else ""
    return resp or ""


def _parse_json_response(content: str | None):
    """从 LLM 响应提取 JSON(剥洋葱;移植 app/core/llm_json.parse_json_response)。

    1. 直接 json.loads → 2. 去 markdown 包裹 → 3. 截 {..} → 4. 截 [..] → None。
    返回 None 而非抛异常:调用方都有降级策略。
    """
    if not content:
        return None
    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ════════════════════════════════════════════════════════════════
# LLM 中文富化(移植 data_sources._enrich_with_llm)
# ════════════════════════════════════════════════════════════════

def _enrich_with_llm(content: SemanticModelContent, llm,
                     conv_id: str | None = None,
                     deadline: float | None = None) -> None:
    """扫描后用 LLM 给无注释的列补中文 display_name(原地 patch)。

    移植要点(逐点保留):
      - 有注释的列不动(source=manual);只补 source=auto_inferred 且
        confidence=0.5(退化列名)的列 → 补完升 source=auto_inferred, confidence=0.8
      - 跳过系统表;全量优先(≤200 表一次发),超过分批每批 100 张
      - LLM 失败/返回非法 → 降级保持退化列名,不阻塞扫描
      - deadline(monotonic 时点)超时 → 剩余批次降级退化列名
        (对标源 asyncio.wait_for 600s/阶段;逐批检查而非硬中断——
        单批 LLM 请求由客户端自身超时兜底)
    """
    if llm is None:
        return
    # 筛出需要推断的业务表(跳过系统表 + 全有注释的表)
    tasks: list[tuple[Model, list[Column]]] = []
    for model in content.models:
        if model.name in _SYSTEM_TABLES:
            continue
        needs_infer = [c for c in model.columns
                       if c.source == "auto_inferred" and c.confidence == 0.5]
        if needs_infer:
            tasks.append((model, needs_infer))
    if not tasks:
        return

    # 全量优先: 200 表以内一次性发送; 超过则分批 100 表
    batch_size = 200 if len(tasks) <= 200 else 100
    import time as _time
    for batch_start in range(0, len(tasks), batch_size):
        if deadline is not None and _time.monotonic() > deadline:
            logger.warning("LLM 富化阶段超时, 剩余 %d 张表降级退化列名",
                           len(tasks) - batch_start)
            return
        batch = tasks[batch_start:batch_start + batch_size]
        tables_desc = []
        for model, needs_infer in batch:
            col_desc = ", ".join(f"{c.name}({c.data_type})" for c in needs_infer)
            tables_desc.append(f"表 {model.name}: {col_desc}")

        prompt = (
            "你是数据库语义推断助手。以下是多张表需要推断中文展示名的列。\n\n"
            + "\n".join(tables_desc)
            + "\n\n为每个列推断一个简洁的中文展示名（display_name）。"
              "只返回 JSON，格式: {\"表名\": {\"列名\": \"中文名\"}}，不要解释。"
        )
        try:
            # dict 形态响应走 chat_json(引擎自带 JSON 引导与容错解析);
            # 失败时再走 chat + 剥洋葱解析(与源实现 parse_json_response 等价)
            result = None
            try:
                result = llm.chat_json(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, stage="chatbi.semantic.enrich",
                    conv_id=conv_id)
            except Exception:
                result = None
            if not isinstance(result, dict):
                result = _parse_json_response(_chat_text(
                    llm, messages=[{"role": "user", "content": prompt}],
                    temperature=0.0, stage="chatbi.semantic.enrich",
                    conv_id=conv_id))
            if not isinstance(result, dict):
                logger.warning("_enrich_with_llm: LLM 返回非对象, 本批降级")
                continue
        except Exception as e:
            logger.warning("_enrich_with_llm: LLM 批量推断失败, 降级: %s", e)
            continue

        # patch 结果(只动本批的退化列)
        for model, needs_infer in batch:
            table_result = result.get(model.name, {})
            if not isinstance(table_result, dict):
                continue
            name_to_col = {c.name: c for c in needs_infer}
            for col_name, display_name in table_result.items():
                if col_name in name_to_col:
                    col = name_to_col[col_name]
                    col.display_name = str(display_name)
                    col.confidence = 0.8   # LLM 推断, 升级置信度


# ════════════════════════════════════════════════════════════════
# 指标推断(移植 semantic_scanner 的规则推断 + enrich_metrics)
# ════════════════════════════════════════════════════════════════

# 金额类列名关键词 (→ SUM + AVG); 正则单词边界匹配, 避免 discount 匹配 count
_AMOUNT_KEYWORDS = frozenset(("amount", "price", "fee", "cost", "revenue",
                              "salary", "income", "payment"))
# 数量类列名关键词 (→ SUM + COUNT)
_COUNT_KEYWORDS = frozenset(("count", "num", "qty", "quantity", "cnt", "number"))
# 金额类数据类型 (单词边界, 避免 INTERVAL 匹配 INT)
_AMOUNT_TYPES = frozenset(("DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "MONEY", "REAL"))
# 数量类数据类型
_COUNT_TYPES = frozenset(("INT", "BIGINT", "SERIAL", "SMALLINT", "INTEGER", "BIGSERIAL"))


def _type_matches(data_type: str, type_set: frozenset) -> bool:
    """data_type 是否命中类型集合(精确或带括号前缀, 移植同名单)。

    例: "INTEGER" 匹配 "INT"(子串语义由调用方保证), "DECIMAL(10,2)" 匹配 "DECIMAL"。
    """
    type_upper = (data_type or "").upper()
    for t in type_set:
        if type_upper == t or type_upper.startswith(t + "(") or type_upper.startswith(t + " "):
            return True
    return False


def _name_matches_keyword(name: str, keywords: frozenset) -> bool:
    """列名是否含关键词(单词边界正则, 移植同名单)。

    例: "order_count" 命中 "count"; "discount" 不命中 "count"。
    """
    name_lower = name.lower()
    for kw in keywords:
        if re.search(rf"(?:^|[_\b]){re.escape(kw)}(?:[_\b]|$)", name_lower):
            return True
    return False


def _infer_simple_metrics(model: Model) -> list[Metric]:
    """规则推断 simple 指标(零 LLM;移植 _infer_simple_metrics)。

    金额列(名含 amount/... + DECIMAL/...) → SUM + AVG;
    数量列(名含 count/... + INT/...) → SUM + COUNT;
    通用 measure → SUM;(列名, 聚合) 去重;source=rule_inferred。
    """
    metrics: list[Metric] = []
    seen: set[tuple[str, str]] = set()
    for col in model.columns:
        if col.semantic_type != "measure":
            continue
        col_name = col.name
        col_display = col.display_name or col_name
        type_upper = col.data_type or ""
        is_amount = (_name_matches_keyword(col_name, _AMOUNT_KEYWORDS)
                     and _type_matches(type_upper, _AMOUNT_TYPES))
        is_count = (_name_matches_keyword(col_name, _COUNT_KEYWORDS)
                    and _type_matches(type_upper, _COUNT_TYPES))

        # (列名, 函数, 展示模板) 组合;模板对齐源实现的 "合计/平均/计数" 后缀
        wanted: list[tuple[str, str, str]] = []
        if is_amount:
            wanted = [("SUM", "sum", "{c}合计"), ("AVG", "avg", "平均{c}")]
        elif is_count:
            wanted = [("SUM", "sum", "{c}合计"), ("COUNT", "count", "{c}计数")]
        else:
            wanted = [("SUM", "sum", "{c}合计")]
        for func, suffix, template in wanted:
            key = (col_name, func)
            if key in seen:
                continue
            seen.add(key)
            metrics.append(Metric(
                name=f"{col_name}_{suffix}",
                display_name=template.format(c=col_display),
                formula=f"{func}({col_name})",
                type="single",
                source="rule_inferred",
            ))
    return metrics


def _validate_llm_metrics(parsed: list, model: Model) -> list[Metric]:
    """LLM 指标逐条 Pydantic 校验 + composite 子指标交叉校验(移植,F4)。

    - 校验失败(缺字段/多字段/composite 缺 factor) → 丢弃该条,不阻塞
    - factor_metric_names 引用不存在的子指标 → 过滤;过滤后为空 → 整条丢弃
      (引用范围为"模型已有指标",即 composite 增量路径;
       LLM 全量推断回退路径的交叉校验在 _llm_infer_all 内联实现)
    返回通过校验的指标列表(可能为空)。
    """
    existing_names = {m.name for m in model.metrics}
    validated: list[Metric] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            m = Metric(**item)
        except Exception as e:
            logger.warning("enrich_metrics: 指标校验失败 (表=%s, 数据=%s): %s",
                           model.name, item, e)
            continue
        if m.type == "composite" and m.factor_metric_names:
            valid = [n for n in m.factor_metric_names if n in existing_names]
            invalid = [n for n in m.factor_metric_names if n not in existing_names]
            if invalid:
                logger.warning(
                    "enrich_metrics: composite 指标 %s 的 factor_metric_names "
                    "引用了不存在的子指标 %s, 已过滤 (表=%s)",
                    m.name, invalid, model.name)
            if not valid:
                logger.warning(
                    "enrich_metrics: composite 指标 %s 无有效 factor_metric_names, "
                    "丢弃 (表=%s)", m.name, model.name)
                continue
            m.factor_metric_names = valid
        validated.append(m)
    return validated


def _llm_infer_composite(model: Model, llm, conv_id: str | None) -> None:
    """LLM 推断 composite 指标(输入是已有的 simple 指标;移植 _infer_composite)。"""
    simple_desc = ", ".join(
        f"{m.name}({m.display_name}) = {m.formula}"
        + (f" WHERE {m.condition}" if m.condition else "")
        for m in model.metrics if m.type == "single"
    )
    if not simple_desc:
        return
    prompt = (
        f"你是 BI 业务指标推断助手。表名: {model.name}\n"
        f"已有简单指标: {simple_desc}\n\n"
        f"基于以上简单指标，推断复合业务指标。规则:\n"
        f"1. composite 指标: 由子指标组合，如 gmv / order_count，需填 factor_metric_names\n"
        f"2. 每个指标需有 name(英文标识), display_name(中文名), formula, type(composite)\n"
        f"3. factor_metric_names 必须指向已有简单指标的 name\n"
        f"4. 如有过滤条件填 condition\n"
        f"5. 只推断有业务含义的复合指标，不要凑数\n\n"
        f"只返回 JSON 数组，格式:\n"
        f'[{{"name": "avg_order_amount", "display_name": "客单价", "formula": "total_amount_sum / id_count", '
        f'"type": "composite", "factor_metric_names": ["total_amount_sum", "id_count"]}}]\n'
        f"不要解释，不要 markdown 包裹。"
    )
    try:
        result_text = _chat_text(
            llm, messages=[{"role": "user", "content": prompt}],
            temperature=0.0, stage="chatbi.semantic.metrics", conv_id=conv_id)
    except Exception as e:
        logger.warning("enrich_metrics: LLM 调用失败 (表=%s), 跳过: %s", model.name, e)
        return
    parsed = _parse_json_response(result_text)
    if not isinstance(parsed, list):
        logger.warning("enrich_metrics: LLM 返回非数组 (表=%s), 跳过", model.name)
        return
    composite = _validate_llm_metrics(parsed, model)
    if composite:
        model.metrics = list(model.metrics) + composite
        logger.info("enrich_metrics (LLM): 表 %s 推断出 %d 个 composite 指标",
                    model.name, len(composite))


def _llm_infer_all(model: Model, llm, conv_id: str | None) -> None:
    """LLM 推断所有指标(single + composite;规则推断关闭时的回退,移植 _infer_all)。"""
    measure_cols = [c for c in model.columns if c.semantic_type == "measure"]
    if not measure_cols:
        return
    col_desc = ", ".join(f"{c.name}({c.display_name}, {c.data_type})" for c in measure_cols)
    prompt = (
        f"你是 BI 业务指标推断助手。表名: {model.name}\n"
        f"度量列: {col_desc}\n\n"
        f"基于以上度量列，推断业务指标。规则:\n"
        f"1. single 指标: 单列聚合，如 SUM(col), AVG(col), COUNT(col)\n"
        f"2. composite 指标: 由子指标组合，如 gmv / order_count，需填 factor_metric_names\n"
        f"3. 每个指标需有 name(英文标识), display_name(中文名), formula, type(single/composite)\n"
        f"4. composite 指标的 factor_metric_names 必须指向已有指标的 name\n"
        f"5. 如有过滤条件填 condition\n"
        f"6. 只推断有业务含义的指标，不要凑数\n\n"
        f"只返回 JSON 数组，格式:\n"
        f'[{{"name": "gmv", "display_name": "成交总额", "formula": "SUM(total_amount)", "type": "single"}}, '
        f'{{"name": "avg_order_amount", "display_name": "客单价", "formula": "gmv / order_count", '
        f'"type": "composite", "factor_metric_names": ["gmv", "order_count"]}}]\n'
        f"不要解释，不要 markdown 包裹。"
    )
    try:
        result_text = _chat_text(
            llm, messages=[{"role": "user", "content": prompt}],
            temperature=0.0, stage="chatbi.semantic.metrics", conv_id=conv_id)
    except Exception as e:
        logger.warning("enrich_metrics: LLM 调用失败 (表=%s), 跳过: %s", model.name, e)
        return
    parsed = _parse_json_response(result_text)
    if not isinstance(parsed, list):
        logger.warning("enrich_metrics: LLM 返回非数组 (表=%s), 跳过", model.name)
        return
    # 本批全部过 Pydantic;composite 的子指标引用限定在本批 single 名单
    llm_metrics: list[Metric] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            llm_metrics.append(Metric(**item))
        except Exception as e:
            logger.warning("enrich_metrics: 指标校验失败 (表=%s, 数据=%s): %s",
                           model.name, item, e)
    single_names = {m.name for m in llm_metrics if m.type == "single"}
    validated: list[Metric] = []
    for m in llm_metrics:
        if m.type == "composite" and m.factor_metric_names:
            valid = [n for n in m.factor_metric_names if n in single_names]
            invalid = [n for n in m.factor_metric_names if n not in single_names]
            if invalid:
                logger.warning(
                    "enrich_metrics: composite 指标 %s 的 factor_metric_names "
                    "引用了不存在的子指标 %s, 已过滤 (表=%s)", m.name, invalid, model.name)
            if not valid:
                logger.warning(
                    "enrich_metrics: composite 指标 %s 无有效 factor_metric_names, "
                    "丢弃 (表=%s)", m.name, model.name)
                continue
            m.factor_metric_names = valid
        validated.append(m)
    if validated:
        model.metrics = validated
        logger.info("enrich_metrics (LLM 回退): 表 %s 推断出 %d 个指标",
                    model.name, len(validated))


def enrich_metrics(content: SemanticModelContent, llm,
                   rule_inference: bool = True,
                   conv_id: str | None = None,
                   deadline: float | None = None) -> None:
    """扫描后推断业务指标(原地 patch;移植 enrich_metrics,两阶段)。

    阶段 1: 规则推断 simple 指标(零 LLM;rule_inference 开启时),保留已有
            manual 指标,同名不覆盖。
    阶段 2: LLM 推断 —— 规则开启时只对有 2+ simple 指标的表推 composite;
            规则关闭时对所有有 measure 列的表全量推(single+composite 回退路径)。
    系统表跳过;LLM 失败/校验失败不阻塞(宁缺毋滥)。
    """
    # ── 阶段 1: 规则推断 simple 指标 ──────────────────────────
    if rule_inference:
        for model in content.models:
            if model.name in _SYSTEM_TABLES:
                continue
            simple_metrics = _infer_simple_metrics(model)
            if simple_metrics:
                existing_names = {m.name for m in model.metrics}
                for m in simple_metrics:
                    if m.name not in existing_names:
                        model.metrics.append(m)
                        existing_names.add(m.name)

    # ── 阶段 2: LLM 推断(同步顺序;源实现为信号量并发,等价改写) ──
    if llm is None:
        return
    if rule_inference:
        candidates = [m for m in content.models
                      if m.name not in _SYSTEM_TABLES and len(m.metrics) >= 2]
    else:
        candidates = [m for m in content.models
                      if m.name not in _SYSTEM_TABLES
                      and any(c.semantic_type == "measure" for c in m.columns)]
    import time as _time
    for idx, model in enumerate(candidates):
        if deadline is not None and _time.monotonic() > deadline:
            logger.warning("指标推断阶段超时, 剩余 %d 张表跳过 LLM 推断",
                           len(candidates) - idx)
            return
        if rule_inference:
            _llm_infer_composite(model, llm, conv_id)
        else:
            _llm_infer_all(model, llm, conv_id)


# ════════════════════════════════════════════════════════════════
# 示例问题(移植 ai/question_generator.py,降级链全保留)
# ════════════════════════════════════════════════════════════════

_QUESTION_PROMPT = """你是 BI 数据分析师。根据下面的数据库表结构, 生成 6-8 个用户最可能问的业务分析问题。

表结构:
{schema}

要求:
1. 问题要具体、可直接用 SQL 回答 (聚合/排名/占比/趋势)
2. 覆盖不同表和不同分析角度 (不要只问一张表)
3. 用自然语言, 不要出现 SQL/表名/列名, 用业务术语
4. 每行一个问题, 不要编号, 不要解释

示例格式:
本月各品类的销售额排名
消费金额最高的前10个用户
不同状态的订单数量占比
"""


def generate_sample_questions(models: list[Model], llm=None,
                              conv_id: str | None = None) -> list[str]:
    """基于语义层生成示例 BI 问题(LLM → 规则降级 → 硬编码兜底;fail-closed)。

    数据流: schema 摘要为空 → 直接规则降级;LLM 成功 → 解析 6-8 条;
    LLM 失败/空 → 规则降级;规则也为空 → 3 条硬编码通用问题。
    """
    schema_text = _build_schema_summary(models)
    if not schema_text:
        # schema 为空 (如无业务表) → 直接规则降级, 跳过 LLM 调用
        return _fallback_questions(models)
    prompt = _QUESTION_PROMPT.format(schema=schema_text)
    if llm is None:
        return _fallback_questions(models)
    try:
        content = _chat_text(
            llm,
            messages=[{"role": "system", "content": "你是 BI 分析专家, 生成用户最可能问的业务问题。"},
                      {"role": "user", "content": prompt}],
            temperature=0.3, stage="chatbi.semantic.sample_questions",
            conv_id=conv_id)
        questions = _parse_questions(content)
        if questions:
            logger.info("LLM 生成 %d 个示例问题", len(questions))
            return questions
        logger.warning("LLM 示例问题为空, 降级规则生成")
        return _fallback_questions(models)
    except Exception as e:
        # 异常兜底: 不抛到上层, 降级规则生成
        logger.warning("示例问题 LLM 生成失败, 降级规则生成: %s", e)
        return _fallback_questions(models)


def _build_schema_summary(models: list[Model]) -> str:
    """构建喂给 LLM 的表结构摘要(表名+中文名+关键列;移植同名单)。

    过滤系统表;每表取 measure 前 3 + dimension 前 4 + 关系,控制 prompt 长度。
    """
    if not models:
        return ""
    lines = []
    for m in models:
        if m.name in _SYSTEM_TABLES:
            continue
        measures = [c.display_name for c in m.columns if c.semantic_type == "measure"][:3]
        dimensions = [c.display_name for c in m.columns if c.semantic_type == "dimension"][:4]
        rels = [r.target_model for r in m.relationships]
        parts = [f"- {m.display_name}({m.name})"]
        if measures:
            parts.append(f"度量: {', '.join(measures)}")
        if dimensions:
            parts.append(f"维度: {', '.join(dimensions)}")
        if rels:
            parts.append(f"关联: {', '.join(rels)}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _parse_questions(text: str) -> list[str]:
    """从 LLM 输出解析问题列表(每行一个, 去编号/空行, 截前 8;移植)。"""
    questions = []
    for line in (text or "").strip().split("\n"):
        line = line.strip()
        # 去掉行首编号 (1. 2. - 等)
        line = re.sub(r"^[\d]+[.、)]\s*", "", line)
        line = re.sub(r"^[-*]\s*", "", line)
        if line and len(line) >= 4:
            questions.append(line)
    return questions[:8]


def _fallback_questions(models: list[Model]) -> list[str]:
    """规则降级: 基于列语义类型生成基础问题(移植;极端兜底硬编码 3 条)。"""
    questions: list[str] = []
    for m in models:
        if m.name in _SYSTEM_TABLES:
            continue
        measures = [c for c in m.columns if c.semantic_type == "measure"]
        dimensions = [c for c in m.columns if c.semantic_type == "dimension"]
        table_label = m.display_name or m.name
        if measures and dimensions:
            dim = dimensions[0].display_name
            mea = measures[0].display_name
            questions.append(f"{table_label}中各{dim}的{mea}排名")
            questions.append(f"{table_label}中{mea}最高的前10条记录")
        elif dimensions:
            dim = dimensions[0].display_name
            questions.append(f"{table_label}中各{dim}的数量统计")
        elif measures:
            mea = measures[0].display_name
            questions.append(f"{table_label}中{mea}的汇总统计")
        if len(questions) >= 6:
            break
    # 极端兜底: 一个规则问题都没生成 (无业务表)
    if not questions:
        questions = ["本月数据概览", "最近新增的记录", "各分类的数量统计"]
    return questions[:8]


# ════════════════════════════════════════════════════════════════
# 扫描流水线(编排源 data_sources._run_scan_background 的语义阶段)
# ════════════════════════════════════════════════════════════════

def _resolve_datasource_id(db, connect_info: dict) -> str | None:
    """按连接信息在注册表(chatbi_data_sources)匹配数据源 id(落库归属)。

    扫描契约的 connect_info 不含 datasource_id,而版本落库需要归属行;
    以 (db_type, host, port, database, username) 匹配,多行命中取最新创建的
    (并发重名注册属异常配置,告警日志提示)。未命中返回 None → 仅返回内容不落库。
    """
    db_type = (connect_info.get("db_type") or "postgresql").lower()
    username = connect_info.get("user") or connect_info.get("username") or ""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM chatbi_data_sources "
            "WHERE db_type = ? AND host = ? AND port = ? AND database = ? AND username = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (db_type, connect_info.get("host") or "", int(connect_info.get("port") or 5432),
             connect_info.get("database") or "", username)).fetchone()
    return row["id"] if row else None


def scan_datasource(llm, db, connect_info: dict, infer_metrics: bool = True,
                    progress_cb=None, datasource_id: str | None = None,
                    persist: bool = True
                    ) -> SemanticModelContent:
    """数据源扫描全流水线: 内省 → 外键 → LLM 富化 → 指标 → 示例问题 → 版本落库。

    Args:
        llm: LLM 客户端(.chat → (content, meta)/str, .chat_json → dict);None 时
             全部 LLM 环节降级(退化列名/仅规则指标/规则问题)。
        db: PackRelationalDB(chatbi)——注册表匹配与版本落库用。
        connect_info: {host, port, user, password, database, db_type}。
        infer_metrics: False 时跳过指标推断(映射源配置 scan_metric_inference)。
        progress_cb: progress_cb(pct: int, stage: str);异常只告警不阻塞
                     (移植 _update_scan 的"进度更新失败不抛"语义)。
        datasource_id: 直传数据源 id(同连接注册多行时反查可能归属错误)。
        persist: False 时只扫描不落库——刷新链路专用: 外部先 _merge_content
            保留标注再 save_content 一次落库。若内部也落, 裸结构中间版本
            会污染指纹基准(每周期净增 2 版本)且短暂成为 is_current。

    Returns:
        SemanticModelContent(若注册表能按连接信息匹配到数据源行,已落最新版本)。

    Raises:
        连接失败/内省失败向上抛(调用方 tasks.py 据此置 scan_status=failed)。

    阶段进度(对齐源扫描流水线):
      10 连接 → 20 表结构 → 30 外键 → 35 N 张表 → 45 LLM 中文名 →
      65 LLM 指标 → 80 示例问题 → 88 保存 → 95 完成
    """
    def progress(pct: int, stage: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(int(pct), stage)
        except Exception as e:
            logger.warning("进度回调失败(不阻塞扫描): %s", e)

    # ── Stage 1: 连库内省(10% → 35%) ──────────────────────────
    progress(10, "连接数据库...")
    conn = _connect_business(connect_info)
    try:
        progress(20, "扫描表结构...")
        db_type = (connect_info.get("db_type") or "postgresql").lower()
        if db_type == "postgresql":
            introspected = _pg_introspect(conn)
        else:
            introspected = _mysql_introspect(conn, connect_info.get("database") or "")
        content = _build_content(introspected)
        progress(30, "扫描外键关系...")
        _apply_foreign_keys(content, introspected.get("foreign_keys") or [])
        progress(35, f"扫描到 {len(content.models)} 张表")
    finally:
        conn.close()   # 扫描完即关(无引擎池,不占业务库连接)

    # ── LLM 阶段共享 deadline(对标源 600s/阶段兜底) ─────────────
    # 单请求超时(LLM_TIMEOUT, 默认 300s)只约束一次调用;逐表顺序的指标
    # 推断最坏 N×300s(100 表≈8h), 任务卡死在 65% 且被 dedupe 挡住。
    # deadline 到点 → 剩余表降级(退化列名/仅规则指标), 扫描仍能完成。
    import time as _time
    llm_deadline = (_time.monotonic() + 600
                    if llm is not None else None)

    # ── Stage 2: LLM 中文富化(45%) — 失败降级退化列名 ───────────
    progress(45, "LLM 推断中文名...")
    try:
        _enrich_with_llm(content, llm, deadline=llm_deadline)
    except Exception as e:
        logger.warning("LLM 推断失败, 退化列名: %s", e)

    # ── Stage 2b: 指标推断(65%) — 失败降级仅规则指标 ────────────
    progress(65, "LLM 推断业务指标...")
    if infer_metrics:
        try:
            enrich_metrics(content, llm, deadline=llm_deadline)
        except Exception as e:
            logger.warning("LLM 指标推断失败, 跳过: %s", e)

    # ── Stage 3: 示例问题(80%) — 降级链,失败留空不阻塞 ──────────
    progress(80, "生成示例问题...")
    try:
        content.sample_questions = generate_sample_questions(content.models, llm)
    except Exception:
        logger.warning("示例问题生成失败, 留空 (不阻塞扫描)", exc_info=True)
        content.sample_questions = []

    # ── Stage 4: 版本保存(88% → 95%;persist=False 跳过) ───────
    if not persist:
        progress(95, f"完成(未落库): {len(content.models)} 张表")
        return content
    progress(88, "保存语义层...")
    try:
        # datasource_id 直传优先(tasks 持有 payload 的 ds_id, 不靠连接信息反查——
        # 同连接注册多行时反查可能归属错误, 评审 I5)
        ds_id = datasource_id or _resolve_datasource_id(db, connect_info)
    except Exception as e:
        logger.warning("注册表匹配失败(不落库,仅返回内容): %s", e)
        ds_id = None
    if ds_id:
        # 图谱 LLM 关系推断 (对标 _run_scan_background 的 infer 阶段:
        # enrich 之后、save 之前, 保证一次落库即含推断关系;失败降级不阻塞)
        try:
            from domains.chatbi.graph_infer import infer_knowledge_graph
            progress(88, "图谱关系推断...")
            inferred = infer_knowledge_graph(content, use_llm=llm is not None, llm=llm)
            if inferred:
                _apply_inferred_relationships(content, inferred)
                logger.info("图谱推断新增 %d 条关系", len(inferred))
        except Exception as e:
            logger.warning("图谱推断失败(降级为外键+命名规则关系): %s", e)
        version = save_content(db, ds_id, content, source="scan")
        progress(95, f"完成: v{version}, {len(content.models)} 张表")
    else:
        logger.warning("scan_datasource: 注册表未匹配到数据源行, 语义层未落库")
        progress(95, f"完成(未落库): {len(content.models)} 张表")
    return content


# ════════════════════════════════════════════════════════════════
# 版本管理(chatbi_semantic_models;api.py 语义端点的读写面)
# ════════════════════════════════════════════════════════════════

def mark_manual_edits(old: SemanticModelContent | None,
                      new: SemanticModelContent) -> int:
    """人工校正打标(源 semantic_models.py:269-297 PATCH 的等价物, 前置版)。

    与当前版本 diff: 表/列的 display_name/description 被修改 →
    source=manual, confidence=1.0(原地改 new)。返回打标数量。

    为什么必要: _enrich_with_llm 只挑 source==auto_inferred 且
    confidence==0.5 的列补名——人工校正若不打标, 下次全量重扫时
    LLM 推断名会覆盖人工校正名(自动刷新路径有 _merge_content 保护,
    手动重扫没有)。
    """
    if old is None:
        return 0
    old_models = {m.name: m for m in old.models}
    marked = 0
    for new_m in new.models:
        old_m = old_models.get(new_m.name)
        if old_m is None:
            continue  # 新表: 无对比基准
        if (new_m.display_name != old_m.display_name
                or new_m.description != old_m.description):
            new_m.source = "manual"
            new_m.confidence = 1.0
            marked += 1
        old_cols = {c.name: c for c in old_m.columns}
        for new_c in new_m.columns:
            old_c = old_cols.get(new_c.name)
            if old_c is None:
                continue  # 新列
            if (new_c.display_name != old_c.display_name
                    or new_c.description != old_c.description):
                new_c.source = "manual"
                new_c.confidence = 1.0
                marked += 1
    return marked


def save_content(db, datasource_id: str, content: SemanticModelContent,
                 source: str = "manual", expected_version: int | None = None) -> int:
    """落新版本 + is_current 翻转,返回版本号(append-only)。

    - 版本号 = 该数据源当前最大版本 + 1,单调递增
    - 旧 is_current 全部置 0,新版本置 1(同一事务内完成,崩溃不留双 current)
    - 内容指纹与当前最新版本完全一致 → 幂等返回原版本号,不产空版本
      (tasks.py 的自动刷新依赖此语义: 内容没变不落新版本)
    - source 仅记日志(DDL 无来源列;语义审计由调用方任务层负责)
    """
    payload = json.dumps(content.model_dump(), ensure_ascii=False)
    fingerprint = json.dumps(content.model_dump(), ensure_ascii=False, sort_keys=True)
    with db.connect() as conn:
        # 九审 7.2: 悲观锁 + 版本前置条件——多管理员/并发写不允许旧快照静默覆盖
        try:
            conn.execute(
                "SELECT id FROM chatbi_data_sources WHERE id = ? FOR UPDATE",
                (datasource_id,))
        except Exception:
            pass
        row = conn.execute(
            "SELECT version, content FROM chatbi_semantic_models "
            "WHERE data_source_id = ? ORDER BY version DESC LIMIT 1",
            (datasource_id,)).fetchone()
        # 获锁后校验 expected_version(九审 7.2: 旧快照提交 → 领域异常而非静默覆盖)
        if expected_version is not None and row:
            if int(row["version"]) != expected_version:
                from domains.chatbi.graph_infer import VersionConflictError
                raise VersionConflictError(
                    expected_version=expected_version,
                    current_version=int(row["version"]),
                    pending_updates={},)
        if row:
            latest_version = int(row["version"])
            try:
                latest_fp = json.dumps(json.loads(row["content"]),
                                       ensure_ascii=False, sort_keys=True)
            except Exception:
                latest_fp = None   # 存量脏数据: 无法解析则视为有变化,正常落新版本
            if latest_fp == fingerprint:
                logger.info("save_content: 内容未变化 (数据源=%s), 幂等返回 v%d",
                            datasource_id, latest_version)
                return latest_version
            # is_current 翻转(同事务)
            conn.execute(
                "UPDATE chatbi_semantic_models SET is_current = 0 "
                "WHERE data_source_id = ? AND is_current = 1", (datasource_id,))
        else:
            latest_version = 0
        new_version = latest_version + 1
        conn.execute(
            "INSERT INTO chatbi_semantic_models "
            "(id, data_source_id, version, content, is_current, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), datasource_id, new_version, payload, _now()))
    logger.info("save_content(来源=%s): 数据源=%s 落版本 v%d (%d 张表)",
                source, datasource_id, new_version, len(content.models))
    return new_version


def load_content(db, datasource_id: str,
                 version: int | None = None) -> tuple[SemanticModelContent | None, int | None]:
    """按版本读语义层;version=None 读当前版本。缺失返回 (None, None)。"""
    with db.connect() as conn:
        if version is None:
            row = conn.execute(
                "SELECT version, content FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND is_current = 1 "
                "ORDER BY version DESC LIMIT 1", (datasource_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT version, content FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND version = ?",
                (datasource_id, version)).fetchone()
    if not row:
        return None, None
    try:
        content = SemanticModelContent.model_validate(json.loads(row["content"]))
    except Exception as e:
        logger.warning("load_content: 语义层 JSON 解析失败 (数据源=%s v%s): %s",
                       datasource_id, row["version"], e)
        return None, None
    return content, int(row["version"])


def load_current_content(db, datasource_id: str) -> SemanticModelContent | None:
    """当前版本语义层(is_current=1);未扫描返回 None(检索栈 fail-closed 依赖)。"""
    content, _ = load_content(db, datasource_id, None)
    return content


def rollback(db, datasource_id: str, version: int,
             expected_current: int | None = None) -> tuple:
    """回滚 = 把目标版本内容复制成新版本并置 current(append-only,不改历史)。

    移植 semantic_models.py rollback_to_version 语义;版本不存在抛 ValueError
    (api.py 捕获后回 404)。返回 (new_version, content)——回滚后索引重建需要
    回滚到的内容(与源 rollback 后 rebuild 的编排对齐)。
    """
    content, _ = load_content(db, datasource_id, version)
    if content is None:
        raise ValueError(f"版本 {version} 不存在")
    # 十三审 7.5: 用客户端传入的 expected_current(识别旧页面意图)
    _, cur_v = load_content(db, datasource_id)
    effective_expected = expected_current if expected_current is not None else cur_v
    new_version = save_content(db, datasource_id, content, source="rollback",
                               expected_version=effective_expected)
    logger.info("rollback: 数据源=%s 从 v%d 复制落新版本 v%d",
                datasource_id, version, new_version)
    return new_version, content


def delete_by_datasource(db, datasource_id: str) -> int:
    """删除该数据源的全部语义版本(数据源删除时级联调用),返回删除行数。"""
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_semantic_models WHERE data_source_id = ?",
            (datasource_id,))
        return cur.rowcount


# ════════════════════════════════════════════════════════════════
# 语义层 diff(纯函数;移植 semantic_diff.py 全文)
# ════════════════════════════════════════════════════════════════

# 对比的列属性: 只关注语义相关的属性变化
# data_type: 数据库类型变化 (如 VARCHAR→TEXT) 影响 SQL 生成
# semantic_type: 语义类型变化 (如 普通列→度量) 影响 BI 分析
# 列级 diff 参与属性: 结构(data_type) + 语义(semantic_type) + 人工标注
# (display_name)。display_name 原版不参与——但版本对比 UI 需要展示
# "中文名被改过"的表(否则人工校正后 diff 显示无差异, 用户困惑)。
_COL_DIFF_ATTRS = ("data_type", "semantic_type", "display_name")


def diff_semantic_contents(old: dict | None, new: dict | None) -> dict:
    """对比两个语义层 content 的差异(纯函数,不依赖 DB;移植)。

    表级: 新增/删除/变更表;列级: 变更表里的增/删/改列(data_type/semantic_type)。
    display_name/description 是人工/LLM 标注,不参与列级 diff(不代表结构变化),
    但表级顶层字段变化也算变更。排序输出保证结果稳定。
    """
    old_models = {m["name"]: m for m in (old or {}).get("models", [])}
    new_models = {m["name"]: m for m in (new or {}).get("models", [])}

    # 集合运算: 新增 = new - old, 删除 = old - new, 共同 = old & new
    added = sorted(set(new_models) - set(old_models))
    removed = sorted(set(old_models) - set(new_models))
    common = sorted(set(old_models) & set(new_models))

    changed_models = []
    unchanged = []
    for name in common:
        col_diff = _diff_columns(old_models[name], new_models[name])
        # 整表顶层字段(display_name/description/source/confidence)变化也算变更
        top_changed = any(
            old_models[name].get(k) != new_models[name].get(k)
            for k in ("display_name", "description", "source", "confidence")
        )
        if col_diff["has_column_changes"] or top_changed:
            changed_models.append({"table": name, **col_diff})
        else:
            unchanged.append(name)

    # has_changes 是元数据刷新判断是否写新版本的依据
    has_changes = bool(added or removed or changed_models)
    return {
        "added_models": added,
        "removed_models": removed,
        "changed_models": changed_models,
        "unchanged_models": unchanged,
        "has_changes": has_changes,
    }


def _diff_columns(old_model: dict, new_model: dict) -> dict:
    """对比单个表的列差异(增删 + data_type/semantic_type 变更;移植)。

    列名精确匹配(不忽略大小写);列顺序变化不影响 diff。
    """
    old_cols = {c["name"]: c for c in old_model.get("columns", [])}
    new_cols = {c["name"]: c for c in new_model.get("columns", [])}

    added_columns = sorted(set(new_cols) - set(old_cols))
    removed_columns = sorted(set(old_cols) - set(new_cols))

    # 同名列的关键属性变更 (类型/语义类型); 一个属性变化就标记
    common_cols = set(old_cols) & set(new_cols)
    changed_columns = []
    for col_name in common_cols:
        for attr in _COL_DIFF_ATTRS:
            if old_cols[col_name].get(attr) != new_cols[col_name].get(attr):
                changed_columns.append(col_name)
                break
    return {
        "added_columns": added_columns,
        "removed_columns": removed_columns,
        "changed_columns": sorted(set(changed_columns)),   # 去重 + 排序
        "has_column_changes": bool(added_columns or removed_columns or changed_columns),
    }


def is_empty_diff(diff_result: dict) -> bool:
    """便捷判断: diff 结果是否无任何变化(元数据刷新用;移植)。"""
    return not diff_result.get("has_changes", False)


def diff_versions(db, datasource_id: str, from_version: int, to_version: int) -> dict:
    """对比两个已落库版本的 content 差异(移植 semantic_models.py diff 端点)。

    返回 diff_semantic_contents 结果并附 from_version/to_version;
    版本缺失抛 ValueError(api.py 捕获后回 404)。
    """
    old_content, _ = load_content(db, datasource_id, from_version)
    new_content, _ = load_content(db, datasource_id, to_version)
    if old_content is None:
        raise ValueError(f"版本 {from_version} 不存在")
    if new_content is None:
        raise ValueError(f"版本 {to_version} 不存在")
    result = diff_semantic_contents(old_content.model_dump(), new_content.model_dump())
    result["from_version"] = from_version
    result["to_version"] = to_version
    return result


# ════════════════════════════════════════════════════════════════
# 注入防御(F9;移植 semantic_layer._METRIC_DANGEROUS/_DDL_DML_KEYWORDS)
# ════════════════════════════════════════════════════════════════

def validate_metric_formula(formula: str) -> list[str]:
    """指标公式注入防御校验,返回错误列表(空列表 = 通过)。

    - _METRIC_DANGEROUS: 分号/SQL 注释符(; -- /* */) — 防多语句与注释截断
    - _DDL_DML_KEYWORDS: DDL/DML 关键词(DROP/TRUNCATE/ALTER/EXEC/INSERT/DELETE/UPDATE)
    正则与 chat-bi schemas/semantic_layer.py 逐字符一致(models.py 已持有同名
    常量,此处直接复用保证单一事实源)。API 层对公式与 condition 均调用本函数。
    """
    errors: list[str] = []
    if _METRIC_DANGEROUS.search(formula or ""):
        errors.append("公式含非法字符(分号/SQL注释符)")
    if _DDL_DML_KEYWORDS.search(formula or ""):
        errors.append("公式含 DDL/DML 关键词")
    return errors


def _apply_inferred_relationships(content, inferred) -> None:
    """把图谱推断的关系建议写回 content(去重: 已有同表对关系不覆盖)。

    分组键 = rel.name 拆出的来源表(源语义: Relationship.name 格式
    "<from>_to_<to>", 见 graph_infer 产出与源 data_sources.py:525 的
    from_table = rel.name.split("_to_")[0])——按 rel.name 直接分组
    永远匹配不到表名, 推断关系会全部丢失。"""
    by_model: dict = {}
    for rel in inferred:
        from_table = rel.name.split("_to_")[0]
        by_model.setdefault(from_table, []).append(rel)
    for model in content.models:
        existing_pairs = {(r.name, r.target_model) for r in model.relationships}
        for rel in by_model.get(model.name, []):
            if (rel.name, rel.target_model) not in existing_pairs:
                model.relationships.append(rel)
