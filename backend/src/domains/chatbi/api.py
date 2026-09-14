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
  图谱:   GET /datasources/{id}/graph, POST /join-path-preview
  记忆:   GET /memories, DELETE /memories/{mid}
  引导:   GET /sample-questions
"""
from __future__ import annotations

import logging

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


# ── 数据源管理(移植 data_sources.py 全集,密码永不回显) ────────

@router.get("/datasources", dependencies=[Depends(admin_required)])
async def list_datasources():
    return {"items": [_info_to_dict(i) for i in datasources.list_datasources(_db())]}


@router.post("/datasources", dependencies=[Depends(admin_required)])
async def create_datasource(body: DatasourceCreate):
    try:
        info = datasources.create_datasource(
            _db(), body.name, body.db_type, body.host, body.port,
            body.database, body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    # 级联清理(超越源——源无删除端点, 停用替代): 语义版本 + 向量 collection
    # + fewshot 行(stores.delete_data_source_storage: drop collection +
    # 删 few-shot + clear scope 登记)。失败升级为 500 而非静默孤儿。
    from domains.chatbi import semantic, stores
    semantic.delete_by_datasource(db, ds_id)
    try:
        stores.delete_data_source_storage(db, stores.get_vector(request.app.state), ds_id)
    except Exception as e:
        logger.error("数据源 %s 向量/fewshot 级联清理失败: %s", ds_id, e)
        raise HTTPException(500, f"向量存储清理失败, 已中止删除(避免孤儿): {e}")
    if not datasources.delete_datasource(db, ds_id):
        raise HTTPException(404, "数据源不存在")
    return {"ok": True}


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
    from services.task_manager import DuplicateTaskError
    manager = request.app.state.task_manager
    try:
        task = manager.submit("chatbi.scan_datasource",
                              payload={"datasource_id": ds_id},
                              dedupe_key=f"chatbi:scan:{ds_id}")
    except DuplicateTaskError:
        raise HTTPException(409, "该数据源已有扫描任务在进行")
    return {"task_id": task["id"]}


@router.get("/datasources/{ds_id}/scan", dependencies=[Depends(admin_required)])
async def scan_status(ds_id: str):
    info = datasources.get_datasource(_db(), ds_id)
    if not info:
        raise HTTPException(404, "数据源不存在")
    return {"scanStatus": info.scan_status, "scanProgress": info.scan_progress,
            "scanStage": info.scan_stage, "scanError": info.scan_error,
            "scannedAt": info.scanned_at}


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
    """人工校正 → F9 注入防御校验 → 落新版本(is_current 翻转) → 重建索引。"""
    from domains.chatbi import semantic, stores
    from domains.chatbi.models import SemanticModelContent
    try:
        content = SemanticModelContent.model_validate(body.content)
    except Exception as e:
        raise HTTPException(400, f"语义层结构校验失败: {e}")
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
    return {"ok": True, "version": ver}


@router.get("/semantic-diff", dependencies=[Depends(admin_required)])
async def semantic_diff(ds_id: str, from_version: int, to_version: int):
    from domains.chatbi import semantic
    try:
        return {"diff": semantic.diff_versions(_db(), ds_id, from_version, to_version)}
    except ValueError as e:
        raise HTTPException(404, str(e))


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


@router.post("/join-path-preview", dependencies=[Depends(admin_required)])
async def join_path_preview(ds_id: str, body: JoinPathIn):
    """表集 → JOIN 路径预览(图谱预计算;管理端调试用)。"""
    from domains.chatbi import schema_graph
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


# ── 记忆(移植 memory.py 管理端点) ────────────────────────────

@router.get("/memories", dependencies=[Depends(admin_required)])
async def list_memories(user_id: str | None = None, limit: int = 50):
    from domains.chatbi import memory
    return {"items": memory.list_memories(_db(), user_id=user_id, limit=limit)}


@router.delete("/memories/{mid}", dependencies=[Depends(admin_required)])
async def delete_memory(mid: str):
    from domains.chatbi import memory
    if not memory.delete_memory(_db(), mid):
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


# ── 引导(扫描产出的示例问题) ─────────────────────────────────

@router.get("/sample-questions")
async def sample_questions(ds_id: str):
    from domains.chatbi import semantic
    content = semantic.load_current_content(_db(), ds_id)
    if content is None:
        return {"items": []}
    return {"items": content.sample_questions}
