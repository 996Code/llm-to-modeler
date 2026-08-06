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

# 创建路由器，tags=["health"] 让这些接口在 /docs 文档里归到 health 分组
# 类比 Spring：@RestController + 在 controller 类上打标签
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(request: Request):
    """健康检查接口。

    供运维监控、负载均衡、K8s liveness 探针调用。
    注意：这里只检查 upstream 客户端是否已初始化（对象存在），
    不发实际网络请求（避免每次探针都打上游），属于轻量级存活检查。

    Args:
        request: 当前请求对象，用于读取 app 全局状态。

    Returns:
        dict: {
            status: 固定 "healthy"，
            service: 服务名，
            version: 应用版本号（从 app.version 读，和 main.py 同步），
            upstream: 上游客户端是否已初始化（True=已就绪）
        }
    """
    return {
        "status": "healthy",
        "service": "LLM Form Modeler",
        "version": request.app.version,
        # getattr 兜底：lifespan 未执行时 app.state.upstream 不存在，返回 None（Fail-Closed）
        # lifespan 在 main.py 中初始化 UpstreamClient 并挂到 app.state.upstream
        "upstream": getattr(request.app.state, "upstream", None) is not None,
    }


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
