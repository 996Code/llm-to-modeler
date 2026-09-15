"""M4: 保存查询 + 看板 —— 移植自原系统 saved_queries.py + dashboard.py。

原系统对标:
  - SavedQuery: 成功查询自动保存 + 列表/详情/CSV 导出
  - Dashboard: CRUD + Widget 添加/删除/布局/实时刷新
  - 实时查询模式: 不存结果快照, refresh 重跑 SQL + 缓存 chart_config 注入

pack 适配:
  - tenant 删除(user_id 归属); SQLAlchemy → PackRelationalDB
  - asyncio → 同步; JWT → user_required(用户级, 信 X-User-Id 行级隔离)
  - execute_sql → datasources.execute_readonly
  - generate_chart → chart_engine.generate_chart(同步)
  - CSV 注入防护 + utf-8-sig BOM(Excel 中文)保留
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from sdk.pack_api import user_required
from sdk.relational_store import PackRelationalDB

from domains.chatbi import datasources
from domains.chatbi.models import M4_DDL
from domains.chatbi.runtime import get_pack_db
from domains.chatbi.security.sql_validator import validate_sql

logger = logging.getLogger(__name__)

router = APIRouter()

# CSV 导出上限(防 OOM; 对标原系统 _CSV_EXPORT_MAX_ROWS)
_CSV_EXPORT_MAX_ROWS = 50000


def _db() -> PackRelationalDB:
    db = get_pack_db()
    db.init_schema(M4_DDL)
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_id(request: Request) -> str:
    return request.headers.get("X-User-Id", "anonymous")


# ── Pydantic 请求体 ──────────────────────────────────────────

class DashboardCreate(BaseModel):
    name: str

    def model_post_init(self, __context) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("看板名称不能为空")


class WidgetCreate(BaseModel):
    question: str
    query_sql: str
    datasource_id: str
    chart_type: str = "table"
    position_x: int | None = None
    position_y: int | None = None
    width: int = 6
    height: int = 4


class LayoutItem(BaseModel):
    id: str
    position_x: int
    position_y: int
    width: int = 6
    height: int = 4


class LayoutUpdate(BaseModel):
    layout: list[LayoutItem]


# ── 保存查询(自动落库由 ask_data finalize 调用) ───────────────

def save_query(db: PackRelationalDB, user_id: str, data_source_id: str,
               question: str, sql: str, conversation_id: str | None = None,
               chart_config: dict | None = None, result_summary: dict | None = None) -> dict:
    """成功查询后自动保存(哈希去重: 同 user+ds+sql_hash 唯一)。"""
    import json as _json
    import hashlib as _hash
    sql_hash = _hash.md5(sql.encode()).hexdigest()
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM chatbi_saved_queries "
            "WHERE user_id = ? AND data_source_id = ? AND sql_hash = ? LIMIT 1",
            (user_id, data_source_id, sql_hash)).fetchone()
        if existing:
            return {"id": existing["id"], "deduplicated": True}
        qid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO chatbi_saved_queries
               (id, user_id, data_source_id, conversation_id, question, sql_text,
                sql_hash, result_summary, chart_config, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (qid, user_id, data_source_id, conversation_id, question, sql,
             sql_hash,
             _json.dumps(result_summary) if result_summary else None,
             _json.dumps(chart_config) if chart_config else None, _now()))
    return {"id": qid, "deduplicated": False}


# ── 保存查询 API ─────────────────────────────────────────────

@router.get("/saved-queries", dependencies=[Depends(user_required)])
async def list_saved_queries(request: Request, limit: int = Query(50, ge=1, le=200)):
    uid = _user_id(request)
    with _db().connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chatbi_saved_queries WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (uid, min(limit, 200))).fetchall()
    import json as _json
    return {"items": [{
        "id": r["id"], "dataSourceId": r["data_source_id"],
        "conversationId": r["conversation_id"], "question": r["question"],
        "sqlText": r["sql_text"],
        "chartConfig": _safe_json(r["chart_config"]),
        "createdAt": r["created_at"],
    } for r in rows]}


@router.get("/saved-queries/{sq_id}/run", dependencies=[Depends(user_required)])
async def run_saved_query(sq_id: str, request: Request):
    """重跑保存查询返回 JSON 结果(前端导出 Excel 用:数据 sheet 的数据源)。

    与 export CSV 同一套安全闸(三层校验 + 只读执行), 只是返回 JSON
    而非 CSV——前端 exceljs 生成 .xlsx(数据 sheet + 图表 sheet)。
    """
    uid = _user_id(request)
    db = _db()
    with db.connect() as conn:
        q = conn.execute(
            "SELECT * FROM chatbi_saved_queries WHERE id = ? AND user_id = ?",
            (sq_id, uid)).fetchone()
    if not q:
        raise HTTPException(404, "查询记录不存在")

    from domains.chatbi import semantic as _sem
    from domains.chatbi.retrieval import extract_allowed_columns
    _content = _sem.load_current_content(db, q["data_source_id"])
    if _content is None:
        raise HTTPException(422, "该数据源无语义层, 无法校验列白名单, 拒绝执行(fail-closed)")
    _allowed = extract_allowed_columns(_content)
    validation = validate_sql(q["sql_text"], allowed_columns=_allowed)
    if not validation.ok:
        raise HTTPException(422, f"SQL 校验失败: {validation.reason}")

    ds = datasources.get_datasource(db, q["data_source_id"], decrypt=True)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if not ds.is_active:
        raise HTTPException(403, "数据源已禁用")
    result = datasources.execute_readonly(ds, q["sql_text"], max_rows=_CSV_EXPORT_MAX_ROWS)
    if not result.ok:
        raise HTTPException(500, f"查询执行失败: {result.error}")

    return {
        "question": q["question"], "sql": q["sql_text"],
        "columns": result.columns,
        "rows": [list(r) for r in result.rows],
        "rowCount": result.rowcount,
        "truncated": bool(result.truncated),
        "maxRows": _CSV_EXPORT_MAX_ROWS,
    }


@router.get("/saved-queries/{sq_id}/export", dependencies=[Depends(user_required)])
async def export_saved_query_csv(sq_id: str, request: Request):
    """导出为 CSV(重跑 SQL + 三层校验 + CSV 注入防护 + utf-8-sig BOM)。"""
    uid = _user_id(request)
    db = _db()
    with db.connect() as conn:
        q = conn.execute(
            "SELECT * FROM chatbi_saved_queries WHERE id = ? AND user_id = ?",
            (sq_id, uid)).fetchone()
    if not q:
        raise HTTPException(404, "查询记录不存在")

    # SEC: 三层校验(不信任历史 SQL)—— Layer3 用语义层白名单(对标原系统 B3)
    from domains.chatbi import semantic as _sem
    from domains.chatbi.retrieval import extract_allowed_columns
    _content = _sem.load_current_content(db, q["data_source_id"])
    if _content is None:
        raise HTTPException(422, "该数据源无语义层, 无法校验列白名单, 拒绝导出(fail-closed)")
    _allowed = extract_allowed_columns(_content)
    validation = validate_sql(q["sql_text"], allowed_columns=_allowed)
    if not validation.ok:
        raise HTTPException(422, f"SQL 校验失败: {validation.reason}")

    # 重跑 SQL
    ds = datasources.get_datasource(db, q["data_source_id"], decrypt=True)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    if not ds.is_active:
        raise HTTPException(403, "数据源已禁用, 无法导出")
    result = datasources.execute_readonly(ds, q["sql_text"], max_rows=_CSV_EXPORT_MAX_ROWS)
    if not result.ok:
        raise HTTPException(500, f"查询执行失败: {result.error}")

    # CSV 生成(注入防护 + BOM)
    output = io.StringIO()
    writer = csv.writer(output)
    if result.truncated:
        writer.writerow([f"# 提示: 超过 {_CSV_EXPORT_MAX_ROWS} 行, 仅导出前 {_CSV_EXPORT_MAX_ROWS} 行"])
    writer.writerow(result.columns)
    for row in result.rows:  # execute_readonly 已截断到 max_rows
        writer.writerow([_sanitize_csv_cell(c) for c in row])

    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="query-{sq_id[:8]}.csv"'},
    )


def _sanitize_csv_cell(value) -> str:
    """CSV 注入防护: = + - @ 开头加 ' 前缀(OWASP); None → 空。"""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


# ── 看板 CRUD ────────────────────────────────────────────────

@router.get("/dashboards", dependencies=[Depends(user_required)])
async def list_dashboards(request: Request):
    uid = _user_id(request)
    with _db().connect() as conn:
        rows = conn.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM chatbi_dashboard_widgets w "
            "WHERE w.dashboard_id = d.id) AS widget_count "
            "FROM chatbi_dashboards d WHERE d.user_id = ? ORDER BY d.updated_at DESC",
            (uid,)).fetchall()
    return {"items": [{
        "id": r["id"], "name": r["name"], "widgetCount": r["widget_count"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    } for r in rows]}


@router.post("/dashboards", dependencies=[Depends(user_required)])
async def create_dashboard(body: DashboardCreate, request: Request):
    uid = _user_id(request)
    did = str(uuid.uuid4())
    now = _now()
    with _db().connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_dashboards (id, user_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (did, uid, body.name, now, now))
    return {"id": did, "name": body.name, "createdAt": now}


@router.put("/dashboards/{did}", dependencies=[Depends(user_required)])
async def update_dashboard(did: str, body: DashboardCreate, request: Request):
    uid = _user_id(request)
    with _db().connect() as conn:
        cur = conn.execute(
            "UPDATE chatbi_dashboards SET name = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (body.name, _now(), did, uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "看板不存在")
    return {"ok": True}


@router.delete("/dashboards/{did}", dependencies=[Depends(user_required)])
async def delete_dashboard(did: str, request: Request):
    uid = _user_id(request)
    with _db().connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_dashboards WHERE id = ? AND user_id = ?", (did, uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "看板不存在")
        conn.execute("DELETE FROM chatbi_dashboard_widgets WHERE dashboard_id = ?", (did,))
    return {"ok": True}


@router.get("/dashboards/{did}", dependencies=[Depends(user_required)])
async def get_dashboard(did: str, request: Request):
    uid = _user_id(request)
    with _db().connect() as conn:
        dash = conn.execute(
            "SELECT * FROM chatbi_dashboards WHERE id = ? AND user_id = ?",
            (did, uid)).fetchone()
        if not dash:
            raise HTTPException(404, "看板不存在")
        widgets = conn.execute(
            "SELECT * FROM chatbi_dashboard_widgets WHERE dashboard_id = ? "
            "ORDER BY position_y, position_x", (did,)).fetchall()
    import json as _json
    return {
        "id": dash["id"], "name": dash["name"],
        "createdAt": dash["created_at"], "updatedAt": dash["updated_at"],
        "widgets": [{
            "id": w["id"], "question": w["question"], "querySql": w["query_sql"],
            "datasourceId": w["datasource_id"], "chartType": w["chart_type"],
            "chartOption": _safe_json(w["chart_option"]),
            "positionX": w["position_x"], "positionY": w["position_y"],
            "width": w["width"], "height": w["height"],
        } for w in widgets],
    }


# ── Widget 操作 ──────────────────────────────────────────────

@router.post("/dashboards/{did}/widgets", dependencies=[Depends(user_required)])
async def add_widget(did: str, body: WidgetCreate, request: Request):
    """添加 widget(毫秒级):只存 SQL + 图表类型, 不跑查询不调 LLM。

    图表配置在首次 refresh 时按需生成并缓存(refresh 端点已有该逻辑)。
    此前保存时同步跑 SQL + LLM 生成图表配置, 电商库等大表一次要等
    十几秒——"添加到看板超级慢"的根因。保存与查询解耦后, 添加即返回,
    打开看板/点刷新才真正执行(与原版 ChatBI 的行为一致)。
    """
    uid = _user_id(request)
    db = _db()
    with db.connect() as conn:
        dash = conn.execute(
            "SELECT id FROM chatbi_dashboards WHERE id = ? AND user_id = ?",
            (did, uid)).fetchone()
        if not dash:
            raise HTTPException(404, "看板不存在")

    # SEC: SQL 校验(只校验不执行)
    validation = validate_sql(body.query_sql, allowed_columns=set())
    if not validation.ok:
        raise HTTPException(400, f"SQL 校验失败: {validation.reason}")

    # 自动布局: 按行填充(每行 2 个, 列宽 6 总宽 12)
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT position_x, position_y, width FROM chatbi_dashboard_widgets "
            "WHERE dashboard_id = ? ORDER BY position_y, position_x", (did,)).fetchall()
    auto_x, auto_y = _calc_next_position([(r["position_x"], r["position_y"], r["width"]) for r in existing])

    import json as _json
    wid = str(uuid.uuid4())
    now = _now()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO chatbi_dashboard_widgets
               (id, dashboard_id, question, query_sql, datasource_id, chart_type,
                chart_option, position_x, position_y, width, height, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (wid, did, body.question, body.query_sql, body.datasource_id,
             body.chart_type, None,
             body.position_x if body.position_x is not None else auto_x,
             body.position_y if body.position_y is not None else auto_y,
             body.width, body.height, now, now))
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (now, did))
    return {"id": wid, "chartConfig": None}


@router.delete("/dashboards/{did}/widgets/{wid}", dependencies=[Depends(user_required)])
async def delete_widget(did: str, wid: str, request: Request):
    uid = _user_id(request)
    db = _db()
    _require_dashboard(db, did, uid)
    with db.connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_dashboard_widgets WHERE id = ? AND dashboard_id = ?",
            (wid, did))
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (_now(), did))
    return {"ok": True}


@router.put("/dashboards/{did}/widgets/layout", dependencies=[Depends(user_required)])
async def update_layout(did: str, body: LayoutUpdate, request: Request):
    """批量更新 widget 布局(拖拽后保存)。"""
    uid = _user_id(request)
    db = _db()
    _require_dashboard(db, did, uid)
    updated = 0
    with db.connect() as conn:
        for item in body.layout:
            cur = conn.execute(
                "UPDATE chatbi_dashboard_widgets "
                "SET position_x = ?, position_y = ?, width = ?, height = ?, updated_at = ? "
                "WHERE id = ? AND dashboard_id = ?",
                (item.position_x, item.position_y, item.width, item.height, _now(), item.id, did))
            updated += cur.rowcount
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (_now(), did))
    return {"ok": True, "updated": updated}


@router.put("/dashboards/{did}/widgets/{wid}/refresh", dependencies=[Depends(user_required)])
async def refresh_widget(did: str, wid: str, request: Request):
    """刷新 widget: 实时重跑 SQL + 缓存 chart_config inject_data(不调 LLM)。"""
    db = _db()
    _require_dashboard(db, did, _user_id(request))
    with db.connect() as conn:
        w = conn.execute(
            "SELECT * FROM chatbi_dashboard_widgets WHERE id = ? AND dashboard_id = ?",
            (wid, did)).fetchone()
    if not w:
        raise HTTPException(404, "Widget 不存在")
    if not w["query_sql"]:
        raise HTTPException(400, "该组件无保存的 SQL, 无法刷新")

    # SEC: 重校验
    validation = validate_sql(w["query_sql"], allowed_columns=set())
    if not validation.ok:
        raise HTTPException(400, f"SQL 校验失败: {validation.reason}")

    ds = datasources.get_datasource(db, w["datasource_id"], decrypt=True)
    if not ds or not ds.is_active:
        raise HTTPException(403, "数据源不存在或已禁用")
    result = datasources.execute_readonly(ds, w["query_sql"])
    if not result.ok:
        raise HTTPException(500, f"查询执行失败: {result.error}")

    # 图表: 缓存 config + 实时数据注入(毫秒级, 不调 LLM)
    import json as _json
    chart_option = None
    if w["chart_option"]:
        try:
            from domains.chatbi.chart_engine import inject_data
            chart_option = inject_data(
                _safe_json(w["chart_option"]) or {}, result.columns,
                [tuple(r) for r in result.rows])
        except Exception as e:
            logger.warning("看板图表缓存注入失败: %s", e)
    if chart_option is None:
        from domains.chatbi.chart_engine import infer_chart_by_rule
        chart_option = infer_chart_by_rule(result.columns, [tuple(r) for r in result.rows])

    # 缓存回写:首次 refresh 时 chart_option 为空, 把规则推断的列映射
    # config 落库, 下次刷新走 inject_data 快路径(不再依赖规则推断)
    if not w["chart_option"] and chart_option:
        try:
            cfg = _extract_chart_config(w["chart_type"], result.columns,
                                        [tuple(r) for r in result.rows])
            if cfg:
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE chatbi_dashboard_widgets SET chart_option = ?, "
                        "updated_at = ? WHERE id = ?",
                        (_json.dumps(cfg), _now(), wid))
        except Exception as e:
            logger.warning("看板图表配置缓存回写失败(不影响返回): %s", e)

    return {
        "id": w["id"], "question": w["question"], "querySql": w["query_sql"],
        "datasourceId": w["datasource_id"],
        "columns": result.columns, "rows": [list(r) for r in result.rows[:200]],
        "rowCount": result.rowcount, "chartOption": chart_option,
    }


def _extract_chart_config(chart_type: str | None, columns: list, rows: list) -> dict | None:
    """从查询结果提取图表列映射 config(dim_col/measure_cols)。

    refresh 缓存回写用——存列映射而非完整 option, 下次刷新经 inject_data
    注入实时数据。维度列取第一个文本列, 度量列取全部数值列(与
    infer_chart_by_rule 的规则一致, 不调 LLM)。
    """
    if not columns or not rows:
        return None
    ctype = chart_type or "bar"
    dim_col = None
    measure_cols = []
    for i, c in enumerate(columns):
        sample = rows[0][i] if rows and len(rows[0]) > i else None
        if isinstance(sample, (int, float)) and not isinstance(sample, bool):
            measure_cols.append(c)
        elif dim_col is None:
            dim_col = c
    if ctype == "kpi":
        return {"chart_type": "kpi", "measure_cols": measure_cols[:1]}
    if ctype == "table":
        return {"chart_type": "table"}
    if not dim_col or not measure_cols:
        return None
    return {"chart_type": ctype, "dim_col": dim_col, "measure_cols": measure_cols}


def _safe_json(text: str | None) -> dict | None:
    """JSON 反序列化容错: 损坏数据返回 None(不 500 整个列表)。"""
    if not text:
        return None
    try:
        import json as _j
        v = _j.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _require_dashboard(db: PackRelationalDB, did: str, uid: str) -> None:
    """归属校验: 看板必须属于 uid, 否则 404(不区分不存在/无权)。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM chatbi_dashboards WHERE id = ? AND user_id = ?",
            (did, uid)).fetchone()
    if not row:
        raise HTTPException(404, "看板不存在")


def _calc_next_position(existing: list, width: int = 6, total_cols: int = 12) -> tuple:
    """自动布局: 按行扫描占列, 找第一个能放下 width 列的空位(对标原系统)。

    existing: [(position_x, position_y, widget_width), ...]
    """
    if not existing:
        return 0, 0
    # 占用矩阵: occupied[y][x] = True
    max_y = max((p[1] for p in existing), default=0)
    occupied = set()
    for px, py, pw in existing:
        for dx in range(max(pw, 1)):
            occupied.add((px + dx, py))
    # 按行扫描找第一个能放下的位置
    for y in range(max_y + 2):
        for x in range(0, total_cols - width + 1):
            if all((x + dx, y) not in occupied for dx in range(width)):
                return x, y
    return 0, max_y + 1
