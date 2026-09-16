"""chatbi 插件 HTTP API(/api/packs/chatbi/*)。

移植来源: chat-bi backend/app/api/data_sources.py(613) + semantic_models.py(613)
+ graph.py(381) + memory.py(442) 的端点全集;适配: JWT/租户 → 宿主身份
(X-User-Id;管理操作走 admin_required)。

端点:
  数据源: GET/POST /datasources, PUT/DELETE /datasources/{id},
          POST /datasources/{id}/health, POST /datasources/{id}/scan,
          GET  /datasources/{id}/scan
  语义层: GET/PUT /datasources/{id}/semantic-models, GET /semantic-diff,
          POST /semantic-rollback
  图谱:   GET /datasources/{id}/graph, GET /datasources/{id}/graph/subgraph,
          POST /join-path-preview
  记忆:   GET /memories, PUT /memories, DELETE /memories/{mid},
          POST /memories/consolidate(异步任务, 任务中心轮询进度)
  引导:   GET /sample-questions
"""
from __future__ import annotations

import logging

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sdk.pack_api import admin_required
from sdk.relational_store import PackRelationalDB

from domains.chatbi import datasources
from domains.chatbi.runtime import get_pack_db

logger = logging.getLogger(__name__)

router = APIRouter()

# M4: 保存查询 + 看板路由(端口全集见 m4.py)
from domains.chatbi.m4 import router as m4_router
router.include_router(m4_router)


def _db() -> PackRelationalDB:
    return get_pack_db()


def _user_id(request: Request) -> str:
    return request.headers.get("X-User-Id", "anonymous")


def _audit(request: Request, resource_type: str, action: str,
           resource_id: str = None, detail: dict = None,
           status: str = "success", conv_id: str = None,
           duration_ms: int = None) -> None:
    """写业务审计事件(平台统一入口;失败只记日志不阻断主流程)。

    pack_name 固定 chatbi(本文件即 chatbi 域 API);store 来自 app.state。
    """
    try:
        store = request.app.state.conversation_store
        store.write_audit_log(
            user_id=_user_id(request),
            resource_type=resource_type,
            action=action,
            status=status,
            resource_id=resource_id,
            detail=detail,
            conv_id=conv_id,
            pack_name="chatbi",
            ip_address=request.client.host if request.client else None,
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.warning("chatbi audit write failed: %s", e)


def _info_to_dict(info) -> dict:
    """DataSourceInfo → 前端驼峰 dict(密码字段永不回显)。"""
    return {
        "id": info.id, "name": info.name, "dbType": info.db_type,
        "host": info.host, "port": info.port, "database": info.database,
        "username": info.username, "isActive": info.is_active,
        "scanStatus": info.scan_status, "scanProgress": info.scan_progress,
        "scanStage": info.scan_stage, "scanError": info.scan_error,
        "scannedAt": info.scanned_at, "createdAt": info.created_at,
        "updatedAt": info.updated_at,
    }


# ── Pydantic 请求体 ──────────────────────────────────────────

class DatasourceCreate(BaseModel):
    name: str
    db_type: str = "postgresql"       # mysql | postgresql
    host: str
    port: int = 5432
    database: str
    username: str
    password: str


class DatasourceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None       # 提供则重加密
    is_active: bool | None = None


class SemanticContentIn(BaseModel):
    content: dict                      # SemanticModelContent JSON(人工校正)


class JoinPathIn(BaseModel):
    tables: list[str]


class GraphRelationshipIn(BaseModel):
    """图谱关系增删请求(非法枚举直接 422, 不等内部构造才炸)。"""
    from_table: str
    target_table: str
    join_type: Literal["INNER", "LEFT", "RIGHT", "FULL"] = "LEFT"
    on: str                       # JOIN ON 条件(表.列 = 表.列 [AND ...])
    cardinality: Literal["N:1", "1:N", "1:1", "N:N"] = "N:1"


# ── 数据源管理(移植 data_sources.py 全集,密码永不回显) ────────

@router.get("/datasources", dependencies=[Depends(admin_required)])
async def list_datasources():
    return {"items": [_info_to_dict(i) for i in datasources.list_datasources(_db())]}


@router.post("/datasources", dependencies=[Depends(admin_required)])
async def create_datasource(body: DatasourceCreate, request: Request):
    try:
        info = datasources.create_datasource(
            _db(), body.name, body.db_type, body.host, body.port,
            body.database, body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(request, "datasource", "create", resource_id=info.id,
           detail={"name": info.name, "db_type": info.db_type})
    return _info_to_dict(info)


@router.put("/datasources/{ds_id}", dependencies=[Depends(admin_required)])
async def update_datasource(ds_id: str, body: DatasourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not datasources.update_datasource(_db(), ds_id, **fields):
        raise HTTPException(404, "数据源不存在")
    return {"ok": True}


@router.delete("/datasources/{ds_id}", dependencies=[Depends(admin_required)])
async def delete_datasource(ds_id: str, request: Request):
    db = _db()
    info = datasources.get_datasource(db, ds_id)
    # 级联清理(超越源——源无删除端点, 停用替代): 语义版本 + 向量 collection
    # + fewshot 行(stores.delete_data_source_storage: drop collection +
    # 删 few-shot + clear scope 登记) + 记忆(按 data_source_id 定向清理,
    # 表已加列——此前无列想清也清不了, linkage 残留污染图谱演化)。
    # 失败升级为 500 而非静默孤儿。
    from domains.chatbi import semantic, stores
    semantic.delete_by_datasource(db, ds_id)
    # M4 级联: 清理保存查询/看板 widget 的悬空引用
    with db.connect() as conn:
        conn.execute("DELETE FROM chatbi_saved_queries WHERE data_source_id = ?", (ds_id,))
        conn.execute("DELETE FROM chatbi_dashboard_widgets WHERE datasource_id = ?", (ds_id,))
        # 记忆级联(含 linkage 共现经验; recent_queries 等用户级记忆不绑 ds 不动)
        conn.execute("DELETE FROM chatbi_agent_memories WHERE data_source_id = ?", (ds_id,))
    try:
        stores.delete_data_source_storage(db, stores.get_vector(request.app.state), ds_id)
    except Exception as e:
        logger.error("数据源 %s 向量/fewshot 级联清理失败: %s", ds_id, e)
        raise HTTPException(500, f"向量存储清理失败, 已中止删除(避免孤儿): {e}")
    if not datasources.delete_datasource(db, ds_id):
        raise HTTPException(404, "数据源不存在")
    _audit(request, "datasource", "delete", resource_id=ds_id,
           detail={"name": info.name if info else ds_id})
    return {"ok": True}


@router.post("/datasources/health-check/all", dependencies=[Depends(admin_required)])
async def health_check_all():
    """批量健康巡检(对标源 POST /health-check/all): 连续失败自动停用/恢复。

    同步执行(数据源数量级小, 单源 5s 超时);定时巡检由 pack 调度线程
    每 5 分钟触发同一函数。
    """
    from domains.chatbi import datasources as ds_mod
    from domains.chatbi.runtime import get_pack_db
    return ds_mod.check_all_health(get_pack_db())


@router.post("/datasources/{ds_id}/health", dependencies=[Depends(admin_required)])
async def datasource_health(ds_id: str):
    info = datasources.get_datasource(_db(), ds_id, decrypt=True)
    if not info:
        raise HTTPException(404, "数据源不存在")
    return datasources.check_health(info)


@router.post("/datasources/{ds_id}/scan", dependencies=[Depends(admin_required)])
async def trigger_scan(ds_id: str, request: Request):
    """触发语义层扫描(后台任务,进度经 GET /scan 轮询)。"""
    if not datasources.get_datasource(_db(), ds_id):
        raise HTTPException(404, "数据源不存在")
    from sdk.pack_api import DuplicateTaskError
    manager = request.app.state.task_manager
    try:
        task = manager.submit("chatbi.scan_datasource",
                              payload={"datasource_id": ds_id},
                              dedupe_key=f"chatbi:scan:{ds_id}")
    except DuplicateTaskError:
        raise HTTPException(409, "该数据源已有扫描任务在进行")
    _audit(request, "datasource", "scan", resource_id=ds_id,
           detail={"task_id": task["id"]})
    return {"task_id": task["id"]}


@router.get("/datasources/{ds_id}/scan", dependencies=[Depends(admin_required)])
async def scan_status(ds_id: str):
    info = datasources.get_datasource(_db(), ds_id)
    if not info:
        raise HTTPException(404, "数据源不存在")
    return {"scanStatus": info.scan_status, "scanProgress": info.scan_progress,
            "scanStage": info.scan_stage, "scanError": info.scan_error,
            "scannedAt": info.scanned_at}


@router.post("/datasources/refresh-metadata/all", dependencies=[Depends(admin_required)])
async def refresh_metadata_all(request: Request):
    """手动触发元数据刷新(对标源 POST /data-sources/refresh-metadata/all)。

    纯结构内省(无 LLM)+ 保留人工标注; 已扫描过的 active 数据源全量检查,
    内容指纹未变不落新版本。后台任务执行, 结果经任务中心查询。
    """
    from sdk.pack_api import DuplicateTaskError
    manager = request.app.state.task_manager
    try:
        task = manager.submit("chatbi.refresh_semantics",
                              payload={},
                              dedupe_key="chatbi:refresh:all")
    except DuplicateTaskError:
        raise HTTPException(409, "已有元数据刷新任务在进行")
    return {"task_id": task["id"]}


# ── 语义层(移植 semantic_models.py 全集) ─────────────────────

@router.get("/datasources/{ds_id}/semantic-models", dependencies=[Depends(admin_required)])
async def get_semantic_models(ds_id: str, version: int | None = None):
    from domains.chatbi import semantic
    content, ver = semantic.load_content(_db(), ds_id, version=version)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    return {"version": ver, "content": content.model_dump()}


@router.put("/datasources/{ds_id}/semantic-models", dependencies=[Depends(admin_required)])
async def update_semantic_models(ds_id: str, body: SemanticContentIn, request: Request):
    """人工校正 → F9 注入防御校验 → 人工标注打标 → 落新版本 → 重建索引。

    人工标注打标(源 semantic_models.py:269-297 的 PATCH 语义): 与当前版本
    diff, 被修改的表/列 display_name/description 置 source=manual,
    confidence=1.0——防止下次全量重扫时 _enrich_with_llm(只挑
    auto_inferred/0.5 的列)把 LLM 推断名覆盖掉人工校正名。
    """
    from domains.chatbi import semantic, stores
    from domains.chatbi.models import SemanticModelContent
    try:
        content = SemanticModelContent.model_validate(body.content)
    except Exception as e:
        raise HTTPException(400, f"语义层结构校验失败: {e}")
    # 人工修改打标: 与当前版本对比, 变化的表/列 → manual/1.0
    current = semantic.load_current_content(_db(), ds_id)
    marked = semantic.mark_manual_edits(current, content)
    if marked:
        logger.info("人工校正打标: %d 处表/列标注 → manual/1.0", marked)
    # F9 注入防御(源 semantic_models.py PATCH 对 formula/condition 校验):
    # schema 层不校验, API 层是唯一关口, 执行层三层校验是终极防线
    for model in content.models:
        for metric in model.metrics:
            errors = semantic.validate_metric_formula(metric.formula)
            if metric.condition:
                errors += semantic.validate_metric_formula(metric.condition)
            if errors:
                raise HTTPException(422, f"指标 {metric.name} 公式非法: {errors[0]}")
    ver = semantic.save_content(_db(), ds_id, content, source="manual")
    # 人工校正后重建向量索引(源 semantic_models.py:345-362;失败降级不阻塞)
    try:
        from domains.chatbi import indexing
        from domains.chatbi.llm_compat import LLMCompat
        indexing.rebuild_index(content, ds_id, stores.get_vector(request.app.state),
                               stores.get_embedder(LLMCompat(request.app.state.llm_client)),
                               db=_db())
    except Exception as e:
        logger.warning("人工校正后索引重建失败(降级): %s", e)
    _audit(request, "semantic", "update", resource_id=ds_id,
           detail={"version": ver})
    return {"ok": True, "version": ver}


@router.get("/semantic-diff", dependencies=[Depends(admin_required)])
async def semantic_diff(ds_id: str, from_version: int, to_version: int):
    from domains.chatbi import semantic
    try:
        return {"diff": semantic.diff_versions(_db(), ds_id, from_version, to_version)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/datasources/{ds_id}/semantic-versions", dependencies=[Depends(admin_required)])
async def semantic_versions(ds_id: str):
    """版本历史列表(对标源 GET /semantic-models/{id}/versions)。

    回滚/diff 前端需要枚举可用版本号;此前只有取单版本/diff/rollback,
    版本清单无从获知。
    """
    if not datasources.get_datasource(_db(), ds_id):
        raise HTTPException(404, "数据源不存在")
    with _db().connect() as conn:
        rows = conn.execute(
            "SELECT version, is_current, created_at FROM chatbi_semantic_models "
            "WHERE data_source_id = ? ORDER BY version DESC",
            (ds_id,)).fetchall()
    return {"items": [{"version": r["version"],
                       "isCurrent": bool(r["is_current"]),
                       "createdAt": r["created_at"]} for r in rows]}


@router.post("/semantic-rollback", dependencies=[Depends(admin_required)])
async def semantic_rollback(ds_id: str, version: int, request: Request):
    from domains.chatbi import semantic, stores
    try:
        ver, content = semantic.rollback(_db(), ds_id, version)
    except ValueError as e:
        raise HTTPException(404, str(e))
    # 回滚后重建索引(源 semantic_models.py:196-214;失败降级不阻塞)
    try:
        from domains.chatbi import indexing
        from domains.chatbi.llm_compat import LLMCompat
        indexing.rebuild_index(content, ds_id, stores.get_vector(request.app.state),
                               stores.get_embedder(LLMCompat(request.app.state.llm_client)),
                               db=_db())
    except Exception as e:
        logger.warning("回滚后索引重建失败(降级): %s", e)
    _audit(request, "semantic", "rollback", resource_id=ds_id,
           detail={"from_version": version, "new_version": ver})
    return {"ok": True, "version": ver}


# ── 图谱(移植 graph.py 的数据端点) ───────────────────────────

@router.get("/datasources/{ds_id}/graph", dependencies=[Depends(admin_required)])
async def get_graph(ds_id: str):
    """全图数据(节点/边/社区/枢纽;前端 G6/ECharts 渲染)。"""
    from domains.chatbi import schema_graph
    from domains.chatbi import semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    return schema_graph.get_schema_graph(content).to_vis_data()


@router.get("/datasources/{ds_id}/graph/subgraph", dependencies=[Depends(admin_required)])
async def get_graph_subgraph(ds_id: str, center: str, depth: int = 2):
    """子图数据(聚焦某表及其 depth-hop 邻居;大图浏览时按需下钻)。"""
    from domains.chatbi import schema_graph, semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    return schema_graph.get_schema_graph(content).to_vis_subgraph(center, depth)


@router.post("/join-path-preview", dependencies=[Depends(admin_required)])
async def join_path_preview(ds_id: str, body: JoinPathIn):
    """表集 → JOIN 路径预览(图谱预计算;管理端调试用)。"""
    from domains.chatbi import semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    from domains.chatbi.schema_graph import (get_schema_graph,
                                             expand_with_relationships,
                                             build_join_path_section)
    sg = get_schema_graph(content)
    expanded = expand_with_relationships(content, body.tables, graph=sg)
    join_text = build_join_path_section(content, expanded, graph=sg,
                                        seed_names=list(body.tables))
    return {"expanded_tables": expanded, "join_paths": join_text}


@router.get("/datasources/{ds_id}/graph/communities", dependencies=[Depends(admin_required)])
async def graph_communities(ds_id: str):
    """社区发现——将表按业务域聚类(社区着色/枢纽度排序的基础数据)。"""
    from domains.chatbi import schema_graph, semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    sg = schema_graph.get_schema_graph(content)
    return {"communities": sg.get_communities()}


@router.get("/datasources/{ds_id}/graph/hubs", dependencies=[Depends(admin_required)])
async def graph_hubs(ds_id: str, top_k: int = 10):
    """枢纽表识别——度中心度最高的表(前后端统一渲染节点大小用)。"""
    from domains.chatbi import schema_graph, semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    sg = schema_graph.get_schema_graph(content)
    return {"hubs": [{"table": t, "centrality": round(c, 4)} for t, c in sg.get_hub_tables(top_k)]}


@router.get("/datasources/{ds_id}/graph/impact", dependencies=[Depends(admin_required)])
async def graph_impact(ds_id: str, table: str):
    """影响分析——从给定表可达的所有下游表(改表前评估风险)。"""
    from domains.chatbi import schema_graph, semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    sg = schema_graph.get_schema_graph(content)
    return {"table": table, "impact": sg.get_impact(table)}


@router.get("/datasources/{ds_id}/graph/reverse-relationships", dependencies=[Depends(admin_required)])
async def graph_reverse_relationships(ds_id: str, table: str):
    """反向关系——哪些表的正向关系指向此表(如 uc_users 被 N 张表引用)。"""
    from domains.chatbi import schema_graph, semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    sg = schema_graph.get_schema_graph(content)
    return {"table": table, "relationships": sg.get_reverse_relationships(table)}


@router.get("/datasources/{ds_id}/graph/table-columns", dependencies=[Depends(admin_required)])
async def graph_table_columns(ds_id: str, table: str):
    """表列信息——节点详情面板按需拉列名/类型/语义类型。"""
    from domains.chatbi import semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        raise HTTPException(404, "该数据源尚未扫描语义层")
    for m in content.models:
        if m.name == table:
            return {"columns": [
                {"name": c.name, "display_name": c.display_name,
                 "data_type": c.data_type, "semantic_type": c.semantic_type,
                 "description": c.description} for c in m.columns
            ]}
    raise HTTPException(404, f"表 {table} 不存在")


def _graph_index_rebuilder(request: Request, ds_id: str, content):
    """图谱变更后的索引重建回调(向量 store/embedder 从 app_state 构造)。"""
    def _rebuild():
        from domains.chatbi import indexing, stores
        from domains.chatbi.llm_compat import LLMCompat
        indexing.rebuild_index(
            content, ds_id,
            stores.get_vector(request.app.state),
            stores.get_embedder(LLMCompat(request.app.state.llm_client)),
            db=_db())
    return _rebuild


@router.post("/datasources/{ds_id}/graph/relationship", dependencies=[Depends(admin_required)])
async def graph_add_relationship(ds_id: str, body: GraphRelationshipIn, request: Request):
    """新增图谱关系——源/目标表 + ON 列级校验,写回语义层新版本 + 重建索引 + 审计。

    索引重建失败不谎报成功: 响应带 index_rebuilt=False + warning,
    前端提示"语义层已保存, 索引待重建"。
    """
    from domains.chatbi import graph_edit, semantic
    try:
        result = graph_edit.add_relationship(
            _db(), ds_id,
            from_table=body.from_table, target_table=body.target_table,
            join_type=body.join_type, on=body.on, cardinality=body.cardinality)
    except graph_edit.GraphEditError as e:
        raise HTTPException(e.status, e.message)
    # 版本落库后重建索引(成功/失败均不改变语义层结果, 只影响响应标志)
    content = semantic.load_current_content(_db(), ds_id)
    try:
        _graph_index_rebuilder(request, ds_id, content)()
        index_rebuilt, warning = True, None
    except Exception as e:
        logger.warning("新增关系后索引重建失败(降级): %s", e)
        index_rebuilt, warning = False, str(e)[:200]
    _audit(request, "graph", "create", resource_id=ds_id,
           detail={"from": body.from_table, "to": body.target_table,
                   "on": body.on, "version": result["version"],
                   "index_rebuilt": index_rebuilt})
    return {"ok": True, "version": result["version"],
            "index_rebuilt": index_rebuilt, "warning": warning}


@router.delete("/datasources/{ds_id}/graph/relationship", dependencies=[Depends(admin_required)])
async def graph_delete_relationship(ds_id: str, from_table: str, target_table: str, on: str = "",
                                     request: Request = None):
    """删除图谱关系——正反向一起清理,写回语义层新版本 + 重建索引 + 审计。"""
    from domains.chatbi import graph_edit, semantic
    try:
        result = graph_edit.delete_relationship(
            _db(), ds_id, from_table=from_table, target_table=target_table, on=on)
    except graph_edit.GraphEditError as e:
        raise HTTPException(e.status, e.message)
    content = semantic.load_current_content(_db(), ds_id)
    try:
        _graph_index_rebuilder(request, ds_id, content)()
        index_rebuilt, warning = True, None
    except Exception as e:
        logger.warning("删除关系后索引重建失败(降级): %s", e)
        index_rebuilt, warning = False, str(e)[:200]
    _audit(request, "graph", "delete", resource_id=ds_id,
           detail={"from": from_table, "to": target_table, "on": on,
                   "version": result["version"],
                   "removed_forward": result["removed_forward"],
                   "removed_reverse": result["removed_reverse"],
                   "index_rebuilt": index_rebuilt})
    return {"ok": True, "version": result["version"],
            "removed_forward": result["removed_forward"],
            "removed_reverse": result["removed_reverse"],
            "index_rebuilt": index_rebuilt, "warning": warning}


# ── 记忆(移植 memory.py 管理端点) ────────────────────────────

@router.get("/memories", dependencies=[Depends(admin_required)])
async def list_memories(user_id: str | None = None, limit: int = 100,
                        include_consolidated: bool = True):
    from domains.chatbi import memory
    return {"items": memory.list_memories(_db(), user_id=user_id, limit=limit,
                                          include_consolidated=include_consolidated)}


class MemoryIn(BaseModel):
    name: str
    description: str = ""
    content: str
    memory_type: str = "project"      # project | preference | business | linkage
    mem_id: str | None = None         # 有值 = 更新, 无值 = 新建
    data_source_id: str | None = None


@router.put("/memories", dependencies=[Depends(admin_required)])
async def save_memory(body: MemoryIn, request: Request):
    """新建/更新记忆(对标源 PUT /memory;业务方自助沉淀业务约定)。"""
    from domains.chatbi import memory
    if not body.name.strip() or not body.content.strip():
        raise HTTPException(422, "名称与内容不能为空")
    mem_id = memory.get_memory_store(_db()).save_memory(
        name=body.name.strip(), description=body.description.strip(),
        content=body.content.strip(), memory_type=body.memory_type,
        mem_id=body.mem_id, data_source_id=body.data_source_id)
    _audit(request, "memory", "update" if body.mem_id else "create",
           resource_id=mem_id, detail={"name": body.name.strip(), "type": body.memory_type})
    return {"ok": True, "id": mem_id}


@router.delete("/memories/{mid}", dependencies=[Depends(admin_required)])
async def delete_memory(mid: str, request: Request):
    from domains.chatbi import memory
    if not memory.delete_memory(_db(), mid):
        raise HTTPException(404, "记忆不存在")
    _audit(request, "memory", "delete", resource_id=mid)
    return {"ok": True}


class ConsolidateIn(BaseModel):
    """记忆整理请求(可选数据源限定)。"""
    data_source_id: str | None = None   # 只整理该数据源(None=全部, 仍按scope分组)


@router.post("/memories/consolidate", dependencies=[Depends(admin_required)])
async def consolidate_memories_ep(body: ConsolidateIn | None = None, request: Request = None):
    """记忆整理(LLM 合并去重碎片记忆)——异步任务, 进度走任务中心 SSE。

    对标源 POST /memory/consolidate 的 202+轮询语义;插件化后复用平台
    任务中心(任务列表/日志/进度统一观测), 不再自造轮询端点。

    scope 隔离(复核报告 P0): 按 用户+数据源 分组整理, 新记忆继承归属;
    data_source_id 可限定只整理某个数据源。整理完成后对涉及的每个数据源
    执行 linkage → 图谱同步(任务日志可见, 失败降级不静默)。
    """
    from sdk.pack_api import DuplicateTaskError
    manager = request.app.state.task_manager
    payload = {"data_source_id": body.data_source_id} if body and body.data_source_id else {}
    try:
        task = manager.submit("chatbi.memory.consolidate", payload=payload,
                              dedupe_key="chatbi:memconsolidate")
    except DuplicateTaskError:
        raise HTTPException(409, "已有记忆整理任务在进行")
    _audit(request, "memory", "consolidate",
           detail={"task_id": task["id"], "data_source_id": body.data_source_id if body else None})
    return {"task_id": task["id"], "status": "submitted"}


# ── 引导(扫描产出的示例问题) ─────────────────────────────────

@router.get("/sample-questions", dependencies=[Depends(admin_required)])
async def sample_questions(ds_id: str):
    from domains.chatbi import semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        return {"items": []}
    return {"items": content.sample_questions}


# ── Skills 业务规则(只读管理端点;文件随 pack 分发, 编辑走部署流程) ──

@router.get("/skills", dependencies=[Depends(admin_required)])
async def list_skills():
    """业务规则清单(名称/描述/版本/正文/方言 reference)——管理页展示用。"""
    from domains.chatbi.skills_loader import SkillsLoader
    skills = SkillsLoader().load_all()
    return {"items": [{
        "name": s.name, "description": s.description, "version": s.version,
        "content": s.content,
        "references": {k: v for k, v in s.references.items()},
    } for s in skills.values()]}
