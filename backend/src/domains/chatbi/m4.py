"""M4: 保存查询 + 看板 —— 移植自原系统 saved_queries.py + dashboard.py。

原系统对标:
  - SavedQuery: 成功查询自动保存 + 列表/详情/CSV 导出
  - Dashboard: CRUD + Widget 添加/删除/布局/实时刷新
  - 实时查询模式: 不存结果快照, refresh 重跑 SQL + 缓存 chart_config 注入

pack 适配:
  - tenant 删除(user_id 归属); SQLAlchemy → PackRelationalDB
  - asyncio → 同步; JWT → admin_required / X-User-Id
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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from sdk.pack_api import admin_required
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
               chart_config: dict | None = None) -> dict:
    """成功查询后自动保存(应用层去重: 同 user+ds+sql 不重复)。"""
    import json as _json
    # 去重(SELECT-then-INSERT)
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM chatbi_saved_queries "
            "WHERE user_id = ? AND data_source_id = ? AND sql_text = ? LIMIT 1",
            (user_id, data_source_id, sql)).fetchone()
        if existing:
            return {"id": existing["id"], "deduplicated": True}
        qid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO chatbi_saved_queries
               (id, user_id, data_source_id, conversation_id, question, sql_text,
                result_summary, chart_config, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (qid, user_id, data_source_id, conversation_id, question, sql,
             None, _json.dumps(chart_config) if chart_config else None, _now()))
    return {"id": qid, "deduplicated": False}


# ── 保存查询 API ─────────────────────────────────────────────

@router.get("/saved-queries", dependencies=[Depends(admin_required)])
async def list_saved_queries(request: Request, limit: int = 50):
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
        "chartConfig": _json.loads(r["chart_config"]) if r["chart_config"] else None,
        "createdAt": r["created_at"],
    } for r in rows]}


@router.get("/saved-queries/{sq_id}/export", dependencies=[Depends(admin_required)])
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

    # SEC: 复用三层校验(不信任历史 SQL, 防注入)
    validation = validate_sql(q["sql_text"], allowed_columns=set())
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
    for row in result.rows[:_CSV_EXPORT_MAX_ROWS]:
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

@router.get("/dashboards", dependencies=[Depends(admin_required)])
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


@router.post("/dashboards", dependencies=[Depends(admin_required)])
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


@router.put("/dashboards/{did}", dependencies=[Depends(admin_required)])
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


@router.delete("/dashboards/{did}", dependencies=[Depends(admin_required)])
async def delete_dashboard(did: str, request: Request):
    uid = _user_id(request)
    with _db().connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_dashboards WHERE id = ? AND user_id = ?", (did, uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "看板不存在")
        conn.execute("DELETE FROM chatbi_dashboard_widgets WHERE dashboard_id = ?", (did,))
    return {"ok": True}


@router.get("/dashboards/{did}", dependencies=[Depends(admin_required)])
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
            "chartOption": _json.loads(w["chart_option"]) if w["chart_option"] else None,
            "positionX": w["position_x"], "positionY": w["position_y"],
            "width": w["width"], "height": w["height"],
        } for w in widgets],
    }


# ── Widget 操作 ──────────────────────────────────────────────

@router.post("/dashboards/{did}/widgets", dependencies=[Depends(admin_required)])
async def add_widget(did: str, body: WidgetCreate, request: Request):
    """添加 widget: 保存时跑一次 SQL 生成 chart_config 缓存(避免 refresh 重跑 LLM)。"""
    uid = _user_id(request)
    db = _db()
    with db.connect() as conn:
        dash = conn.execute(
            "SELECT id FROM chatbi_dashboards WHERE id = ? AND user_id = ?",
            (did, uid)).fetchone()
        if not dash:
            raise HTTPException(404, "看板不存在")

    # SEC: SQL 校验
    validation = validate_sql(body.query_sql, allowed_columns=set())
    if not validation.ok:
        raise HTTPException(400, f"SQL 校验失败: {validation.reason}")

    # 跑一次 SQL + 图表配置缓存
    chart_config = None
    try:
        ds = datasources.get_datasource(db, body.datasource_id, decrypt=True)
        if ds and ds.is_active:
            result = datasources.execute_readonly(ds, body.query_sql)
            if result.ok:
                from domains.chatbi.chart_engine import generate_chart
                from domains.chatbi.llm_compat import LLMCompat
                from domains.chatbi import stores
                # 管理端无 ToolContext, 直接构造 LLMCompat 包引擎 client
                llm = request.app.state.llm_client
                compat = LLMCompat(llm) if not isinstance(llm, LLMCompat) else llm
                chart_result = generate_chart(
                    compat, body.question, result.columns,
                    [tuple(r) for r in result.rows], chart_type_hint=body.chart_type)
                if chart_result.ok and chart_result.config:
                    chart_config = chart_result.config
    except Exception as e:
        logger.warning("看板 widget 图表配置生成失败(不影响保存): %s", e)

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
             body.chart_type, _json.dumps(chart_config) if chart_config else None,
             body.position_x if body.position_x is not None else auto_x,
             body.position_y if body.position_y is not None else auto_y,
             body.width, body.height, now, now))
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (now, did))
    return {"id": wid, "chartConfig": chart_config}


@router.delete("/dashboards/{did}/widgets/{wid}", dependencies=[Depends(admin_required)])
async def delete_widget(did: str, wid: str):
    with _db().connect() as conn:
        cur = conn.execute(
            "DELETE FROM chatbi_dashboard_widgets WHERE id = ? AND dashboard_id = ?",
            (wid, did))
        if cur.rowcount == 0:
            raise HTTPException(404, "Widget 不存在")
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (_now(), did))
    return {"ok": True}


@router.put("/dashboards/{did}/widgets/layout", dependencies=[Depends(admin_required)])
async def update_layout(did: str, body: LayoutUpdate):
    """批量更新 widget 布局(拖拽后保存)。"""
    with _db().connect() as conn:
        for item in body.layout:
            conn.execute(
                "UPDATE chatbi_dashboard_widgets "
                "SET position_x = ?, position_y = ?, width = ?, height = ?, updated_at = ? "
                "WHERE id = ? AND dashboard_id = ?",
                (item.position_x, item.position_y, item.width, item.height, _now(), item.id, did))
        conn.execute("UPDATE chatbi_dashboards SET updated_at = ? WHERE id = ?", (_now(), did))
    return {"ok": True, "updated": len(body.layout)}


@router.put("/dashboards/{did}/widgets/{wid}/refresh", dependencies=[Depends(admin_required)])
async def refresh_widget(did: str, wid: str, request: Request):
    """刷新 widget: 实时重跑 SQL + 缓存 chart_config inject_data(不调 LLM)。"""
    db = _db()
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
                _json.loads(w["chart_option"]), result.columns,
                [tuple(r) for r in result.rows])
        except Exception as e:
            logger.warning("看板图表缓存注入失败: %s", e)
    if chart_option is None:
        from domains.chatbi.chart_engine import infer_chart_by_rule
        chart_option = infer_chart_by_rule(result.columns, [tuple(r) for r in result.rows])

    return {
        "id": w["id"], "question": w["question"], "querySql": w["query_sql"],
        "datasourceId": w["datasource_id"],
        "columns": result.columns, "rows": [list(r) for r in result.rows[:200]],
        "rowCount": result.rowcount, "chartOption": chart_option,
    }


def _calc_next_position(existing: list) -> tuple:
    """自动布局: 按行填充, 每行 2 个(列宽 6 总宽 12), 超出换行。"""
    if not existing:
        return 0, 0
    max_y = max(p[1] for p in existing)
    same_row = [p for p in existing if p[1] == max_y]
    if len(same_row) < 2:
        return 6, max_y
    return 0, max_y + 1
