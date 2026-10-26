"""metrology_assistant 自有 HTTP API(挂 /api/packs/metrology_assistant)。

Demo 阶段仅一个只读端点 GET /test/env:供管理端「智能客服-测试」页
可视化演示环境——用途→资料库映射/两库导入状态/进度数据源与 MCP 注册/
单号夹具。P1 配置化后改读 DB 场景配置(设计 §9 Tab3 联调预演的基础)。
"""
import logging
import os

from fastapi import APIRouter, Depends, Request

from sdk.pack_api import admin_required

from domains.metrology_assistant.tools import assistant as cfg

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/test/env", dependencies=[Depends(admin_required)])
async def test_env(request: Request) -> dict:
    """客服运行依赖面的只读快照(不跑 LLM/不发业务查询)。"""
    # ── 知识库两库(强检判定链的资料来源) ──
    from domains.knowledge_graph import runtime as kg_runtime
    errors: list[str] = []
    kbs = [{"name": name, "exists": False, "docs": 0, "imported": 0,
            "status": "error"}
           for name in (cfg.MANDATORY_CHECK_KB, cfg.REGULATION_KB)]
    try:
        store = kg_runtime.get_kg_store(request.app.state)
    except Exception:
        logger.exception("Metrology test environment: KG store unavailable")
        errors.append("知识库服务不可用，请查看服务日志")
    else:
        for item in kbs:
            try:
                kb = store.get_kb_by_name(item["name"])
                item["exists"] = kb is not None
                docs = store.list_documents(kb["id"]) if kb else []
                item.update(
                    docs=len(docs),
                    imported=sum(1 for d in docs
                                 if d.get("importStatus") == "succeeded"),
                    status="ok" if kb is not None else "missing")
            except Exception:
                logger.exception("Metrology test environment: KB read failed (%s)",
                                 item["name"])
                errors.append(f"知识库「{item['name']}」状态读取失败，请查看服务日志")

    # ── 进度数据源 + MCP server(进度查询链的执行来源) ──
    ds_id = os.getenv(cfg.PROGRESS_DATASOURCE_ENV, "").strip()
    progress: dict = {
        "env_configured": bool(ds_id), "datasource": None, "mcp": None,
        "datasourceStatus": "not_configured", "mcpStatus": "error"}
    try:
        if ds_id:
            from domains.chatbi import datasources
            from domains.chatbi.runtime import get_pack_db
            info = datasources.get_datasource(get_pack_db(), ds_id)
            progress["datasource"] = (
                {"name": info.name, "active": info.is_active,
                 "dbType": info.db_type, "host": info.host,
                 "tool": info.database} if info else None)
            progress["datasourceStatus"] = "ok" if info else "missing"
    except Exception:
        logger.exception("Metrology test environment: datasource read failed")
        progress["datasourceStatus"] = "error"
        errors.append("进度数据源状态读取失败，请查看服务日志")
    try:
        mcp = request.app.state.mcp_server_store.get_by_code("metrology")
        progress["mcp"] = (
            {"code": "metrology", "status": mcp.get("status"),
             "enabled": mcp.get("enabled"),
             "tools": [t.get("name") for t in mcp.get("tools_snapshot")
                       or []]} if mcp else None)
        progress["mcpStatus"] = "ok" if mcp else "missing"
    except Exception:
        logger.exception("Metrology test environment: MCP registry read failed")
        errors.append("MCP 注册状态读取失败，请查看服务日志")

    return {
        "errors": errors,
        # 用途→资料库映射(演示配置常量,P1 移 DB)
        "kbMapping": {
            "purposes": list(cfg.STRONG_INSPECTION_PURPOSES),
            "mandatoryCheckKb": cfg.MANDATORY_CHECK_KB,
            "regulationKb": cfg.REGULATION_KB,
        },
        "kbs": kbs,
        "progress": progress,
        # 单号夹具(mock 业务平台;话术口径:模拟数据)
        "orderFixtures": [
            {"order": o.label, "desc": o.description}
            for o in cfg.DEMO_ORDER_OPTIONS
        ] + [
            {"order": "BJ8888", "desc": "他主体单(拒权)"},
            {"order": "BJ9000", "desc": "不存在(查无)"},
            {"order": "BJ0000", "desc": "触发超时(约10s 如实报错)"},
        ],
    }
