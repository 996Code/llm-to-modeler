"""
健康检查 API 模块。

提供应用健康检查和根路径接口，供运维监控、K8s liveness/readiness 探针使用。

核心设计（Java 视角）：
  - APIRouter：类比 Spring MVC 的 @RestController，集中注册本模块的路由。
    tags 用于 OpenAPI（/docs）分组。
  - Request：类比 HttpServletRequest，FastAPI 通过依赖注入把请求对象传进来。
    这里只读取 app 全局状态，不做业务处理。
  - app.state：类比 Spring 的 ApplicationContext（全局单例容器）。
    在 main.py 的 lifespan 中初始化，所有请求共享。
    upstream、conversation_store、llm_client、graph 等共享对象都挂在这里。
  - app.version：FastAPI 应用版本号（main.py 里 FastAPI(version=...) 设置），
    从 app.state 读保证版本和 main.py 始终同步，避免硬编码。
"""

import logging
import importlib

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
from fastapi.responses import JSONResponse

# 创建路由器，tags=["health"] 让这些接口在 /docs 文档里归到 health 分组
# 类比 Spring：@RestController + 在 controller 类上打标签
router = APIRouter(tags=["health"])


def _loaded_pack_health(request: Request) -> tuple[bool, str | None]:
    """调用已装配 pack 的可选 health_status 钩子。

    平台只聚合插件结果，不识别任何具体领域或 scheduler 名称。未导出
    health_status 的 pack 不参与检查。普通可选 pack 的异常/降级作为
    warning 返回但不拉低平台 readiness；只有 PACKS_CRITICAL 显式声明
    的 pack 才 fail-closed。这样单个可选插件故障不会摘除整个平台实例。
    """
    from sdk.pack_api import critical_packs
    critical = critical_packs()
    warnings = []
    assembly_errors = getattr(
        request.app.state, "_pack_assembly_errors", {}) or {}
    for pack_name, error in sorted(assembly_errors.items()):
        detail = f"pack {pack_name} assembly failed: {error}"
        if pack_name in critical:
            return False, detail
        warnings.append(detail)
    lifecycle_errors = getattr(
        request.app.state, "_pack_lifecycle_errors", {}) or {}
    for pack_name in sorted(getattr(request.app.state, "_loaded_packs", ()) or ()):
        if pack_name in lifecycle_errors:
            detail = (f"pack {pack_name} lifecycle failed: "
                      f"{lifecycle_errors[pack_name]}")
            if pack_name in critical:
                return False, detail
            warnings.append(detail)
        try:
            module = importlib.import_module(f"domains.{pack_name}.pack")
            checker = getattr(module, "health_status", None)
            if not callable(checker):
                continue
            result = checker()
            if isinstance(result, dict):
                status = result.get("status", "ok")
                if status not in {"ok", "healthy", "disabled"}:
                    detail = str(
                        result.get("detail") or f"pack {pack_name}: {status}")
                    if pack_name in critical:
                        return False, detail
                    warnings.append(detail)
        except Exception as exc:
            logger.exception("pack health check failed: %s", pack_name)
            detail = f"pack {pack_name} health check failed: {exc}"
            if pack_name in critical:
                return False, detail
            warnings.append(detail)
    return True, "; ".join(warnings) if warnings else None


# 别名：/api/health——嵌入场景宿主经统一前缀代理探测（宿主前缀 /<mount>/api/* 剥前缀
# 后到 /api/*），与业务 API 同链路。根路径 /health 保留（运维/K8s 探针惯例）。
@router.get("/api/health")
@router.get("/health")
async def health_check(request: Request):
    """健康检查接口：只回答「本服务是否活着」。

    供运维监控、负载均衡、K8s liveness 探针与嵌入宿主（悬浮球显隐）调用。
    不探测上游服务——嵌入模式下真实上游地址由宿主 services
    按请求下发，启动/探测期不存在可探的目标；上游可用性由请求时的
    preflight 前置校验与 resolve_base fail-closed 保证。

    Args:
        request: 当前请求对象，用于读取 app 全局状态。

    Returns:
        JSONResponse: body 为 {
            status: 固定 "healthy"（本进程存活即可达），
            service: 服务名，
            version: 应用版本号（从 app.version 读，和 main.py 同步）
        }

    响应头 Cache-Control: no-store —— 健康检查响应禁止被网关/浏览器缓存：
    曾出现网关对 200 做协商缓存回 304，宿主探测的 fetch 把缓存拼成 200
    误判健康（后端已挂入口仍显示）。no-store 让每次探测都拿到实时状态。

    三十八审 P1-A(降级可见): 热切换回滚失败时 app.state.pack_runtime_
    degraded 被置位——health 如实返回 503 degraded(不能继续谎报
    healthy 让流量打进分裂的 runtime), 直到重启恢复。
    """
    if getattr(request.app.state, "pack_runtime_degraded", False):
        return JSONResponse(
            {
                "status": "degraded",
                "service": "LLM Form Modeler",
                "version": request.app.version,
                "detail": "pack hot-reload rollback failed—restart required",
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    ready, detail = _loaded_pack_health(request)
    if not ready:
        return JSONResponse(
            {
                "status": "degraded",
                "service": "LLM Form Modeler",
                "version": request.app.version,
                "detail": detail,
            },
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    body = {
        "status": "healthy",
        "service": "LLM Form Modeler",
        "version": request.app.version,
    }
    if detail:
        body["warnings"] = [detail]
    return JSONResponse(
        body,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/")
async def root(request: Request):
    """根路径接口。

    返回 API 基本信息（服务名、版本、文档地址），主要用于浏览器访问根路径时
    给开发者一个入口提示，引导到 Swagger 文档。

    Args:
        request: 当前请求对象。

    Returns:
        dict: {message, version, docs}
    """
    return {
        "message": "LLM Form Modeler API",
        "version": request.app.version,
        "docs": "/docs",  # FastAPI 自动生成的 Swagger UI 地址
    }
