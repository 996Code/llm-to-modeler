"""RedactFilter 日志凭证脱敏测试。"""
import logging
import pytest
from io import StringIO

from engine.logging_filter import RedactFilter, install_redact_filter


class TestRedactFilter:
    """各种敏感模式的 redact。"""

    def test_redact_bearer_token(self):
        """Bearer token 被 redact(可能被 authorization 规则先匹配)。"""
        result = RedactFilter._redact("Authorization: Bearer eyJabc123.def")
        assert "eyJabc123" not in result
        assert "***REDACTED***" in result

    def test_redact_sk_api_key(self):
        assert RedactFilter._redact("key=sk-abc123456789012345") == "key=sk-***REDACTED***"

    def test_redact_authorization_header(self):
        assert RedactFilter._redact("headers={'authorization': 'token123'}") == "headers={'authorization': '***REDACTED***'}"

    def test_redact_cookie(self):
        assert RedactFilter._redact("cookie: session_id=abc123") == "cookie: ***REDACTED***"

    def test_redact_set_cookie(self):
        assert "abc123" not in RedactFilter._redact("set-cookie: session=abc123")

    def test_redact_x_api_key(self):
        assert RedactFilter._redact("x-api-key: secret123") == "x-api-key: ***REDACTED***"

    def test_redact_jwt(self):
        """JWT 模式被 redact(完整 JWT 格式)。"""
        # 完整 JWT 格式(3 段 base64,中间有 .)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = RedactFilter._redact(f"token={jwt}")
        assert jwt not in result
        assert "***REDACTED" in result

    def test_normal_text_unchanged(self):
        """无敏感信息的日志原样输出。"""
        assert RedactFilter._redact("正常日志: 用户创建表单请假表") == "正常日志: 用户创建表单请假表"

    def test_empty_string(self):
        assert RedactFilter._redact("") == ""

    def test_multiple_secrets_in_one_line(self):
        """一行日志含多个敏感信息,全部 redact。"""
        line = "Authorization: Bearer xxx, cookie: yyy, sk-zzz123456789012"
        redacted = RedactFilter._redact(line)
        # 原始敏感值不再出现
        assert "Bearer xxx" not in redacted
        assert "yyy" not in redacted
        assert "sk-zzz123456789012" not in redacted
        # redact 标记出现
        assert "***REDACTED***" in redacted


class TestFilterIntegration:
    """过滤器集成到 logging 的行为。"""

    def test_filter_attached_to_logger(self):
        """RedactFilter 挂载后,logger.info 的输出被 redact。"""
        logger = logging.getLogger("test_redact_integration")
        logger.handlers.clear()
        logger.filters.clear()

        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        # 挂载 filter
        install_redact_filter("test_redact_integration")

        # 记录含敏感信息的日志
        logger.info("Authorization: Bearer secret_token_123")

        output = stream.getvalue()
        assert "secret_token_123" not in output
        assert "***REDACTED***" in output

    def test_install_idempotent(self):
        """多次 install 不重复挂载。"""
        logger_name = "test_redact_idempotent"
        logging.getLogger(logger_name).filters.clear()

        f1 = install_redact_filter(logger_name)
        f2 = install_redact_filter(logger_name)
        assert f1 is f2  # 同一实例

        filters = [f for f in logging.getLogger(logger_name).filters if isinstance(f, RedactFilter)]
        assert len(filters) == 1
