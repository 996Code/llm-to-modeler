"""RedactFilter - 日志凭证脱敏。

对标 Claude Code secretScanner.ts:
- Bearer token / API key (sk-xxx)
- Cookie 值
- Authorization header
- 其他敏感模式

Engine 启动时挂载到 root logger,所有子 logger 继承。
"""
import logging
import re


# 敏感模式 + 替换规则(顺序敏感:先匹配长模式)
_SECRET_RULES: list[tuple[re.Pattern, str]] = [
    # Bearer token: "Bearer eyJxxx" -> "Bearer ***REDACTED***"
    (
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=?", re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # API key: "sk-xxxxxx"
    (
        re.compile(r"(sk-)[a-zA-Z0-9]{8,}"),
        r"\1***REDACTED***",
    ),
    # Authorization header value: "Authorization: xxx" 或 "authorization": "xxx"
    (
        re.compile(r'((?:authorization|auth)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # Cookie: "cookie: xxx" 或 "cookie": "xxx"
    (
        re.compile(r'((?:cookie|set-cookie)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # x-api-key / x-token 等常见 header
    (
        re.compile(r'((?:x-api-key|x-token|x-auth-token|x-secret)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # JWT 模式: eyJxxx.eyJxxx.xxx
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "***REDACTED_JWT***",
    ),
]


class RedactFilter(logging.Filter):
    """日志凭证脱敏过滤器。

    挂载到 logger 后,所有 log record 的 message 都会经过 _SECRET_RULES redact。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """redact message 中的敏感信息。返回 True(总是放行)。"""
        msg = record.getMessage()
        redacted = self._redact(msg)
        if redacted != msg:
            # 替换 record 的 message
            record.msg = redacted
            record.args = None  # 已替换,不再 format
        return True

    @staticmethod
    def _redact(text: str) -> str:
        """对文本应用所有脱敏规则。"""
        for pattern, replacement in _SECRET_RULES:
            text = pattern.sub(replacement, text)
        return text


def install_redact_filter(logger_name: str = None) -> RedactFilter:
    """安装 RedactFilter 到指定 logger(默认 root)。

    Args:
        logger_name: logger 名,None 表示 root logger

    Returns:
        安装的 RedactFilter 实例(可用于卸载)
    """
    logger = logging.getLogger(logger_name)
    # 避免重复安装
    for existing in logger.filters:
        if isinstance(existing, RedactFilter):
            return existing
    f = RedactFilter()
    logger.addFilter(f)
    return f
