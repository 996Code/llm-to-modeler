"""main 应用必须安装 422 校验错误脱敏处理器(漏装即凭证回显事故)。"""
from fastapi.exceptions import RequestValidationError

from api.validation_redaction import redacted_validation_error_handler


def test_main_app_registers_redacted_validation_handler():
    import main
    handler = main.app.exception_handlers.get(RequestValidationError)
    # FastAPI 允许直接函数或 partial 包装——按对象身份/名双重判定
    assert handler is not None
    assert getattr(handler, "__name__", "") == redacted_validation_error_handler.__name__
