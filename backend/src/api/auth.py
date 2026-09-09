"""统一认证：SECRET_KEY → Token 签发 + 全局中间件。

【定位】
- 独立模式：auth.html 门禁页输入密钥 → 获取 token → 进入应用
- 嵌入模式：宿主调 /api/auth/token 获取 token → embed SDK headers 透传
- 未配 SECRET_KEY → 全额开放（向后兼容）

Token 算法：base64(expire_at.HMAC-SHA256(secret, expire_at))
24h 有效期，服务端无状态，不存 token。
"""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# ── Token 算法 ────────────────────────────────────────────────

_TOKEN_TTL = 86400  # 24 小时


def _sign(secret: str, expire_at: int) -> str:
    """HMAC-SHA256 签名 → base64 编码。"""
    mac = hmac.new(secret.encode(), str(expire_at).encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode()


def _verify(secret: str, expire_at: int, signature: str) -> bool:
    """常量时间比对签名。"""
    expected = _sign(secret, expire_at)
    return hmac.compare_digest(expected, signature)


def encode_token(secret: str) -> str:
    """签发 token：base64(expire_at.signature)。"""
    expire_at = int(time.time()) + _TOKEN_TTL
    payload = f"{expire_at}.{_sign(secret, expire_at)}"
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def decode_token(secret: str, token: str) -> Optional[int]:
    """校验 token：成功返回 expire_at，失败返回 None。"""
    try:
        # 补齐 base64 padding
        padding = 4 - len(token) % 4
        if padding != 4:
            token += "=" * padding
        payload = base64.urlsafe_b64decode(token).decode()
        parts = payload.split(".")
        if len(parts) != 2:
            return None
        expire_at = int(parts[0])
        if not _verify(secret, expire_at, parts[1]):
            return None
        if time.time() > expire_at:
            return None
        return expire_at
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


# ── Token 签发端点 ─────────────────────────────────────────────

@router.post("/auth/token")
async def issue_token(request: Request):
    """用 ADMIN_TOKEN 换取访问 token。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")

    secret_input = str(body.get("secret") or "").strip()
    if not secret_input:
        raise HTTPException(400, "缺少 secret 字段")

    configured = _get_admin_token()
    if not configured:
        raise HTTPException(503, "服务器未配置 ADMIN_TOKEN，token 签发不可用")

    # 常量时间比对防时序攻击
    if not hmac.compare_digest(secret_input, configured):
        raise HTTPException(401, "密钥无效")

    token = encode_token(_signing_key())
    expire_at = int(time.time()) + _TOKEN_TTL

    return JSONResponse({
        "token": token,
        "expires_at": expire_at,
        "expires_in": _TOKEN_TTL,
    })


# ── 门禁模式探测端点 ───────────────────────────────────────────

@router.get("/auth/config")
async def auth_config():
    """告诉前端门禁页当前认证模式(白名单路径,免 token)。

    前端 admin.html/index.html 的本地门禁依赖它:未配置 ADMIN_TOKEN
    (开放模式)时不拦截、不跳认证页——否则会出现"开放模式却卡在
    认证页"的死锁(中间件全放行,token 签发却 503,页面进不去)。
    """
    return {"secret_configured": bool(_get_admin_token())}


# ── 全局中间件 ─────────────────────────────────────────────────

# 白名单路径（不校验 token）
_AUTH_WHITELIST = frozenset([
    "/api/health", "/health", "/",
    "/api/auth/token",
])


def _get_admin_token() -> str:
    """访问口令(用户在 auth.html 输入的):来自 ADMIN_TOKEN 配置。"""
    return os.getenv("ADMIN_TOKEN", "").strip()


# 签名密钥:进程启动时随机生成。与口令分离——口令是长期配置(部署方管理),
# 签名密钥是短期凭证材料,每次重启/重新部署自动轮换,旧 token 全部失效
# (用户需重新登录)。无状态校验不受影响:密钥活在本进程内,签发与校验同源。
_signing_key_value = secrets.token_hex(32)


def _signing_key() -> str:
    return _signing_key_value


class AuthMiddleware(BaseHTTPMiddleware):
    """全局 token 校验中间件。

    未配 ADMIN_TOKEN → 全部放行（开放模式）。
    已配 → 白名单以外的路径必须带 Authorization: Bearer <token>。
    签名密钥为进程启动时随机生成,重启/重新部署自动轮换(旧 token 全失效)。
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        if _get_admin_token():
            logger.info("AuthMiddleware: token 模式已启用（ADMIN_TOKEN 已配置,签名密钥随启动轮换）")
        else:
            logger.info("AuthMiddleware: 开放模式（ADMIN_TOKEN 未配置）")

    async def dispatch(self, request: Request, call_next):
        # 开放模式：全部放行
        if not _get_admin_token():
            return await call_next(request)

        # 白名单放行
        path = request.url.path
        if path in _AUTH_WHITELIST or path.startswith("/api/auth/"):
            return await call_next(request)

        # 校验 token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "invalid_token", "detail": "缺少 Authorization: Bearer <token>"},
                status_code=401,
            )

        token = auth_header[7:].strip()
        expire_at = decode_token(_signing_key(), token)
        if expire_at is None:
            return JSONResponse(
                {"error": "invalid_token", "detail": "token 无效或已过期"},
                status_code=401,
            )

        # token 有效，注入到 request.state 供下游使用
        request.state.auth_expire_at = expire_at
        return await call_next(request)


# ── 辅助函数（供 api/admin.py 等复用）──────────────────────────

def is_token_authorized(request: Request) -> bool:
    """检查当前请求的 token 是否有效（供 require_admin 等依赖使用）。

    ADMIN_TOKEN 未配置（开放模式）→ 返回 False（回退到旧 X-Admin-Token
    或直接放行语义,由调用方决定）;已配置 → True 仅当 token 已过中间件校验。
    """
    if not _get_admin_token():
        return False  # 开放模式，无 token 可言
    return getattr(request.state, "auth_expire_at", None) is not None