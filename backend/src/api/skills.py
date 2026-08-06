"""
Skills API 模块 —— 上游 njmind-modeler 的元数据代理路由。

本模块是"薄代理层"：本身不存储任何数据，所有模板、Schema、填写指南
都通过 UpstreamClient 从上游 njmind-modeler（:7001）拉取后原样返回。

核心设计（Java 视角）：
  - 代理模式：类比 Spring 里一个 Controller 委托给 Service，
    这里 Controller(skills.py) → Service(UpstreamClient) → 上游 HTTP。
  - 路由分组：所有接口都有 /api/skills 前缀，在 /docs 归到 skills 分组。
  - app.state.upstream：UpstreamClient 单例，在 main.py lifespan 中初始化。
    类比 Spring 注入的 @Autowired Service。

为什么需要这个代理：
  前端只对接本服务（一个端口），本服务统一聚合上游所有能力，
  前端无需感知上游 njmind-modeler 的存在。
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
# prefix 给所有路由加统一前缀，tags 用于 /docs 分组
router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/templates")
async def list_templates(request: Request) -> List[str]:
    """列出上游所有表单模板文件名。

    返回的是文件名列表（如 ["simple_form.json", ...]），不含模板内容。
    要拿内容再调 GET /api/skills/templates/{name}。

    Args:
        request: 当前请求对象，用于取 app.state.upstream。

    Returns:
        模板文件名列表。上游不可用时 UpstreamClient 返回空列表（不抛异常）。
    """
    upstream = request.app.state.upstream
    return upstream.list_templates()


@router.get("/templates/{name}")
async def get_template(name: str, request: Request) -> Dict[str, Any]:
    """获取指定模板的完整 JSON 内容。

    Args:
        name: 模板名，可不带 .json 后缀（UpstreamClient 会自动补全）。
        request: 当前请求对象。

    Returns:
        模板 JSON 字典。

    Raises:
        HTTPException(404): 上游找不到该模板（UpstreamClient 返回 None）。
    """
    upstream = request.app.state.upstream
    template = upstream.get_template(name)
    # Fail-Closed：上游找不到就返回 404，不返回空对象，避免前端误判
    if not template:
        raise HTTPException(404, f"Template '{name}' not found")
    return template


@router.get("/schemas")
async def list_schemas(request: Request) -> List[str]:
    """列出上游所有 JSON Schema 文件名。

    Schema 用于表单配置的结构校验（如 form-config.schema.json）。

    Args:
        request: 当前请求对象。

    Returns:
        Schema 文件名列表。上游不可用时返回空列表。
    """
    upstream = request.app.state.upstream
    return upstream.list_schemas()


@router.get("/schemas/{name}")
async def get_schema(name: str, request: Request) -> Dict[str, Any]:
    """获取指定 JSON Schema 的完整内容。

    Args:
        name: Schema 名，可不带 .schema.json 后缀（自动补全）。
        request: 当前请求对象。

    Returns:
        Schema JSON 字典。

    Raises:
        HTTPException(404): 上游找不到该 Schema。
    """
    upstream = request.app.state.upstream
    schema = upstream.get_schema(name)
    if not schema:
        raise HTTPException(404, f"Schema '{name}' not found")
    return schema


@router.get("/guide")
async def get_guide(request: Request) -> Dict[str, Any]:
    """获取字段类型填写指南（guide.json）。

    指南里包含每种字段类型的关键词索引和填写规范，
    供 LLM 在生成表单配置时参考（哪些描述对应哪种字段类型）。

    Args:
        request: 当前请求对象。

    Returns:
        指南 JSON 字典。

    Raises:
        HTTPException(404): 上游没有 guide.json。
    """
    upstream = request.app.state.upstream
    guide = upstream.get_guide()
    if not guide:
        raise HTTPException(404, "Guide not found")
    return guide
