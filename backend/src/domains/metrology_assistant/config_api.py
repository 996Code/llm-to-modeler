"""metrology_assistant 场景配置管理 API(挂 /api/packs/metrology_assistant)。

管理页数据面(自动路由版的润色集保存):
  GET  /config   当前润色场景 + revision(运行时缓存计数,UI 不暴露)
  PUT  /config   整档直存(400=校验失败)

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
    comment: str = ""


class CapabilityDraftBody(BaseModel):
    """能力起草请求(server+tool → 场景草稿,不落库)。"""
    server: str
    tool: str


def _db():
    return runtime.get_pack_db()


def _user(request: Request) -> str:
    return getattr(request.state, "user_id", None) or "admin"


@router.get("/config", dependencies=[Depends(admin_required)])
async def get_config() -> dict:
    """当前生效配置(无行=尚未种子,返回出厂默认与 revision 0)。

    返回升级视图:存量演示场景补全显式 conclusion/模板/兜底选库规则
    ——管理员看到的就是装载生效的形态,整档 PUT 回去自然通过严格校验
    (否则改一个场景会因其他未升级场景被 400 拒绝)。
    """
    config = store.load_config(_db())
    if config is None:
        default = factory_default_config()
        return {"revision": 0, "content": default,
                "factory": True}
    revision = config.pop("_revision")
    from domains.metrology_assistant.legacy_demo import upgrade_legacy_scenario
    content = {k: v for k, v in config.items() if not k.startswith("_")}
    content = {**content, "scenarios": {
        key: upgrade_legacy_scenario(key, cfg)
        for key, cfg in content.get("scenarios", {}).items()}}
    return {"revision": revision, "content": content, "factory": False}


@router.put("/config", dependencies=[Depends(admin_required)])
async def put_config(body: ConfigSaveBody, request: Request) -> dict:
    """整档直存:校验 → 保存 → 失效缓存(热生效)。

    400=结构校验失败。
    """
    try:
        validate_config(body.content, strict=True)
    except ConfigValidationError as e:
        raise HTTPException(400, f"场景配置校验失败: {e}")
    new_rev = store.save_config(
        _db(), body.content, updated_by=_user(request), comment=body.comment)
    runtime.invalidate_config_cache()
    return {"revision": new_rev}


@router.get("/config/routing", dependencies=[Depends(admin_required)])
async def routing_overview_ep(request: Request) -> dict:
    """自动路由总览(管理页主视图):能力三态(polished/auto/blocked)
    + 数据源解析 + 主槽位追问预览;顺带触发过期快照的后台刷新。

    ?refresh=true:先同步重探测全部启用服务的 MCP 声明再返回——
    业务平台补全声明后点「刷新」立即可见,不等 60s TTL。
    探测走网络,必须 to_thread(不能在事件循环线程里跑 asyncio.run)。
    """
    import asyncio
    from domains.metrology_assistant.capability_import import (
        refresh_snapshots, routing_overview)
    if (request.query_params.get("refresh") or "").strip().lower() in ("1", "true"):
        await asyncio.to_thread(refresh_snapshots)
    try:
        return routing_overview()
    except Exception as e:  # noqa: BLE001
        logger.exception("路由总览读取失败")
        raise HTTPException(500, f"路由总览读取失败: {e}")


@router.get("/config/capability-import",
            dependencies=[Depends(admin_required)])
async def capability_inventory() -> dict:
    """能力库存 + 发现 diff(管理页向导数据面;只读)。"""
    from domains.metrology_assistant.capability_import import inventory
    try:
        return inventory()
    except Exception:  # noqa: BLE001
        logger.exception("能力库存读取失败")
        raise HTTPException(500, "MCP 注册表读取失败,请查看服务日志")


@router.post("/config/capability-import/draft",
             dependencies=[Depends(admin_required)])
async def capability_draft(body: CapabilityDraftBody) -> dict:
    """MCP 工具声明 → 场景草稿(不落库;前端插入编辑器走整档保存)。"""
    from domains.metrology_assistant.capability_import import draft_scenario
    try:
        return draft_scenario(body.server, body.tool)
    except ValueError as e:
        raise HTTPException(400, str(e))


class DryRunBody(BaseModel):
    """联调预演(设计 §9 Tab3):一句话走提取+求值,不落库不写 flow。"""
    text: str
    answers: dict = {}          # 可选:模拟补答(如 {"委托单号": "BJ2026"})
    config: dict | None = None  # 可选:草案配置(保存前差异对比用)
    execute: bool = False       # 真实执行复选:槽位齐时真的调底层 API


def _dry_run_once(scenarios: dict, llm, body: "DryRunBody") -> dict:
    """Read-only evaluation using the exact conversation plan."""
    from domains.metrology_assistant.tools import assistant as mta
    from domains.metrology_assistant.capability_plan import prepare_plan
    extracted = mta.extract_slots(llm, body.text, {}, None, scenarios)
    scenario = extracted['scenario']
    out = {'input': body.text, 'scenario': scenario if scenario in scenarios else None}
    if scenario not in scenarios:
        return {**out, 'missing': True, 'note': '未命中场景(将答复暂不支持)'}
    cfg = scenarios[scenario]
    merged = mta.merge_slots(extracted, {})
    plan = prepare_plan(cfg, merged, body.text)
    if body.answers:
        pending = plan.get('will_ask')
        merged = mta.merge_slots(mta.parse_clarify_answers(body.answers, pending=pending), merged)
        plan = prepare_plan(cfg, merged, body.text)
    out['slots'] = plan['slots']
    if plan.get('will_ask'):
        ask = plan['will_ask']
        out['will_ask'] = {'question': ask['question'], 'header': ask['header'],
                           'slot': ask['slot'], 'options': [o.get('label') if isinstance(o, dict) else o.label
                                                          for o in ask.get('options') or []]}
        out['missing'] = True
        return out
    if plan.get('error'):
        return {**out, 'missing': True, 'error': plan['error']}
    out.update(missing=False, action={k: v for k, v in plan.items() if k not in ('slots', 'will_ask')})
    return out



def effective_scenarios(config: dict | None = None) -> dict:
    """预演与对话共用的一份路由表:覆盖先绑定当前声明,再并上自动层。

    草案(config 非空)与线上生效走同一条合成路径——否则草案预演用旧
    schema、保存后运行时却按最新声明解析,出现"预演过保存炸"的口径差。
    """
    from domains.metrology_assistant.capability_import import (
        auto_scenarios, resolve_scenarios)
    if config is None:
        from domains.metrology_assistant.scenario_config import load_scenarios
        table = load_scenarios()
    else:
        # 草案走与保存同款 strict 校验:预演不合法草案直接 400,
        # 而不是宽松档放行后"预演过保存炸"
        table = validate_config(config, strict=True)["scenarios"]
    try:
        resolved = resolve_scenarios(table)
        return {**auto_scenarios(resolved), **resolved}
    except Exception:  # noqa: BLE001
        logger.exception("路由表合成失败,按场景表原样求值")
        return table


@router.post("/config/dry-run", dependencies=[Depends(admin_required)])
async def dry_run(body: DryRunBody, request: Request) -> dict:
    """联调预演:提取+求值(只读);execute=True 且槽位齐时真实调底层。

    config 传入草案配置时按草案求值(不落库)——前端对同一句话各跑一次
    当前配置与草案即可得到差异对比;预演永不写 flow/任务。
    """
    try:
        scenarios = effective_scenarios(body.config)
    except ConfigValidationError as e:
        raise HTTPException(400, f"草案配置校验失败: {e}")

    llm = getattr(request.app.state, "llm_client", None)
    out = _dry_run_once(scenarios, llm, body)

    if (body.execute and not out.get("missing")
            and out.get("action", {}).get("kind") == "chatbi"):
        import asyncio
        from domains.chatbi.controlled import execute_controlled
        from domains.metrology_assistant.capability_plan import resolve_datasource
        try:
            ds_id = resolve_datasource(out['action'])
        except ValueError as error:
            out['executed'] = {'ok': False, 'error': str(error)}
            return out
        if not ds_id:
            out["executed"] = {"ok": False, "error": "数据源未配置(env 缺失)"}
        else:
            # execute_controlled → mcp_client 是同步实现(asyncio.run);
            # 本路由在事件循环线程上,须抛到工作线程跑,否则嵌套 run 报错
            r = await asyncio.to_thread(
                execute_controlled, request.app.state, ds_id,
                out["action"]["tool"], out["action"]["params"])
            out["executed"] = {
                "ok": r.ok, "status": r.status,
                "error": r.error.message if r.error else None,
                "rows": len((r.data or {}).get("rows") or [])
                        if isinstance(r.data, dict) else None}
    return out
