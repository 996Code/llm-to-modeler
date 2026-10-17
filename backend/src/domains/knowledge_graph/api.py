"""knowledge_graph 插件 HTTP API(平台挂载在 /api/packs/knowledge_graph)。

【鉴权】管理类端点 admin_required(与 /api/admin 同一把口令);
检索 /search 用户级(M4 落地)。

【端点清单】(M2 范围:库 CRUD / 模板 / 文档上传管理 / 图谱浏览 / 依赖状态)
  GET    /kbs                          知识库列表(含文档/实体/关系计数)
  GET    /kbs/templates                本体模板清单(建库下拉)
  GET    /kbs/tree                     层级树(目录+子库递归,左侧树渲染)
  POST   /kbs                          建库 {name, description, template, kind?, parent_id?}
  GET    /kbs/{id}                     库详情 + 图谱实时计数
  PUT    /kbs/{id}                     改库 {name?, description?, schema?}
  PUT    /kbs/{id}/move                移动节点 {parent_id: string|null}
  DELETE /kbs/{id}                     删库(联动 Neo4j/向量/文件/元数据;
                                       folder 需空,有子节点 422)
  GET    /kbs/{id}/documents           文档列表
  POST   /kbs/{id}/documents           上传(multipart files,多文件;查重跳过)
  DELETE /kbs/{id}/documents/{doc_id}  删文档(联动清理;导入任务 M3)
  GET    /kbs/{id}/graph               图谱浏览(限量节点+邻接边;过滤 q/types)
  GET    /kbs/{id}/graph/expand        点击节点增量展开(1 跳)
  GET    /kbs/{id}/stats               库统计(元数据 + 图谱/向量实时数)
  GET    /dependency-status            Neo4j/向量库探针结果(设置页重检用)
"""
import asyncio
import hashlib
import logging
import re
import shutil
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from sdk.pack_api import admin_required
from domains.knowledge_graph import runtime
from sdk.doc_parser import (
    allowed_extension, chunk_text, mime_for, parse_to_text,
    sub_chunks_for_embedding,
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


# ── D33 建库向导:分段参数(库级,键与插件设置同名;范围与
#    settings.schema.yaml 同源——改 schema 时同步这里) ─────────
_CHUNK_PARAM_RANGES = {
    "chunk_target_chars": (200, 8000),
    "chunk_overlap_chars": (0, 500),
    "chunk_max_chars": (500, 16000),
    "chunk_structural_max_chars": (1000, 50000),
    "vector_subchunk_chars": (300, 4000),
}


def _validated_chunk_params(payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """校验库级分段参数(422 拒非法;None=请求未带,保持现值)。"""
    raw = payload.get("chunk_params")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(422, "chunk_params 必须是对象")
    out: Dict[str, int] = {}
    for key, (lo, hi) in _CHUNK_PARAM_RANGES.items():
        if key not in raw:
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            raise HTTPException(422, f"chunk_params.{key} 须为整数")
        if not lo <= v <= hi:
            raise HTTPException(422, f"chunk_params.{key} 须在 {lo}~{hi}")
        out[key] = v
    unknown = set(raw) - set(_CHUNK_PARAM_RANGES)
    if unknown:
        raise HTTPException(422, f"未知的分段参数: {', '.join(sorted(unknown))}")
    return out


# ── 知识库 CRUD ─────────────────────────────────────────────

@router.get("/kbs", dependencies=[Depends(admin_required)])
async def list_kbs(request: Request):
    items = runtime.get_kg_store(request.app.state).list_kbs()
    # 库列表的实体/关系总数用图谱真实去重计数覆盖(store 的 SUM 是文档级
    # 抽取量之和,跨文档共享实体会被重复计——卡片数字虚高)。图不可达
    # 时回退到求和口径,不让列表接口因依赖失联而 500。
    try:
        graph = _graph(request)
        for kb in items:
            counts = graph.counts(kb["id"])
            kb["entityTotal"] = counts.get("entities", kb.get("entityTotal", 0))
            kb["relationTotal"] = counts.get("relations", kb.get("relationTotal", 0))
    except Exception as e:
        logger.warning(f"库列表图谱计数失败,回退文档求和口径: {e}")
    return {"items": items}


@router.get("/kbs/templates", dependencies=[Depends(admin_required)])
async def kb_templates(request: Request):
    return {"items": list_templates()}


@router.post("/kbs/chunk-preview", dependencies=[Depends(admin_required)])
async def chunk_preview(
    request: Request,
    file: UploadFile = File(...),
    target: int = Form(1200),
    overlap: int = Form(100),
    max_chars: int = Form(3000),
    structural_max: int = Form(10000),
    vector_sub: int = Form(500),
):
    """D33 建库向导:分段实时预览——与导入同一条切块代码路径
    (parse_to_text + chunk_text + sub_chunks_for_embedding),预览
    看到的块=导入落库的块(验收口径:逐块一致)。

    Body: multipart,file=文档 + 五个分段参数(Form)。
    Returns: {filename, textChars, chunks[{seq,charCount,preview,subChunks}], stats}
    """
    params = {
        "chunk_target_chars": target, "chunk_overlap_chars": overlap,
        "chunk_max_chars": max_chars, "chunk_structural_max_chars": structural_max,
        "vector_subchunk_chars": vector_sub,
    }
    for key, (lo, hi) in _CHUNK_PARAM_RANGES.items():
        if not lo <= int(params[key]) <= hi:
            raise HTTPException(422, f"{key} 须在 {lo}~{hi}")

    filename = _sanitize_filename(file.filename or "unnamed")
    if not allowed_extension(filename):
        raise HTTPException(422, "不支持的格式(仅 md/txt/pdf/docx)")
    settings = runtime.settings_reader(request.app.state)
    max_bytes = max(1, int(settings.get("max_upload_mb", 20))) * 1024 * 1024
    declared = int(file.size or 0)
    if declared and declared > max_bytes:
        raise HTTPException(422, f"超过单文件上限 {max_bytes // 1024 // 1024}MB")
    data = await file.read()
    if not data:
        raise HTTPException(422, "空文件")
    if len(data) > max_bytes:
        raise HTTPException(422, f"超过单文件上限 {max_bytes // 1024 // 1024}MB")
    try:
        text = parse_to_text(filename, data)
    except ValueError as e:
        raise HTTPException(422, f"解析失败: {e}")

    chunks = chunk_text(
        text,
        target_chars=params["chunk_target_chars"],
        overlap_chars=params["chunk_overlap_chars"],
        max_chars=params["chunk_max_chars"],
        structural_max_chars=params["chunk_structural_max_chars"],
    )
    items = []
    sub_total = 0
    for c in chunks:
        subs = sub_chunks_for_embedding(c["text"], params["vector_subchunk_chars"])
        sub_total += len(subs)
        items.append({
            "seq": c["seq"], "charCount": c["char_count"],
            "preview": c["text"][:200], "subChunks": len(subs),
        })
    lens = [c["char_count"] for c in chunks]
    return {
        "filename": filename, "textChars": len(text),
        "chunks": items,
        "stats": {
            "chunkCount": len(chunks), "subChunkTotal": sub_total,
            "minChars": min(lens) if lens else 0,
            "maxChars": max(lens) if lens else 0,
            "avgChars": (sum(lens) // len(lens)) if lens else 0,
        },
    }


@router.get("/kbs/tree", dependencies=[Depends(admin_required)])
async def kb_tree(request: Request):
    """层级树(10-15 用户需求:左侧需要是有层级的树)。

    list_kb_tree 直出:顶层列表,folder 节点带 children(递归);
    排序 folder 先于 kb、同级按名称;kb 节点带 doc/entity/relation 计数。
    """
    return {"items": runtime.get_kg_store(request.app.state).list_kb_tree()}


# ── 开放 API Key 管理(D25:宿主推数据;签发/吊销归管理门) ──

@router.get("/kbs/open-api-keys", dependencies=[Depends(admin_required)])
async def list_open_api_keys(request: Request):
    """Key 清单(不含哈希/明文——只有签发时返回一次明文)。"""
    return {"items": runtime.get_kg_store(request.app.state).list_open_api_keys()}


@router.post("/kbs/open-api-keys", dependencies=[Depends(admin_required)])
async def create_open_api_key(request: Request, payload: Dict[str, Any]):
    """签发 Key:明文 kgk_ 前缀 + 48 hex,仅本次返回(库内只存 SHA-256)。"""
    import secrets
    name = str(payload.get("name") or "").strip() or "未命名"
    kb_id = str(payload.get("kb_id") or "").strip() or None
    if kb_id and not runtime.get_kg_store(request.app.state).get_kb(kb_id):
        raise HTTPException(422, "kb_id 指向的知识库不存在")
    plaintext = "kgk_" + secrets.token_hex(24)
    from domains.knowledge_graph.open_api import _hash_key
    store = runtime.get_kg_store(request.app.state)
    rec = store.create_open_api_key(name, _hash_key(plaintext), kb_id=kb_id)
    logger.info(f"open api key issued: {rec['id'][:8]} (name={name})")
    return {**rec, "key": plaintext}


@router.delete("/kbs/open-api-keys/{key_id}", dependencies=[Depends(admin_required)])
async def revoke_open_api_key(key_id: str, request: Request):
    store = runtime.get_kg_store(request.app.state)
    if not store.revoke_open_api_key(key_id):
        raise HTTPException(404, "Key 不存在或已吊销")
    logger.info(f"open api key revoked: {key_id[:8]}")
    return {"success": True}


# ── D7 文档元数据(检索过滤维度;管理端维护,检索时下推) ─────

@router.put("/kbs/{kb_id}/documents/{doc_id}/metadata",
            dependencies=[Depends(admin_required)])
async def set_doc_metadata(kb_id: str, doc_id: str,
                           request: Request, payload: Dict[str, Any]):
    """设置文档元数据(全量替换;空对象=清除过滤维度)。

    Body: {metadata: {"部门":"销售","年份":2026}} 扁平键值;
    检索时宿主传 doc_filter 按此过滤(与 chat 的 pack_params 同义)。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    md = payload.get("metadata")
    if md is None:
        md = {}
    if not isinstance(md, dict):
        raise HTTPException(422, "metadata 必须是对象")
    clean = {}
    for k, v in md.items():
        key = str(k).strip()[:64]
        if not key:
            continue
        if isinstance(v, list):
            vals = [str(x).strip()[:128] for x in v if str(x).strip()]
            if vals:
                clean[key] = vals
        else:
            val = str(v).strip()[:128]
            if val:
                clean[key] = val
    store.set_doc_metadata(doc_id, kb_id, clean)
    logger.info(f"doc metadata set: {doc_id[:8]} ({len(clean)} 维)")
    return {"id": doc_id, "docMetadata": clean}


# ── 检索数据权限 ACL(嵌入模式;管理端维护,检索时生效) ──────

@router.get("/kbs/{kb_id}/acl", dependencies=[Depends(admin_required)])
async def get_kb_acl(kb_id: str, request: Request):
    """某库的检索 ACL:允许的用户/角色 + 可选字典(宿主同步来的候选)。

    空 ACL = 全开放(默认);options 供前端下拉选择(不做手输)。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    return {
        **store.get_kb_acl(kb_id),
        "options": {
            "users": store.list_subjects("user"),
            "roles": store.list_subjects("role"),
        },
    }


@router.put("/kbs/{kb_id}/acl", dependencies=[Depends(admin_required)])
async def set_kb_acl(kb_id: str, request: Request, payload: Dict[str, Any]):
    """全量替换某库的检索 ACL。清空两个列表 = 回到全开放。

    生效条件:嵌入模式下宿主传了 user_id(X-User-Id)或角色
    (pack_params.knowledge_graph.user_roles)才过滤;未传不过滤。
    """
    _kb_or_404(request, kb_id)

    def _subjects(key: str) -> List[str]:
        raw = payload.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HTTPException(422, f"{key} 必须是数组")
        out, seen = [], set()
        for item in raw:
            s = str(item or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    users, roles = _subjects("users"), _subjects("roles")
    runtime.get_kg_store(request.app.state).set_kb_acl(kb_id, users, roles)
    logger.info(f"kb acl set: {kb_id[:8]} (users={len(users)}, roles={len(roles)})")
    return {"users": users, "roles": roles}


# ── 数据权限候选字典(宿主同步;ACL 编辑页下拉选择的数据源) ──

@router.post("/subjects/{subject_type}", dependencies=[Depends(admin_required)])
async def sync_subjects(subject_type: str, request: Request, payload: Dict[str, Any]):
    """宿主同步用户/角色字典(全量替换)。

    Body: {items: [{id, name?}, ...]}——组织/人员/角色系统变更后由宿主
    推送;ACL 编辑页的下拉候选即来自这里。宿主侧删除的条目随之消失。
    subject_type: user | role。
    """
    if subject_type not in ("user", "role"):
        raise HTTPException(422, "subject_type 必须是 user|role")
    raw = payload.get("items")
    if not isinstance(raw, list):
        raise HTTPException(422, "items 必须是数组")
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "").strip()
        if sid:
            items.append({"id": sid, "name": str(it.get("name") or "").strip()})
    runtime.get_kg_store(request.app.state).sync_subjects(subject_type, items)
    logger.info(f"subjects synced: {subject_type} ({len(items)} 条)")
    return {"subjectType": subject_type, "count": len(items)}


@router.get("/subjects/{subject_type}", dependencies=[Depends(admin_required)])
async def list_subjects(subject_type: str, request: Request):
    if subject_type not in ("user", "role"):
        raise HTTPException(422, "subject_type 必须是 user|role")
    return {"items": runtime.get_kg_store(request.app.state).list_subjects(subject_type)}


# ── 标注回复 CRUD(D32:知识库标准答案;管理端维护,检索时短路) ──

@router.get("/kbs/{kb_id}/annotations", dependencies=[Depends(admin_required)])
async def list_kb_annotations(kb_id: str, request: Request):
    """某库的标注列表(管理端标注页;问题/答案/启停/来源)。"""
    _kb_or_404(request, kb_id)
    from domains.knowledge_graph import annotations as kg_annotations
    return {"items": kg_annotations.list_annotations(request.app.state, kb_id)}


@router.post("/kbs/{kb_id}/annotations", dependencies=[Depends(admin_required)])
async def create_kb_annotation(kb_id: str, request: Request, payload: Dict[str, Any]):
    """新增标注:问题向量化入库;下次相似提问(≥0.90)直接返回标准答案。

    Body: {question, answer, source?}(source 记来源如"点踩转化",纯溯源)
    """
    _kb_or_404(request, kb_id)
    question = str((payload or {}).get("question") or "").strip()
    answer = str((payload or {}).get("answer") or "").strip()
    if not question or not answer:
        raise HTTPException(422, "question 和 answer 不能为空")
    from domains.knowledge_graph import annotations as kg_annotations
    import asyncio
    item = await asyncio.to_thread(
        kg_annotations.upsert_annotation, request.app.state,
        kb_id, question, answer, None,
        str((payload or {}).get("source") or "").strip())
    logger.info(f"annotation created: {item['id'][:8]} (kb={kb_id[:8]})")
    return item


@router.put("/kbs/{kb_id}/annotations/{annotation_id}",
            dependencies=[Depends(admin_required)])
async def update_kb_annotation(kb_id: str, annotation_id: str,
                               request: Request, payload: Dict[str, Any]):
    """更新标注(问题重打向量)/启停。Body: {question?, answer?, enabled?}。"""
    _kb_or_404(request, kb_id)
    from domains.knowledge_graph import annotations as kg_annotations
    import asyncio
    existing = next((a for a in kg_annotations.list_annotations(
        request.app.state, kb_id) if a["id"] == annotation_id), None)
    if not existing:
        raise HTTPException(404, "标注不存在")
    enabled = payload.get("enabled")
    if enabled is not None and "question" not in payload and "answer" not in payload:
        # 纯启停:不重打向量
        ok = await asyncio.to_thread(
            kg_annotations.set_enabled, request.app.state,
            kb_id, annotation_id, bool(enabled))
        if not ok:
            raise HTTPException(404, "标注不存在")
        return {"id": annotation_id, "enabled": bool(enabled)}
    question = str(payload.get("question") or existing["question"]).strip()
    answer = str(payload.get("answer") or existing["answer"]).strip()
    if not question or not answer:
        raise HTTPException(422, "question 和 answer 不能为空")
    item = await asyncio.to_thread(
        kg_annotations.upsert_annotation, request.app.state,
        kb_id, question, answer, annotation_id,
        existing.get("source") or "")
    if enabled is not None:
        await asyncio.to_thread(
            kg_annotations.set_enabled, request.app.state,
            kb_id, annotation_id, bool(enabled))
    return item


@router.delete("/kbs/{kb_id}/annotations/{annotation_id}",
               dependencies=[Depends(admin_required)])
async def delete_kb_annotation(kb_id: str, annotation_id: str, request: Request):
    _kb_or_404(request, kb_id)
    from domains.knowledge_graph import annotations as kg_annotations
    import asyncio
    if not await asyncio.to_thread(
            kg_annotations.delete_annotation, request.app.state,
            kb_id, annotation_id):
        raise HTTPException(404, "标注不存在")
    return {"success": True}


# ── 图谱浏览 ─────────────────────────────────────────────────


@router.post("/kbs", dependencies=[Depends(admin_required)])
async def create_kb(request: Request, payload: Dict[str, Any]):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "知识库名称不能为空")
    description = str(payload.get("description") or "").strip()
    template = str(payload.get("template") or "general").strip()
    # 层级树(10-15):kind=kb|folder(默认 kb);parent_id 挂目录下(默认顶层)。
    # folder 是纯分组节点——不套本体模板;合法性(父存在/父是 folder/
    # kind 取值)由 store.create_kb 校验,ValueError → 422。
    kind = str(payload.get("kind") or "kb").strip()
    parent_id = payload.get("parent_id") or None
    # D33 建库向导:库级分段参数(仅 kb 有意义;folder 忽略)
    chunk_params = _validated_chunk_params(payload) if kind == "kb" else None

    store = runtime.get_kg_store(request.app.state)
    if store.get_kb_by_name(name):
        raise HTTPException(409, f"知识库「{name}」已存在")
    try:
        kb = store.create_kb(
            name=name, description=description,
            schema_json=get_template_schema(template) if kind == "kb" else None,
            schema_template=template if kind == "kb" else "",
            kind=kind, parent_id=str(parent_id) if parent_id else None,
            chunk_params=chunk_params,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 向量口径建库预填:本地 bge-m3 定死 dim=1024(10-15 裁决),新库
    # 建好即显示"向量 dim=1024"不再"待导入探测";其它模型(api 嵌入/
    # 测试替身)维度未知,留空由首次导入探测回填(与 _prepare_vector
    # 同一套模型口径,不会误触模型一致性降级)
    try:
        from domains.knowledge_graph import tasks as kg_tasks
        if kind == "kb" and kb.get("vectorEnabled"):
            model = kg_tasks.embed_model_for(request.app.state)
            if model == "bge-m3" and not kb.get("vectorDim"):
                store.set_kb_vector_info(kb["id"], model,
                                         kg_tasks._LOCAL_EMBED_DIM, True)
                kb = store.get_kb(kb["id"])
    except Exception as e:
        logger.warning(f"建库向量口径预填失败(不影响建库): {e}")
    logger.info(f"kb created: {kb['id']} ({name}, template={template}, kind={kind})")
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
        chunk_params=_validated_chunk_params(payload),
    )
    return store.get_kb(kb_id)


@router.put("/kbs/{kb_id}/move", dependencies=[Depends(admin_required)])
async def move_kb(kb_id: str, request: Request, payload: Dict[str, Any]):
    """移动节点(换父目录/提顶层)。body: {parent_id: string|null}。

    合法性(父存在/父是 folder/不成环)由 store.move_kb 校验,
    ValueError → 422;返回移动后的节点(前端据此刷新树)。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    parent_id = payload.get("parent_id") or None
    try:
        moved = store.move_kb(kb_id, str(parent_id) if parent_id else None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not moved:
        raise HTTPException(404, "知识库不存在")
    return store.get_kb(kb_id)


@router.delete("/kbs/{kb_id}", dependencies=[Depends(admin_required)])
async def delete_kb(kb_id: str, request: Request):
    kb = _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)

    # 目录删除守卫:非空目录拒绝——直接删会让子节点 parentId 悬空成
    # 孤儿(树里再也挂不上)。先移出/删光子节点再删目录。
    if kb.get("kind") == "folder":
        children = [k for k in store.list_kbs() if k.get("parentId") == kb_id]
        if children:
            raise HTTPException(
                422, f"目录下还有 {len(children)} 个子节点,请先移出或删除后再删除目录")
        store.delete_kb(kb_id)
        logger.info(f"folder deleted: {kb_id}")
        return {"success": True, "cleanupErrors": []}

    # 删除守卫:有进行中的导入/归纳任务时拒绝——任务与删除并发会让
    # upsert 在清理之后回写,产出再也触达不了的孤儿子图。等任务结束再删。
    from domains.knowledge_graph import tasks as kg_tasks
    for doc in store.list_documents(kb_id):
        task_id = kg_tasks._inflight_task_id(request.app.state, doc["id"])
        if task_id:
            raise HTTPException(
                409, f"库内有正在导入的文档(任务 {task_id[:8]}…),请等任务结束后再删除")

    # 三存储 + 文件联动清理(单点失败不阻断:后续残留可再删一次)
    errors: List[str] = []
    try:
        _graph(request).delete_scope(kb_id)
    except Exception as e:
        errors.append(f"neo4j: {e}")
    try:
        if kb.get("vectorEnabled"):
            runtime.get_vector(request.app.state).drop_collection(kb_id)
    except Exception as e:
        errors.append(f"vector: {e}")
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
        # 分块读取 + 上限截断:Content-Length 只做预检(可伪造),真正的
        # 上限在读取中强制——超限时读到 max+1 即停,GB 级恶意上传不会
        # 先占满内存再被拒绝
        declared = int(f.size or 0)
        if declared and declared > max_bytes:
            results.append({"filename": f.filename, "ok": False,
                            "reason": f"超过单文件上限 {max_bytes // 1024 // 1024}MB"})
            continue
        data = b""
        oversized = False
        while True:
            part = await f.read(1024 * 1024)
            if not part:
                break
            data += part
            if len(data) > max_bytes:
                oversized = True
                break
        if oversized:
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
    return delete_document_full(request, kb_id, doc_id)


def delete_document_full(request: Request, kb_id: str, doc_id: str) -> Dict[str, Any]:
    """删文档(联动清理图谱/向量/文件)。管理端与开放 API(D25)共用
    同一条实现——清理语义只有一份,不会两边漂移。"""
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")

    # 删除守卫:导入进行中拒绝——否则任务后续批次的 upsert 会在清理之后
    # 回写,留下 source_docs 指向已删文档的孤儿实体(取消/等待后重删即可)
    from domains.knowledge_graph import tasks as kg_tasks
    inflight_task = kg_tasks._inflight_task_id(request.app.state, doc_id)
    if inflight_task:
        raise HTTPException(
            409, f"该文档正在导入中(任务 {inflight_task[:8]}…),请等任务结束或先取消任务")

    errors: List[str] = []
    # 图谱贡献清理。failed 也必须清:失败文档按设计保留已完成块的贡献,
    # 删文档不清图 = 永久孤儿(元数据已删,再也触达不了清理)。
    # uploaded = 从未导入过,图谱无贡献,跳过。
    imported_states = ("succeeded", "partial", "importing", "failed")
    if doc["importStatus"] in imported_states:
        try:
            _graph(request).delete_document(kb_id, doc_id)
        except Exception as e:
            errors.append(f"neo4j: {e}")
    # 向量清理(向量启用且导入过的库)
    kb = store.get_kb(kb_id)
    try:
        if kb and kb.get("vectorEnabled") and doc["importStatus"] in imported_states:
            runtime.get_vector(request.app.state).delete_by_doc(kb_id, doc_id)
    except Exception as e:
        errors.append(f"vector: {e}")
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


@router.get("/kbs/{kb_id}/documents/{doc_id}/chunks", dependencies=[Depends(admin_required)])
async def list_chunks(kb_id: str, doc_id: str, request: Request):
    """文档块级明细(导入状态可视 + 定向重试的依据)。

    返回每块 seq/status/字数/预览 + 汇总计数。批次是执行时的动态分组,
    持久状态在块级——前端可按 batch_size 分组展示,但重试以块为单位。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    chunks = store.list_chunks(doc_id)
    items = [{
        "id": c["id"], "seq": c["seq"], "status": c["status"],
        "charCount": c["charCount"],
        # D29 块命中统计:被 LLM 引用次数("哪些知识真被用")。
        # store 层已是 camelCase 键(审查修复:此前读 snake 键恒 0)
        "hitCount": int(c.get("hitCount") or 0),
        "preview": (c["text"] or "").strip().replace("\n", " ")[:80],
    } for c in chunks]
    summary = {
        "total": len(items),
        "done": sum(1 for c in items if c["status"] == "done"),
        "failed": sum(1 for c in items if c["status"] == "failed"),
        "pending": sum(1 for c in items if c["status"] == "pending"),
    }
    return {"items": items, "summary": summary}


@router.get("/kbs/{kb_id}/documents/{doc_id}/chunks/{chunk_id}/extraction",
            dependencies=[Depends(admin_required)])
async def chunk_extraction(kb_id: str, doc_id: str, chunk_id: str, request: Request):
    """单块的抽取产出(实体 + 关系)——块明细"查看产出"用。

    实体经锚定过滤与本体制约后入库、带块级溯源(source_chunks);空产出
    = 该块未抽取/内容被过滤(如目录块)。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    if not any(c["id"] == chunk_id for c in store.list_chunks(doc_id)):
        raise HTTPException(404, "块不存在或不属于该文档")
    return _graph(request).chunk_output(kb_id, chunk_id)


@router.get("/kbs/{kb_id}/documents/{doc_id}/chunks/{chunk_id}",
            dependencies=[Depends(admin_required)])
async def chunk_text_full(kb_id: str, doc_id: str, chunk_id: str, request: Request):
    """单块全文(D27 块编辑的编辑器数据源;列表接口只给 80 字预览)。"""
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    chunk = next((c for c in store.list_chunks(doc_id) if c["id"] == chunk_id), None)
    if not chunk:
        raise HTTPException(404, "块不存在或不属于该文档")
    return {"id": chunk["id"], "seq": chunk["seq"], "text": chunk["text"],
            "charCount": chunk["charCount"], "status": chunk["status"]}


@router.put("/kbs/{kb_id}/documents/{doc_id}/chunks/{chunk_id}",
            dependencies=[Depends(admin_required)])
async def update_chunk(kb_id: str, doc_id: str, chunk_id: str,
                       request: Request, payload: Dict[str, Any]):
    """D27 块编辑:改文本 → 仅重建该块向量(增量重索引,未编辑块零重算)。

    语义边界(第一版):向量/全文即时同步(search_text 生成列随 UPDATE
    重算);**图谱不动**——该块抽取出的实体/关系仍是旧文本的,需「重导」
    全量重建(前端界面警示同口径)。向量重建失败不回滚文本:如实返回
    vectorUpdated=false + warning(文本已生效,向量是旧的——比静默假
    同步诚实)。
    """
    _kb_or_404(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    chunk = next((c for c in store.list_chunks(doc_id) if c["id"] == chunk_id), None)
    if not chunk:
        raise HTTPException(404, "块不存在或不属于该文档")
    if chunk["seq"] < 0:
        # D14 摘要块(seq=-1):导入收尾自动生成/覆盖,手工编辑会被下次
        # 重导冲掉——如实拒绝而非静默接受
        raise HTTPException(422, "摘要块由导入自动维护,不支持手工编辑(重导即重新生成)")

    # 并发守卫(同删除端点口径):导入进行中拒绝编辑——导入任务用的是
    # 启动时快照的旧文本,编辑重嵌之后任务批次回写会把旧文本向量写回,
    # 文本/向量/图谱三方漂移且不可自愈
    from domains.knowledge_graph import tasks as kg_tasks
    inflight = kg_tasks._inflight_task_id(request.app.state, doc_id)
    if inflight:
        raise HTTPException(
            409, f"该文档正在导入中(任务 {inflight[:8]}…),请等任务结束再编辑")

    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "块文本不能为空")
    if len(text) > 50000:
        raise HTTPException(422, "块文本超长(上限 50000 字,请拆分)")

    store.update_chunk_text(chunk_id, text)
    logger.info(f"chunk edited: {chunk_id[:8]} (doc={doc_id[:8]}, "
                f"{chunk['charCount']}→{len(text)} chars)")

    # 增量重嵌:只动这一个块的子段向量
    kb = store.get_kb(kb_id)
    warning = None
    vector_updated = False
    if kb and kb.get("vectorEnabled"):
        try:
            vector_updated = await asyncio.to_thread(
                _reembed_chunk, request.app.state, kb, doc_id, chunk_id, text)
        except Exception as e:
            logger.exception(f"块编辑后向量重建失败: {chunk_id[:8]}")
            warning = f"文本已保存,但向量重建失败({e})——检索暂用旧向量,可重试编辑或重导"
    else:
        warning = "该库向量未启用,仅更新了文本与全文索引"

    fresh = next((c for c in store.list_chunks(doc_id) if c["id"] == chunk_id), None)
    return {"chunk": fresh, "vectorUpdated": vector_updated, "warning": warning}


def _reembed_chunk(app_state, kb: Dict[str, Any], doc_id: str,
                   chunk_id: str, text: str) -> bool:
    """单块向量增量重建:重子切→重嵌→upsert→清残留旧子段。

    子段 id 形如 {chunk_id}#i 确定性可算;新文本子段数可能变少,残留的
    旧 id 必须显式删除(delete_by_ids),否则旧向量永远参与召回。
    """
    import time as _time
    from domains.knowledge_graph import tasks as kg_tasks
    from sdk.doc_parser import sub_chunks_for_embedding
    t0 = _time.monotonic()
    sub_size = max(300, kg_tasks.chunk_params_for(app_state, kb)["vector_subchunk_chars"])
    vector = runtime.get_vector(app_state)
    old_ids = {i for i in vector.existing_chunk_ids(kb["id"], doc_id)
               if i.startswith(f"{chunk_id}#")}
    subs = sub_chunks_for_embedding(text, sub_size)
    rows = [{"chunk_id": f"{chunk_id}#{i}", "doc_id": doc_id,
             "seq": 0, "text": sub} for i, sub in enumerate(subs)]
    vectors = app_state.llm_client.embeddings(
        [r["text"] for r in rows], conv_id=None, stage="kg.embed")
    vector.upsert_chunks(kb["id"], [{**r, "vector": vec} for r, vec in zip(rows, vectors)])
    stale = old_ids - {r["chunk_id"] for r in rows}
    if stale:
        vector.delete_by_ids(kb["id"], sorted(stale))
    logger.info(f"chunk reembedded: {chunk_id[:8]} ({len(rows)} 段, "
                f"清残留 {len(stale)}, {_time.monotonic() - t0:.1f}s)")
    return True


@router.post("/kbs/{kb_id}/documents/{doc_id}/import", dependencies=[Depends(admin_required)])
async def import_document(kb_id: str, doc_id: str, request: Request, payload: Dict[str, Any] = None):
    """发起单文档导入(后台任务;同库串行,返回任务 ID 供任务中心/页面跟踪)。

    body 可选 chunk_ids: 定向重抽指定块(仅非 done 块生效;done 块的图谱
    贡献在库,重抽破坏一致性,换本体请走 force 全量)。
    """
    _kb_or_404(request, kb_id)
    _task_manager_or_503(request)
    payload = payload or {}
    from domains.knowledge_graph import tasks
    try:
        task = tasks.submit_import(
            request.app.state, kb_id, doc_id, force=bool(payload.get("force")),
            chunk_ids=[str(c) for c in (payload.get("chunk_ids") or [])],
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

    Body: {kb: 知识库名/ID/数组(可选), query: 问题, top_k?: 向量条数,
           roles?: 角色数组(嵌入模式数据权限;与 chat 的
           pack_params.knowledge_graph.user_roles 同义)}
      - kb 字符串:单库检索(名或 ID;不存在 404——回显的是用户自己的输入;
        无 ACL 权限 403)
      - kb 数组:跨指定库检索;不存在的名字**静默跳过**(不 404 泄露探测
        结果,防枚举口径:只回显存在的库名);无权限的库同款静默跳过;
        全部不可用才 404
      - kb 缺省:全部库跨库检索(N3 跨库默认;排除 folder 目录节点;
        传了身份则只检索有权限的库)
    """
    query = str((payload or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(422, "query 不能为空")
    kb_ref = (payload or {}).get("kb")
    top_k = payload.get("top_k")

    store = runtime.get_kg_store(request.app.state)
    # 数据权限身份:X-User-Id(头) + roles(体;嵌入模式宿主传入才生效)
    from domains.knowledge_graph import acl as kg_acl
    from sdk.pack_api import user_id
    perm_user, perm_roles = kg_acl.request_identity(user_id(request), payload.get("roles"))
    perm_on = kg_acl.has_identity(perm_user, perm_roles)
    target: List[Dict[str, Any]] = []
    if isinstance(kb_ref, list):
        refs = [str(r).strip() for r in kb_ref if str(r or "").strip()]
        if not refs:
            raise HTTPException(422, "kb 数组不能为空")
        seen_ids = set()
        for ref in refs:
            kb = store.get_kb_by_name(ref) or store.get_kb(ref)
            # folder 目录不存文档不可检索,与不存在的名字同款静默跳过;
            # 无权限的库同款静默跳过(防枚举口径一致)
            if (kb and kb.get("kind") != "folder" and kb["id"] not in seen_ids
                    and (not perm_on or kg_acl.kb_allowed(
                        store, kb["id"], perm_user, perm_roles))):
                seen_ids.add(kb["id"])
                target.append(kb)
        if not target:
            # 全部不可用:只说不存在,不回显任何库名(防枚举)
            raise HTTPException(404, "知识库不存在")
    elif kb_ref is not None and str(kb_ref).strip():
        ref = str(kb_ref).strip()
        kb = store.get_kb_by_name(ref) or store.get_kb(ref)
        if not kb:
            raise HTTPException(404, f"知识库不存在: {ref}")
        if perm_on and not kg_acl.kb_allowed(store, kb["id"], perm_user, perm_roles):
            raise HTTPException(403, f"无权访问知识库: {ref}")
        target = [kb]
    else:
        target = [k for k in store.list_kbs() if k.get("kind") != "folder"]
        if perm_on:
            target, _denied = kg_acl.filter_kbs(store, target, perm_user, perm_roles)
        if not target:
            raise HTTPException(
                404, "当前没有您可访问的知识库" if perm_on else "当前没有任何知识库")

    from domains.knowledge_graph import retrieval
    from sdk.pack_api import user_id
    user = user_id(request)
    # conv_id 可选:传入则检索调用日志(意图/图/向量/回答)关联该会话,
    # 管理端链路视图里能看到完整检索链;不传保持无会话归属
    conv_id = str((payload or {}).get("conv_id") or "").strip() or None
    # D7 文档元数据过滤(body doc_filter;与 chat 的 pack_params 同义):
    # 扁平键值 AND 匹配,逐目标库解析白名单后聚合
    doc_filter_raw = (payload or {}).get("doc_filter")
    doc_filter = None
    if isinstance(doc_filter_raw, dict) and doc_filter_raw:
        doc_filter = set()
        for k in target:
            ids = store.filter_doc_ids_by_metadata(k["id"], doc_filter_raw)
            doc_filter |= (ids if ids is not None else set())

    # D32 标注短路(前置层):标注命中直接返回标准答案(不走检索)。
    # 与 kb_search 工具同层;REST 供宿主/前端直调路径也吃同一红利
    import asyncio
    from domains.knowledge_graph import annotations as kg_annotations
    try:
        hit = None
        hit_kb = None
        for k in target:
            hit = await asyncio.to_thread(
                kg_annotations.match_annotation, request.app.state, k["id"], query)
            if hit:
                hit_kb = k
                break
        if hit:
            logger.info(f"annotation hit: kb={hit_kb['name']}, "
                        f"score={hit['score']}, q={query[:40]}")
            return {
                "answer": hit["answer"],
                "annotationHit": True,
                "annotationQuestion": hit["question"],
                "annotationScore": hit["score"],
                "cited_chunks": [],
                "kb": {"id": hit_kb["id"], "name": hit_kb.get("name") or ""},
                "kbs": [{"id": hit_kb["id"], "name": hit_kb.get("name") or ""}],
                "intent": None,
                "subgraph": {"nodes": [], "edges": []},
                "chunks": [],
                "sources": {"entities": [], "chunks": []},
                "user": user,
            }
    except Exception as e:
        logger.warning(f"标注短路层异常(降级常规检索): {e}")

    try:
        # 检索链含多次 LLM/图/向量同步调用(LLM_TIMEOUT 默认 300s),
        # 直接在 async 端点里跑会阻塞整个事件循环(其他会话的 SSE 心跳
        # 一起冻结)——丢线程池执行。单库走 answer_question(既有路径
        # 零改动),多库走 answer_question_multi(意图一次/合并策略)
        import asyncio
        if len(target) == 1:
            result = await asyncio.to_thread(
                retrieval.answer_question,
                request.app.state, target[0], query, conv_id=conv_id, top_k=top_k,
                doc_filter=doc_filter)
            # 单库也补 kbs 键:响应形状与多库一致(前端/测试统一消费,
            # 不必按库数分支)
            result.setdefault(
                "kbs", [{"id": target[0]["id"],
                         "name": target[0].get("name") or ""}])
        else:
            result = await asyncio.to_thread(
                retrieval.answer_question_multi,
                request.app.state, target, query, conv_id=conv_id, top_k=top_k,
                doc_filter=doc_filter)
        return result | {"user": user}
    except Exception as e:
        # 异常详情只进服务端日志;用户级端点不回显内部错误串(可能带
        # 中间件地址等内部拓扑)
        logger.exception("search failed")
        raise HTTPException(502, "检索失败,请稍后重试或联系管理员")


# ── 检索测试(管理端:两路召回明细可视化,retrieve-only) ─────────

@router.post("/kbs/{kb_id}/retrieve-test", dependencies=[Depends(admin_required)])
async def kb_retrieve_test(request: Request, kb_id: str, payload: Dict[str, Any]):
    """检索测试(管理端,DocManager 检索测试页):两路召回明细可视化。

    retrieve-only:直接跑 hybrid_retrieve,不做 LLM 综合回答(意图解析
    kg.query 仍走——它是检索的一部分)。种子命中/截断水位/向量分数是
    内部排查数据,挂管理门内,不进用户级 /search(防枚举口径同源)。

    Body: {query: 问题, top_k?: 向量条数}
    """
    query = str((payload or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(422, "query 不能为空")
    top_k = payload.get("top_k")

    store = runtime.get_kg_store(request.app.state)
    kb = store.get_kb(kb_id)
    if not kb:
        raise HTTPException(404, f"知识库不存在: {kb_id}")

    from domains.knowledge_graph import retrieval
    import asyncio
    try:
        result = await asyncio.to_thread(
            retrieval.hybrid_retrieve, request.app.state, kb, query,
            None, top_k)
    except Exception:
        # 500 回体是通用文案不泄内部串;堆栈进服务端日志(与 /search 同款纪律)
        logger.exception(f"retrieve-test failed: kb={kb_id}")
        raise
    return result | {"kb": {"id": kb["id"], "name": kb.get("name") or ""}}


# ── 依赖状态(设置页"重新检测"用) ─────────────────────────────

@router.get("/dependency-status", dependencies=[Depends(admin_required)])
async def dependency_status(request: Request):
    """对 Neo4j/向量存储各跑一次探针(结果即时,不走缓存)。

    向量探针调 SDK probe_pg_vector(P0-1 上提;本插件不再自持实现);
    Neo4j 探针为 KG 专属(P2:图模型领域知识留插件)。
    """
    from domains.knowledge_graph import probes
    from sdk.vector_store import probe_pg_vector
    settings = runtime.settings_reader(request.app.state).all()

    def _run(fn) -> Dict[str, Any]:
        try:
            fn(settings)
            return {"ok": True, "detail": ""}
        except Exception as e:
            return {"ok": False, "detail": str(e) or type(e).__name__}

    return {
        "neo4j": _run(probes.neo4j),
        "pg_vector": _run(probe_pg_vector),
    }
