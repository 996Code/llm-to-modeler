"""knowledge_graph 插件 HTTP API(平台挂载在 /api/packs/knowledge_graph)。

【鉴权】管理类端点 admin_required(与 /api/admin 同一把口令);
检索 /search 用户级(M4 落地)。

【端点清单】(M2 范围:库 CRUD / 模板 / 文档上传管理 / 图谱浏览 / 依赖状态)
  GET    /kbs                          知识库列表(含文档/实体/关系计数)
  GET    /kbs/templates                本体模板清单(建库下拉)
  POST   /kbs                          建库 {name, description, template}
  GET    /kbs/{id}                     库详情 + 图谱实时计数
  PUT    /kbs/{id}                     改库 {name?, description?, schema?}
  DELETE /kbs/{id}                     删库(联动 Neo4j/Milvus/文件/元数据)
  GET    /kbs/{id}/documents           文档列表
  POST   /kbs/{id}/documents           上传(multipart files,多文件;查重跳过)
  DELETE /kbs/{id}/documents/{doc_id}  删文档(联动清理;导入任务 M3)
  GET    /kbs/{id}/graph               图谱浏览(限量节点+邻接边;过滤 q/types)
  GET    /kbs/{id}/graph/expand        点击节点增量展开(1 跳)
  GET    /kbs/{id}/stats               库统计(元数据 + 图谱/向量实时数)
  GET    /dependency-status            Neo4j/Milvus 探针结果(设置页重检用)
"""
import hashlib
import logging
import re
import shutil
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from sdk.pack_api import admin_required
from domains.knowledge_graph import runtime
from domains.knowledge_graph.doc_parser import (
    allowed_extension, mime_for, parse_to_text,
)
from domains.knowledge_graph.schema_templates import (
    get_template_schema, list_templates,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _kb_or_404(request: Request, kb_id: str) -> Dict[str, Any]:
    kb = runtime.get_kg_store(request.app.state).get_kb(kb_id)
    if not kb:
        raise HTTPException(404, "知识库不存在")
    return kb


def _graph(request: Request):
    """图存储;连接失败给运维可读的 503。"""
    try:
        return runtime.get_graph(request.app.state)
    except Exception as e:
        logger.exception("neo4j 连接失败")
        raise HTTPException(503, f"Neo4j 连接失败: {e}")


# ── 知识库 CRUD ─────────────────────────────────────────────

@router.get("/kbs", dependencies=[Depends(admin_required)])
async def list_kbs(request: Request):
    return {"items": runtime.get_kg_store(request.app.state).list_kbs()}


@router.get("/kbs/templates", dependencies=[Depends(admin_required)])
async def kb_templates(request: Request):
    return {"items": list_templates()}


@router.post("/kbs", dependencies=[Depends(admin_required)])
async def create_kb(request: Request, payload: Dict[str, Any]):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "知识库名称不能为空")
    description = str(payload.get("description") or "").strip()
    template = str(payload.get("template") or "general").strip()

    store = runtime.get_kg_store(request.app.state)
    if store.get_kb_by_name(name):
        raise HTTPException(409, f"知识库「{name}」已存在")
    kb = store.create_kb(
        name=name, description=description,
        schema_json=get_template_schema(template), schema_template=template,
    )
    logger.info(f"kb created: {kb['id']} ({name}, template={template})")
    return kb


@router.get("/kbs/{kb_id}", dependencies=[Depends(admin_required)])
async def get_kb(kb_id: str, request: Request):
    kb = _kb_or_404(request, kb_id)
    detail = dict(kb)
    detail["graph"] = _graph(request).counts(kb_id)
    return detail


@router.put("/kbs/{kb_id}", dependencies=[Depends(admin_required)])
async def update_kb(kb_id: str, request: Request, payload: Dict[str, Any]):
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    name = payload.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            raise HTTPException(422, "知识库名称不能为空")
        existing = store.get_kb_by_name(name)
        if existing and existing["id"] != kb_id:
            raise HTTPException(409, f"知识库「{name}」已存在")
    schema = payload.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise HTTPException(422, "schema 必须是对象")
    store.update_kb(
        kb_id, name=name,
        description=str(payload.get("description") or "").strip() if payload.get("description") is not None else None,
        schema_json=schema,
    )
    return store.get_kb(kb_id)


@router.delete("/kbs/{kb_id}", dependencies=[Depends(admin_required)])
async def delete_kb(kb_id: str, request: Request):
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)

    # 三存储 + 文件联动清理(单点失败不阻断:后续残留可再删一次)
    errors: List[str] = []
    try:
        _graph(request).delete_kb(kb_id)
    except Exception as e:
        errors.append(f"neo4j: {e}")
    kb = store.get_kb(kb_id)
    try:
        if kb and kb.get("vectorEnabled"):
            runtime.get_vector(request.app.state).drop_collection(kb_id)
    except Exception as e:
        errors.append(f"milvus: {e}")
    doc_dir = runtime.files_root() / kb_id
    if doc_dir.exists():
        shutil.rmtree(doc_dir, ignore_errors=True)
    store.delete_kb(kb_id)
    logger.info(f"kb deleted: {kb_id} (errors={errors})")
    return {"success": True, "cleanupErrors": errors}


# ── 文档管理 ─────────────────────────────────────────────────

@router.get("/kbs/{kb_id}/documents", dependencies=[Depends(admin_required)])
async def list_documents(kb_id: str, request: Request):
    _kb_or_404(request, kb_id)
    return {"items": runtime.get_kg_store(request.app.state).list_documents(kb_id)}


@router.post("/kbs/{kb_id}/documents", dependencies=[Depends(admin_required)])
async def upload_documents(
    kb_id: str, request: Request, files: List[UploadFile] = File(...),
):
    """多文件上传:类型/大小白名单校验,落盘 + 建元数据;同库同内容查重跳过。"""
    _kb_or_404(request, kb_id)
    settings = runtime.settings_reader(request.app.state)
    max_bytes = max(1, int(settings.get("max_upload_mb", 20))) * 1024 * 1024
    store = runtime.get_kg_store(request.app.state)

    results: List[Dict[str, Any]] = []
    for f in files:
        filename = _sanitize_filename(f.filename or "unnamed")
        if not allowed_extension(filename):
            results.append({"filename": f.filename, "ok": False,
                            "reason": "不支持的格式(仅 md/txt/pdf/docx)"})
            continue
        data = await f.read()
        if len(data) > max_bytes:
            results.append({"filename": f.filename, "ok": False,
                            "reason": f"超过单文件上限 {max_bytes // 1024 // 1024}MB"})
            continue
        if not data:
            results.append({"filename": f.filename, "ok": False, "reason": "空文件"})
            continue

        content_hash = hashlib.sha256(data).hexdigest()
        dup = store.get_document_by_hash(kb_id, content_hash)
        if dup:
            results.append({"filename": f.filename, "ok": False,
                            "reason": f"同内容文件已存在: {dup['filename']}",
                            "duplicateOf": dup["id"]})
            continue

        # 解析预检:能解析才收(坏文件当场拒,不进导入队列再炸)
        try:
            parse_to_text(filename, data)
        except Exception as e:
            results.append({"filename": f.filename, "ok": False,
                            "reason": f"解析失败: {e}"})
            continue

        doc = store.create_document(
            kb_id=kb_id, filename=filename, mime_type=mime_for(filename),
            size_bytes=len(data), file_path="", content_hash=content_hash,
        )
        doc_dir = runtime.files_root() / kb_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        file_path = doc_dir / f"{doc['id']}{_ext_of(filename)}"
        file_path.write_bytes(data)
        store.update_document(doc["id"], file_path=str(file_path))
        results.append({"filename": f.filename, "ok": True, "document": doc})
        logger.info(f"document uploaded: {doc['id']} ({filename}, {len(data)}B)")

    return {"items": results}


def _ext_of(filename: str) -> str:
    """保留原始扩展名(小写;存储名 = doc_id + 扩展名,文件名本身已净化)。"""
    import os
    return os.path.splitext(filename)[1].lower()


_SAFE_NAME_RE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")


def _sanitize_filename(name: str) -> str:
    """文件名净化:去路径分隔/控制字符,限长(存储名 = doc_id + 扩展名,本身安全)。"""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._") or "unnamed"
    return base[:120]


@router.delete("/kbs/{kb_id}/documents/{doc_id}", dependencies=[Depends(admin_required)])
async def delete_document(kb_id: str, doc_id: str, request: Request):
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")

    errors: List[str] = []
    # 图谱贡献清理(未导入过的文档跳过)
    if doc["importStatus"] in ("succeeded", "partial", "importing"):
        try:
            _graph(request).delete_document(kb_id, doc_id)
        except Exception as e:
            errors.append(f"neo4j: {e}")
    # 向量清理(向量启用且导入过的库)
    kb = store.get_kb(kb_id)
    try:
        if kb and kb.get("vectorEnabled") and doc["importStatus"] in ("succeeded", "partial", "importing"):
            runtime.get_vector(request.app.state).delete_by_doc(kb_id, doc_id)
    except Exception as e:
        errors.append(f"milvus: {e}")
    # 文件与元数据
    stored_path = store.document_file_path(doc_id)
    if stored_path:
        try:
            import os
            os.remove(stored_path)
        except OSError:
            pass
    store.delete_document(doc_id)
    logger.info(f"document deleted: {doc_id} (errors={errors})")
    return {"success": True, "cleanupErrors": errors}


# ── 图谱浏览 ─────────────────────────────────────────────────

@router.get("/kbs/{kb_id}/graph", dependencies=[Depends(admin_required)])
async def get_graph_view(kb_id: str, request: Request):
    _kb_or_404(request, kb_id)
    settings = runtime.settings_reader(request.app.state)
    q = (request.query_params.get("q") or "").strip()
    types_param = (request.query_params.get("types") or "").strip()
    node_types = [t for t in types_param.split(",") if t.strip()] or None
    return _graph(request).get_graph(
        kb_id, q=q, node_types=node_types,
        limit_nodes=int(settings.get("graph_max_nodes", 80)),
        limit_edges=int(settings.get("graph_max_edges", 150)),
    )


@router.get("/kbs/{kb_id}/graph/expand", dependencies=[Depends(admin_required)])
async def expand_graph_node(kb_id: str, request: Request):
    _kb_or_404(request, kb_id)
    node_id = (request.query_params.get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(422, "node_id 不能为空")
    return _graph(request).expand_node(kb_id, node_id)


@router.get("/kbs/{kb_id}/stats", dependencies=[Depends(admin_required)])
async def kb_stats(kb_id: str, request: Request):
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    kb = store.get_kb(kb_id)
    stats: Dict[str, Any] = {"documents": len(store.list_documents(kb_id))}
    try:
        stats["graph"] = _graph(request).counts(kb_id)
    except Exception as e:
        stats["graph"] = {"error": str(e)}
    if kb and kb.get("vectorEnabled"):
        try:
            stats["vectorChunks"] = runtime.get_vector(request.app.state).count(kb_id)
        except Exception as e:
            stats["vectorChunks"] = None
            stats["vectorError"] = str(e)
    return stats


# ── 导入任务发起 ─────────────────────────────────────────────

def _task_manager_or_503(request):
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise HTTPException(503, "任务框架未初始化")
    return manager


@router.post("/kbs/{kb_id}/documents/{doc_id}/import", dependencies=[Depends(admin_required)])
async def import_document(kb_id: str, doc_id: str, request: Request, payload: Dict[str, Any] = None):
    """发起单文档导入(后台任务;同库串行,返回任务 ID 供任务中心/页面跟踪)。"""
    _kb_or_404(request, kb_id)
    _task_manager_or_503(request)
    payload = payload or {}
    from domains.knowledge_graph import tasks
    try:
        task = tasks.submit_import(
            request.app.state, kb_id, doc_id, force=bool(payload.get("force")),
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return task


@router.post("/kbs/{kb_id}/import", dependencies=[Depends(admin_required)])
async def import_documents(kb_id: str, request: Request, payload: Dict[str, Any] = None):
    """批量导入:默认处理全部"未成功"文档;body 可指定 doc_ids / force。"""
    _kb_or_404(request, kb_id)
    _task_manager_or_503(request)
    payload = payload or {}
    force = bool(payload.get("force"))
    store = runtime.get_kg_store(request.app.state)

    doc_ids = payload.get("doc_ids")
    if doc_ids:
        docs = []
        for did in doc_ids:
            d = store.get_document(str(did))
            if not d or d["kbId"] != kb_id:
                raise HTTPException(404, f"文档不存在: {did}")
            docs.append(d)
    else:
        docs = [d for d in store.list_documents(kb_id)
                if d["importStatus"] != "succeeded" or force]

    from domains.knowledge_graph import tasks
    tasks_created = []
    skipped = []
    for d in docs:
        if d["importStatus"] == "succeeded" and not force:
            skipped.append({"docId": d["id"], "reason": "已导入(未强制)"})
            continue
        try:
            tasks_created.append(tasks.submit_import(request.app.state, kb_id, d["id"], force=force))
        except ValueError as e:
            skipped.append({"docId": d["id"], "reason": str(e)})
    return {"tasks": tasks_created, "skipped": skipped}


@router.post("/kbs/{kb_id}/schema/induce", dependencies=[Depends(admin_required)])
async def induce_schema(kb_id: str, request: Request, payload: Dict[str, Any] = None):
    """发起本体归纳任务(抽样 → LLM 归纳 → 存为待审提案)。"""
    _kb_or_404(request, kb_id)
    _task_manager_or_503(request)
    payload = payload or {}
    from domains.knowledge_graph import tasks
    try:
        task = tasks.submit_induce_schema(
            request.app.state, kb_id,
            sample_chunks=int(payload.get("sample_chunks") or 8),
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return task


# ── 检索(用户级:身份由 X-User-Id 透传,不走管理口令) ──────────

@router.post("/search")
async def search(request: Request, payload: Dict[str, Any]):
    """混合检索问答(非流式;宿主/前端直接调用)。

    Body: {kb: 知识库名或ID(可选,缺省唯一库), query: 问题, top_k?: 向量条数}
    """
    query = str((payload or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(422, "query 不能为空")
    kb_ref = str((payload or {}).get("kb") or "").strip()
    top_k = payload.get("top_k")

    store = runtime.get_kg_store(request.app.state)
    kb = None
    if kb_ref:
        kb = store.get_kb_by_name(kb_ref) or store.get_kb(kb_ref)
    else:
        kbs = store.list_kbs()
        if len(kbs) == 1:
            kb = kbs[0]
        elif not kbs:
            raise HTTPException(404, "当前没有任何知识库")
        else:
            raise HTTPException(422, f"存在 {len(kbs)} 个知识库,请用 kb 参数指定: "
                                     f"{[k['name'] for k in kbs]}")
    if not kb:
        raise HTTPException(404, f"知识库不存在: {kb_ref}")

    from domains.knowledge_graph import retrieval
    from sdk.pack_api import user_id
    user = user_id(request)
    try:
        return retrieval.answer_question(
            request.app.state, kb, query, conv_id=None,
        ) | {"user": user}
    except Exception as e:
        logger.exception("search failed")
        raise HTTPException(502, f"检索失败: {e}")


# ── 依赖状态(设置页"重新检测"用) ─────────────────────────────

@router.get("/dependency-status", dependencies=[Depends(admin_required)])
async def dependency_status(request: Request):
    """对 Neo4j/Milvus 各跑一次探针(结果即时,不走缓存)。"""
    from domains.knowledge_graph import probes
    settings = runtime.settings_reader(request.app.state).all()

    def _probe(fn_name: str) -> Dict[str, Any]:
        fn = getattr(probes, fn_name)
        try:
            fn(settings)
            return {"ok": True, "detail": ""}
        except Exception as e:
            return {"ok": False, "detail": str(e) or type(e).__name__}

    return {
        "neo4j": _probe("neo4j"),
        "milvus": _probe("milvus"),
    }
