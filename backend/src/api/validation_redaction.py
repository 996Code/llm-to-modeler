"""请求校验错误(422)脱敏——任何 FastAPI 应用都应安装。

Pydantic 校验失败的 detail 会携带原始输入(``input``),密码等凭证字段
绝不能随错误回显。安装后按字段名递归打码再返回;main.py 与测试应用
共用本模块,漏装由独立测试把关。
"""
from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_REDACT_KEYS = ("password", "api_key", "apikey", "secret", "token",
                "authorization", "fernet_key")


def _redact_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: "***" if any(s in str(k).lower() for s in _REDACT_KEYS)
            else _redact_sensitive(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive(x) for x in obj]
    return obj


async def redacted_validation_error_handler(request, exc):
    errors = _redact_sensitive(exc.errors())
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder(errors)})


def install(app) -> None:
    """给应用挂上脱敏后的 RequestValidationError 处理器(幂等)。"""
    app.add_exception_handler(RequestValidationError,
                              redacted_validation_error_handler)
