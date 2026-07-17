"""
Skills API — proxy to upstream njmind-modeler.
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/templates")
async def list_templates(request: Request) -> List[str]:
    upstream = request.app.state.upstream
    return upstream.list_templates()


@router.get("/templates/{name}")
async def get_template(name: str, request: Request) -> Dict[str, Any]:
    upstream = request.app.state.upstream
    template = upstream.get_template(name)
    if not template:
        raise HTTPException(404, f"Template '{name}' not found")
    return template


@router.get("/schemas")
async def list_schemas(request: Request) -> List[str]:
    upstream = request.app.state.upstream
    return upstream.list_schemas()


@router.get("/schemas/{name}")
async def get_schema(name: str, request: Request) -> Dict[str, Any]:
    upstream = request.app.state.upstream
    schema = upstream.get_schema(name)
    if not schema:
        raise HTTPException(404, f"Schema '{name}' not found")
    return schema


@router.get("/guide")
async def get_guide(request: Request) -> Dict[str, Any]:
    upstream = request.app.state.upstream
    guide = upstream.get_guide()
    if not guide:
        raise HTTPException(404, "Guide not found")
    return guide
