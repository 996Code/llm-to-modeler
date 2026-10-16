"""知识库开放 API(D25)—— 宿主/外部系统推数据进知识库。

【定位】njmind 宿主或其它系统想把文档灌进我们的知识库,此前只有管理端
手工上传。本模块提供带 API Key 鉴权的 REST(挂载于 /api/open/kb,平台
级路由——AuthMiddleware 对 /api/open/ 前缀放行,由本模块的 API Key
依赖独自设门):

  POST   /kb/{kb_id}/documents          上传 + 自动触发导入(幂等:同库同
                                        内容已存在则跳过上传与导入)
  PUT    /kb/{kb_id}/documents/{doc_id} 更新内容 → 强制重导(图谱/向量重建)
  DELETE /kb/{kb_id}/documents/{doc_id} 删文档(联动清理图谱/向量/文件)

【鉴权】X-Api-Key 头(或 Authorization: Bearer)。Key 由管理端签发
(api.py /kbs/open-api-keys,admin 门),库内只存 SHA-256 哈希;吊销
即失效。校验通过刷新 last_used_at(审计)。

【幂等语义】同库同内容(content_hash)已存在 → 返回既有文档 +
skipped=true,不重复上传不重复导入(宿主重试/对账推送安全)。
"""
import hashlib
import logging
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from domains.knowledge_graph import runtime
from sdk.doc_parser import allowed_extension, mime_for, parse_to_text

logger = logging.getLogger(__name__)

router = APIRouter()


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _extract_api_key(request: Request) -> str:
    key = (request.headers.get("X-Api-Key") or "").strip()
    if not key:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    return key


async def api_key_required(request: Request) -> Dict[str, Any]:
    """开放 API 鉴权:API Key 哈希查库,吊销即拒。401 文案区分无效/吊销
    (宿主排障不需要更多信息;Key 本身不回显)。"""
    key = _extract_api_key(request)
    if not key:
        raise HTTPException(401, "缺少 API Key(X-Api-Key 头)")
    store = runtime.get_kg_store(request.app.state)
    rec = store.find_open_api_key_by_hash(_hash_key(key))
    if not rec:
        raise HTTPException(401, "API Key 无效")
    if rec["revokedAt"]:
        raise HTTPException(401, "API Key 已吊销")
    store.touch_open_api_key(rec["id"])
    return rec


def _read_validated_file(request: Request, file: UploadFile) -> Tuple[str, bytes]:
    """读上传文件 + 类型/大小/解析校验(与管理端上传同口径)。

    返回 (filename, data);非法当场 422(与上传预检一致:坏文件不进
    导入队列再炸)。
    """
    from domains.knowledge_graph.api import _sanitize_filename
    filename = _sanitize_filename(file.filename or "unnamed")
    if not allowed_extension(filename):
        raise HTTPException(422, "不支持的格式(仅 md/txt/pdf/docx)")
    settings = runtime.settings_reader(request.app.state)
    max_bytes = max(1, int(settings.get("max_upload_mb", 20))) * 1024 * 1024
    data = b""
    while True:
        part = file.file.read(1024 * 1024)
        if not part:
            break
        data += part
        if len(data) > max_bytes:
            raise HTTPException(422, f"超过单文件上限 {max_bytes // 1024 // 1024}MB")
    if not data:
        raise HTTPException(422, "空文件")
    try:
        parse_to_text(filename, data)
    except ValueError as e:
        raise HTTPException(422, f"解析失败: {e}")
    return filename, data


def _kb_or_404_open(request: Request, kb_id: str) -> Dict[str, Any]:
    kb = runtime.get_kg_store(request.app.state).get_kb(kb_id)
    if not kb:
        raise HTTPException(404, "知识库不存在")
    return kb


def _write_doc_file(request: Request, kb_id: str, doc_id: str,
                    filename: str, data: bytes) -> str:
    doc_dir = runtime.files_root() / kb_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    from domains.knowledge_graph.api import _ext_of
    file_path = doc_dir / f"{doc_id}{_ext_of(filename)}"
    file_path.write_bytes(data)
    return str(file_path)


@router.post("/kb/{kb_id}/documents", dependencies=[Depends(api_key_required)])
async def push_document(kb_id: str, request: Request, file: UploadFile = File(...)):
    """推送文档:上传 + 自动触发导入。幂等(同库同内容跳过)。"""
    _kb_or_404_open(request, kb_id)
    filename, data = _read_validated_file(request, file)
    store = runtime.get_kg_store(request.app.state)

    content_hash = hashlib.sha256(data).hexdigest()
    dup = store.get_document_by_hash(kb_id, content_hash)
    if dup:
        # 幂等:同内容已在库中(可能已导入/导入中)——不重复上传不重复
        # 触发导入,返回既有文档 + skipped 标记(宿主重试/对账安全)
        return {"skipped": True, "reason": "同内容文件已存在",
                "document": dup, "duplicateOf": dup["id"]}

    doc = store.create_document(
        kb_id=kb_id, filename=filename, mime_type=mime_for(filename),
        size_bytes=len(data), file_path="", content_hash=content_hash)
    file_path = _write_doc_file(request, kb_id, doc["id"], filename, data)
    store.update_document(doc["id"], file_path=file_path)

    task = _submit_import(request, kb_id, doc["id"], force=False)
    logger.info(f"open api push: {doc['id']} ({filename}, {len(data)}B, "
                f"kb={kb_id[:8]}, task={task['id'][:8]})")
    return {"skipped": False, "document": doc, "task": task}


@router.put("/kb/{kb_id}/documents/{doc_id}", dependencies=[Depends(api_key_required)])
async def update_document(kb_id: str, doc_id: str, request: Request,
                          file: UploadFile = File(...)):
    """更新文档内容 → 强制重导(切块/图谱/向量全量重建)。"""
    _kb_or_404_open(request, kb_id)
    store = runtime.get_kg_store(request.app.state)
    doc = store.get_document(doc_id)
    if not doc or doc["kbId"] != kb_id:
        raise HTTPException(404, "文档不存在")
    filename, data = _read_validated_file(request, file)

    content_hash = hashlib.sha256(data).hexdigest()
    dup = store.get_document_by_hash(kb_id, content_hash)
    if dup and dup["id"] != doc_id:
        raise HTTPException(409, f"同内容文件已存在: {dup['filename']}")

    file_path = _write_doc_file(request, kb_id, doc_id, filename, data)
    store.update_document(doc_id, file_path=file_path, content_hash=content_hash,
                          size_bytes=len(data), import_status="uploaded", error="",
                          filename=filename, mime_type=mime_for(filename))

    task = _submit_import(request, kb_id, doc_id, force=True)
    logger.info(f"open api update: {doc_id} ({filename}, {len(data)}B, force reimport)")
    return {"skipped": False, "document": store.get_document(doc_id), "task": task}


@router.delete("/kb/{kb_id}/documents/{doc_id}", dependencies=[Depends(api_key_required)])
async def delete_document(kb_id: str, doc_id: str, request: Request):
    """删文档(联动清理图谱/向量/文件——与管理端同一条删除实现)。"""
    _kb_or_404_open(request, kb_id)
    from domains.knowledge_graph.api import delete_document_full
    return delete_document_full(request, kb_id, doc_id)


def _submit_import(request: Request, kb_id: str, doc_id: str, force: bool) -> Dict:
    from domains.knowledge_graph import tasks as kg_tasks
    try:
        return kg_tasks.submit_import(request.app.state, kb_id, doc_id, force=force)
    except ValueError as e:
        # 重复任务等业务冲突:文档已上传,冲突信息如实回(宿主可对账)
        raise HTTPException(409, str(e))
