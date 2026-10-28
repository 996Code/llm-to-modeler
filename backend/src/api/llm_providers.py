"""平台 LLM 供应商管理 API(设计 8.6/C8,admin 门禁)。

GET  /api/admin/llm-providers   当前配置(api_key 不回显,has_key 布尔)
PUT  /api/admin/llm-providers   整档直存(api_key 留空沿用旧值,revision 用于缓存失效)
POST /api/admin/llm-providers/probe  连通性探测(主/备各发一次 1-token 请求)
"""
import logging
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.admin import require_admin
from services import llm_providers as store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/llm-providers", tags=["admin"],
                   dependencies=[Depends(require_admin)])


class ProviderRoleIn(BaseModel):
    base_url: str
    api_key: str = ""          # 留空 = 沿用旧值(GET 不回显,前端不知明文)
    model: str = ""
    enabled: bool = True
    price_in: float | None = None
    price_out: float | None = None


class ProvidersSaveBody(BaseModel):
    primary: ProviderRoleIn
    fallback: ProviderRoleIn | None = None
    comment: str = ""


def _masked(content: Dict[str, Any]) -> Dict[str, Any]:
    """对外视图:api_key 换 has_key 布尔(明文不出管理 API)。"""
    view: Dict[str, Any] = {"revision": content.get("_revision", 0)}
    for role in ("primary", "fallback"):
        cfg = content.get(role)
        if cfg is None:
            view[role] = None
            continue
        c = dict(cfg)
        c.pop("api_key", None)
        c.pop("_revision", None)
        c["has_key"] = bool(cfg.get("api_key"))
        view[role] = c
    return view


@router.get("")
async def get_providers() -> dict:
    content = store.load_providers()
    if content is None:
        return {"revision": 0, "primary": None, "fallback": None,
                "configured": False}
    return {**_masked(content), "configured": True}


@router.put("")
async def put_providers(body: ProvidersSaveBody, request: Request) -> dict:
    content: Dict[str, Any] = {"primary": body.primary.model_dump()}
    if body.fallback is not None:
        content["fallback"] = body.fallback.model_dump()
    # 留空 key 沿用旧值:把库中旧档的明文 key 并入(_existing_key 通道)
    old = store.load_providers() or {}
    for role in ("primary", "fallback"):
        if role in content and not (content[role].get("api_key") or "").strip():
            old_key = (old.get(role) or {}).get("api_key") or ""
            if old_key:
                content[role]["_existing_key"] = old_key
    try:
        new_rev = store.save_providers(
            content,
            updated_by=getattr(request.state, "user_id", None) or "admin")
    except store.ProviderConfigError as e:
        raise HTTPException(400, f"供应商配置校验失败: {e}")
    store.invalidate_cache()
    return {"revision": new_rev}


@router.post("/probe")
async def probe_providers() -> dict:
    """对当前生效端点逐个发 1-token 探测请求,报可达性与延迟。

    只探测(不改配置);失败不影响保存——探测是辅助诊断。
    """
    results = []
    try:
        from llm.client import LLMClient
        client = LLMClient()
        chain = client._resolve_endpoints()
        if not chain:
            # 未配置也要给出可行动结果(而非空列表让前端白屏)
            results.append({"role": "primary", "ok": False,
                            "base_url": "",
                            "error": "未配置——填写主端点并保存后再探测"})
        for role, cfg, _, _ in chain:
            t0 = time.time()
            try:
                c = client._client_for(cfg)
                c.chat.completions.create(
                    model=cfg.model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1, temperature=0.0)
                results.append({"role": role, "ok": True,
                                "base_url": cfg.base_url,
                                "latency_ms": int((time.time() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                results.append({"role": role, "ok": False,
                                "base_url": cfg.base_url,
                                "error": str(e)[:200]})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"探测失败: {e}")
    return {"results": results}
