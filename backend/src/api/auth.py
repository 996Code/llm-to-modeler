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
    """用 SECRET_KEY 换取访问 token。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")

    secret_input = str(body.get("secret") or "").strip()
    if not secret_input:
        raise HTTPException(400, "缺少 secret 字段")

    configured = _get_secret()
    if not configured:
        raise HTTPException(503, "服务器未配置 SECRET_KEY，token 签发不可用")

    # 常量时间比对防时序攻击
    if not hmac.compare_digest(secret_input, configured):
        raise HTTPException(401, "密钥无效")

    token = encode_token(configured)
    expire_at = int(time.time()) + _TOKEN_TTL

    return JSONResponse({
        "token": token,
        "expires_at": expire_at,
        "expires_in": _TOKEN_TTL,
    })


# ── 全局中间件 ─────────────────────────────────────────────────

# 白名单路径（不校验 token）
_AUTH_WHITELIST = frozenset([
    "/api/health", "/health", "/",
    "/api/auth/token",
])


def _get_secret() -> str:
    """取签名密钥：复用 ADMIN_TOKEN（与旧管理端同一把密钥）。"""
    return os.getenv("ADMIN_TOKEN", "").strip()


class AuthMiddleware(BaseHTTPMiddleware):
    """全局 token 校验中间件。

    未配 SECRET_KEY → 全部放行（开放模式）。
    已配 → 白名单以外的路径必须带 Authorization: Bearer <token>。
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        if _get_secret():
            logger.info("AuthMiddleware: token 模式已启用（SECRET_KEY 已配置）")
        else:
            logger.info("AuthMiddleware: 开放模式（SECRET_KEY 未配置）")

    async def dispatch(self, request: Request, call_next):
        secret = _get_secret()
        # 开放模式：全部放行
        if not secret:
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
        expire_at = decode_token(secret, token)
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

    SECRET_KEY 未配置 → 返回 False（回退到 ADMIN_TOKEN 检查）。
    SECRET_KEY 已配置 → 返回 True 仅当 token 有效。
    """
    if not _get_secret():
        return False  # 开放模式，不做 token 校验，回退到旧 ADMIN_TOKEN
    return getattr(request.state, "auth_expire_at", None) is not None