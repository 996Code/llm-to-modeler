"""metrology_assistant 场景配置管理 API(挂 /api/packs/metrology_assistant)。

P1 管理页数据面(设计 §8.1/§9 Tab1/Tab2 的保存语义):
  GET  /config          当前生效配置 + revision(管理页加载)
  PUT  /config          整档保存(expected_revision CAS;409 冲突/400 校验)
  GET  /config/history  历史快照列表(元信息)
  GET  /config/history/{rev}  某版本完整快照(回滚预览/差异对比)
  POST /config/rollback 回滚旧快照到新 revision

保存成功即失效运行时缓存——下一次对话请求读到新配置(热生效);
校验在保存前(scenario_config.validate_config),坏配置不入库。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sdk.pack_api import admin_required

from domains.metrology_assistant import runtime, store
from domains.metrology_assistant.scenario_config import (
    ConfigValidationError, factory_default_config, validate_config,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ConfigSaveBody(BaseModel):
    content: dict
    expected_revision: int
    comment: str = ""


class RollbackBody(BaseModel):
    to_revision: int
    comment: str = ""


def _db():
    return runtime.get_pack_db()


def _user(request: Request) -> str:
    return getattr(request.state, "user_id", None) or "admin"


@router.get("/config", dependencies=[Depends(admin_required)])
async def get_config() -> dict:
    """当前生效配置(无行=尚未种子,返回出厂默认与 revision 0)。"""
    config = store.load_config(_db())
    if config is None:
        default = factory_default_config()
        return {"revision": 0, "content": default,
                "factory": True}
    revision = config.pop("_revision")
    return {"revision": revision, "content": config, "factory": False}


@router.put("/config", dependencies=[Depends(admin_required)])
async def put_config(body: ConfigSaveBody, request: Request) -> dict:
    """整档保存:校验 → CAS → 失效缓存(热生效)。

    400=结构校验失败;409=revision 冲突(他人已改,请刷新)。
    """
    try:
        validate_config(body.content)
    except ConfigValidationError as e:
        raise HTTPException(400, f"场景配置校验失败: {e}")
    try:
        new_rev = store.save_config(
            _db(), body.content, body.expected_revision,
            updated_by=_user(request), comment=body.comment)
    except store.ConfigConflictError as e:
        raise HTTPException(409, str(e))
    runtime.invalidate_config_cache()
    return {"revision": new_rev}


@router.get("/config/history", dependencies=[Depends(admin_required)])
async def get_history(limit: int = 20) -> dict:
    return {"items": store.list_config_history(_db(), limit=limit)}


@router.get("/config/history/{revision}", dependencies=[Depends(admin_required)])
async def get_history_entry(revision: int) -> dict:
    entry = store.get_config_history_entry(_db(), revision)
    if entry is None:
        raise HTTPException(404, f"历史快照 revision {revision} 不存在")
    return entry


@router.post("/config/rollback", dependencies=[Depends(admin_required)])
async def rollback(body: RollbackBody, request: Request) -> dict:
    """回滚:旧快照经验证后生成新 revision(不改历史)。"""
    try:
        entry = store.get_config_history_entry(_db(), body.to_revision)
        if entry is None:
            raise HTTPException(
                404, f"历史快照 revision {body.to_revision} 不存在")
        validate_config(entry["content"])
    except ConfigValidationError as e:
        raise HTTPException(400, f"历史快照校验失败(不可回滚): {e}")
    new_rev = store.rollback_config(
        _db(), body.to_revision, updated_by=_user(request),
        comment=body.comment)
    runtime.invalidate_config_cache()
    return {"revision": new_rev}
