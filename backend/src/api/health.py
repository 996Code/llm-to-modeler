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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# 创建路由器，tags=["health"] 让这些接口在 /docs 文档里归到 health 分组
# 类比 Spring：@RestController + 在 controller 类上打标签
router = APIRouter(tags=["health"])


# 别名：/api/health——嵌入场景宿主经统一前缀代理探测（/ai-modeler/api/* 剥前缀
# 后到 /api/*），与业务 API 同链路。根路径 /health 保留（运维/K8s 探针惯例）。
@router.get("/api/health")
@router.get("/health")
async def health_check(request: Request):
    """健康检查接口：只回答「本服务是否活着」。

    供运维监控、负载均衡、K8s liveness 探针与嵌入宿主（悬浮球显隐）调用。
    不探测上游 njmind-modeler——嵌入模式下真实上游地址由宿主 services
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
    """
    return JSONResponse(
        {
            "status": "healthy",
            "service": "LLM Form Modeler",
            "version": request.app.version,
        },
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
