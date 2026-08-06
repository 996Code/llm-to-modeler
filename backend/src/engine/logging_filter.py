"""RedactFilter - 日志凭证脱敏过滤器。

模块定位
========

日志安全闸门:在 log record 输出前,把 message 里的凭证(Bearer token、API key、
Cookie、JWT 等)替换成占位符,防止敏感信息泄露到日志文件。

对标 Claude Code 的 ``secretScanner.ts``。

工作原理
========

继承 Python 标准库 ``logging.Filter`` —— 注意这里的 “Filter” 不是“过滤掉某些日志”,
而是“在日志输出前对内容做改写”。``filter`` 方法返回 ``True`` 表示放行(永远放行),
在放行前篡改 record 的 message。Java 类比:类似 Logback 的 ``%replace`` pattern,
或一个改写 event 的 ``TurboFilter`` / 自定义 ``Layout``。

挂载方式:Engine 启动时调 ``install_redact_filter()`` 挂到 root logger,
Python logging 的继承机制让所有子 logger 自动走这个 filter(类比 Java logback
root logger 配的 appender/filter 会被所有 logger 继承)。

Fail-Closed 原则
================

规则顺序敏感(见 ``_SECRET_RULES`` 注释),且宁可错杀不可漏放:即便把正常文本
误判成凭证脱敏了,也比漏掉真凭证强。所有替换都是用占位符,**不会**把原文留在内存里
传播到别处。

Java 类比
========

- ``RedactFilter`` ≈ Servlet Filter / Logback TurboFilter,但职责是改写而非拦截。
- ``_SECRET_RULES`` ≈ 一组正则规则,类似 Spring Security 的 ``RegexRequestMatcher`` 列表。
"""
import logging
import re


# 敏感模式 + 替换规则(顺序敏感!)。
# 设计原则:**先匹配长模式/具体模式,再匹配通用模式**,避免短模式先吃掉前缀导致长模式失配。
# Java 类比:类似 Web 安全过滤器里 SecurityFilterChain 的顺序 —— 越具体的规则越靠前。
# 每条规则是 (编译后的正则, 替换模板);替换模板里的 ``\1`` 是反向引用第 1 个分组(保留 key/前缀,只脱敏 value)。
_SECRET_RULES: list[tuple[re.Pattern, str]] = [
    # Bearer token: "Bearer eyJxxx" -> "Bearer ***REDACTED***"
    # 分组 (Bearer\s+) 保留 "Bearer " 前缀,只替换后面的凭证值;IGNORECASE 兼容 "bearer"
    (
        re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=?", re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # API key: "sk-xxxxxx" -> "sk-***REDACTED***"
    # OpenAI/Anthropic 风格 key 前缀 "sk-";{8,} 要求至少 8 位避免误伤普通 "sk-" 单词
    (
        re.compile(r"(sk-)[a-zA-Z0-9]{8,}"),
        r"\1***REDACTED***",
    ),
    # Authorization header value: "Authorization: xxx" 或 "authorization": "xxx"
    # 同时兼容 HTTP header 风格(:)和 JSON 字段风格(=)两种写法
    (
        re.compile(r'((?:authorization|auth)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # Cookie: "cookie: xxx" 或 "cookie": "xxx"(含 set-cookie 响应头)
    (
        re.compile(r'((?:cookie|set-cookie)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # x-api-key / x-token 等常见自定义鉴权 header
    (
        re.compile(r'((?:x-api-key|x-token|x-auth-token|x-secret)["\']?\s*[:=]\s*["\']?)[^"\',\s]+', re.IGNORECASE),
        r"\1***REDACTED***",
    ),
    # JWT 模式: eyJxxx.eyJxxx.xxx (三段 base64url,前两段以 eyJ 开头是 base64 编码的 '{"')
    # 整体替换(无分组保留),因为 JWT 三段全是敏感内容
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "***REDACTED_JWT***",
    ),
]


class RedactFilter(logging.Filter):
    """日志凭证脱敏过滤器。

    设计模式:职责链上的一个“改写节点”(decorator over logging pipeline)。
    挂载到 logger 后,所有 log record 的 message 都会经过 ``_SECRET_RULES`` 脱敏。

    无状态:所有规则是模块级常量,filter 实例可安全共享/重复使用。

    Java 类比:Logback 的 ``TurboFilter``(在日志事件创建早期介入),
    或 Servlet ``FilterChain`` 里的一个“敏感词过滤 Filter”。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """脱敏 record message 中的敏感信息。

        重要语义:方法名叫 filter,但这里**永远返回 True**(放行所有日志),
        只是在放行前改写 message。如果返回 False,该条日志会被丢弃,这里不这么做。

        Args:
            record: 一条待输出的日志记录(含 msg 模板 + args)

        Returns:
            恒为 True(放行)。脱敏通过副作用完成:直接修改 record 的字段。

        改写机制
        ========

        ``record.getMessage()`` 拿到**已格式化**的字符串(模板 + args 已合并),
        脱敏后写回 ``record.msg``,并把 ``record.args`` 置 None —— 否则 handler
        会再用旧 args 重新 format,导致脱敏被覆盖。这是 Python logging 的一个
        陷阱:必须同时改 msg 和清 args。
        """
        # getMessage():拿到最终字符串(若 msg 是 "%s" 模板,这里会带入 args 格式化)
        msg = record.getMessage()
        redacted = self._redact(msg)
        if redacted != msg:
            # 替换 record 的 message(直接改对象,handler 后续输出的是脱敏版)
            record.msg = redacted
            # 关键:清空 args,否则 handler 会再用旧 args 重格式化,覆盖脱敏结果
            record.args = None  # 已替换,不再 format
        return True

    @staticmethod
    def _redact(text: str) -> str:
        """对文本依次应用所有脱敏规则。

        顺序就是 ``_SECRET_RULES`` 的列表顺序(顺序敏感,见常量注释)。
        每条规则对“上一步的输出”继续匹配,所以多条规则会叠加生效 —— 例如先脱敏
        Authorization,再脱敏其中残留的 JWT。

        Args:
            text: 原始日志文本

        Returns:
            脱敏后的文本(敏感片段已被占位符替换)。

        Java 类比:类似 ``rules.stream().reduce(text, (t, rule) -> rule.apply(t))``。
        """
        for pattern, replacement in _SECRET_RULES:
            text = pattern.sub(replacement, text)
        return text


def install_redact_filter(logger_name: str = None) -> RedactFilter:
    """安装 RedactFilter 到指定 logger(默认 root),保证幂等。

    Args:
        logger_name: logger 名,None 表示 root logger。
            挂到 root 后,Python logging 的 propagate 机制让所有子 logger 自动生效。

    Returns:
        安装的 RedactFilter 实例(可用于 ``logger.removeFilter()`` 卸载)。

    幂等性
    ======

    多次调用不会重复添加:先扫描 logger.filters,若已有 RedactFilter 直接返回旧实例。
    避免 filter 链里出现多个同样的脱敏 filter(否则同一条日志被脱敏多次,虽然结果一致但浪费 CPU)。
    Java 类比:类似 ``@Bean`` 的单例语义,或 ``if (!initialized)`` 守卫。
    """
    logger = logging.getLogger(logger_name)
    # 幂等检查:避免重复安装(重复安装无害但浪费,且让 filter 链变难调试)
    for existing in logger.filters:
        if isinstance(existing, RedactFilter):
            return existing
    f = RedactFilter()
    logger.addFilter(f)
    return f
