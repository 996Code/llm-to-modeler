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


class DryRunBody(BaseModel):
    """联调预演(设计 §9 Tab3):一句话走提取+求值,不落库不写 flow。"""
    text: str
    answers: dict = {}          # 可选:模拟补答(如 {"委托单号": "BJ2026"})
    config: dict | None = None  # 可选:草案配置(保存前差异对比用)
    execute: bool = False       # 真实执行复选:槽位齐时真的调底层 API


def _dry_run_once(scenarios: dict, llm, body: "DryRunBody") -> dict:
    """对一份场景表跑一次预演;返回命中/槽位/缺口/将问/将调。"""
    from domains.metrology_assistant.tools import assistant as mta

    answers = body.answers or {}
    if answers:
        slots = mta.parse_clarify_answers(answers, scenarios)
        scenario = next((k for k, c in scenarios.items()
                         if c["required"]["slot"] in slots), None)
        merged = mta.merge_slots(slots, {})
    else:
        extracted = mta.extract_slots(llm, body.text, {}, None, scenarios)
        scenario = extracted["scenario"]
        merged = mta.merge_slots(extracted, {})

    result: dict = {"input": body.text, "scenario": scenario or None}
    if scenario is None or scenario not in scenarios:
        result["missing"] = True
        result["note"] = "未命中场景(将答复暂不支持)"
        return result
    cfg = scenarios[scenario]
    required = cfg["required"]
    merged, reask = mta.normalize_required_slot(required, merged)
    slot_label = required.get("header", required["slot"])
    result["slots"] = {k: v for k, v in merged.items() if v}
    if reask is not None:
        result["will_ask"] = {"question": reask, "header": required["header"],
                              "options": [o.get("label") if isinstance(o, dict) else o
                                          for o in (required.get("options") or [])]}
        result["note"] = f"槽位类型校验未过({slot_label})"
        return result
    if not merged.get(required["slot"]):
        question = required["question"]
        subject_slot = required.get("subject_slot")
        if subject_slot is not None and "{subject}" in question:
            value = merged.get(subject_slot) or ""
            subject = f"「{value}」" if value else required.get(
                "subject_default", "")
            question = question.format(subject=subject)
        result["will_ask"] = {"question": question,
                              "header": required["header"],
                              "options": [o.get("label") if isinstance(o, dict) else o
                                          for o in (required.get("options") or [])]}
        result["note"] = f"缺少必填槽位 {slot_label}"
        return result
    action = cfg["action"]
    if action["kind"] == "kg":
        result["action"] = {"kind": "kg",
                            "kb": mta.resolve_kb_for_purpose(
                                merged.get("purpose") or "")}
    else:
        result["action"] = {
            "kind": "chatbi", "tool": action["tool"],
            "datasource_env": action["env"],
            "datasource_name": action.get("ds_name", ""),
            "params": {k: merged.get(v, "") for k, v in
                       (action.get("params") or {}).items()}}
    result["missing"] = False
    return result


@router.post("/config/dry-run", dependencies=[Depends(admin_required)])
async def dry_run(body: DryRunBody, request: Request) -> dict:
    """联调预演:提取+求值(只读);execute=True 且槽位齐时真实调底层。

    config 传入草案配置时按草案求值(不落库)——前端对同一句话各跑一次
    当前配置与草案即可得到差异对比;预演永不写 flow/任务。
    """
    try:
        if body.config is not None:
            validated = validate_config(body.config)
            scenarios = validated["scenarios"]
        else:
            from domains.metrology_assistant.scenario_config import load_scenarios
            scenarios = load_scenarios()
    except ConfigValidationError as e:
        raise HTTPException(400, f"草案配置校验失败: {e}")

    llm = getattr(request.app.state, "llm_client", None)
    out = _dry_run_once(scenarios, llm, body)

    if (body.execute and not out.get("missing")
            and out.get("action", {}).get("kind") == "chatbi"):
        import os
        from domains.chatbi.controlled import execute_controlled
        ds_id = os.getenv(out["action"]["datasource_env"], "").strip()
        if not ds_id:
            out["executed"] = {"ok": False, "error": "数据源未配置(env 缺失)"}
        else:
            r = execute_controlled(request.app.state, ds_id,
                                   out["action"]["tool"],
                                   out["action"]["params"])
            out["executed"] = {
                "ok": r.ok, "status": r.status,
                "error": r.error.message if r.error else None,
                "rows": len((r.data or {}).get("rows") or [])
                        if isinstance(r.data, dict) else None}
    return out
