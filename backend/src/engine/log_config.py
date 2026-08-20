"""集中式日志配置（loguru）—— 对标 Java 的 Logback/Log4j2 文件输出。

模块定位
========

统一接管 root logger 输出：控制台 + 文件双写，文件按大小自动轮转
（rotation）/ 保留份数（retention）。项目所有日志统一落 ``logs/`` 目录，
不再散落在启动目录。基于 loguru（一行声明式配置，对标 logback 的
``RollingFileAppender + SizeBasedTriggeringPolicy + MaxHistory``）。

日志文件布局（默认）::

    <repo>/backend/logs/
    ├── app.log            # 当前日志（INFO 及以上）
    └── app.YYYY-MM-DD_HH-MM-SS_000001.log   # 轮转出的历史文件
        ...

Java 类比
=========

- ``logger.add(sink, rotation="50 MB", retention=5, level="INFO")``
  ≈ logback ``<appender class="RollingFileAppender"> + SizeBasedTriggeringPolicy``。
- ``logger.add(sys.stderr)`` ≈ ``ConsoleAppender``。
- ``logger.remove()`` 后重配 ≈ logback 的 ``reset()``（日志配置可热替换）。

用法
====

::

    from engine.log_config import setup_logging
    setup_logging()                        # main.py 启动时调一次

    from loguru import logger              # 业务代码直接 import loguru 打日志
    logger.info("...")

    与标准库的互通：setup_logging() 会把标准库的日志也拦截进 loguru 统一输出
    （uvicorn/httpx/langgraph 等第三方日志不再漏到 stdout 或散落文件）。

与 RedactFilter 的关系
======================

脱敏是独立环节：loguru 的 sink 只是输出通道，RedactFilter 依旧挂在标准库
root logger（logging_filter.py），脱敏在 loguru 拦截标准库日志时已生效。
main.py 先 setup_logging() 再 install_redact_filter() 即可（顺序无强约束）。

可配置项（环境变量）
====================

- ``LOG_DIR``：日志目录，默认 ``<repo>/backend/logs``（部署时指到挂载卷）。
- ``LOG_LEVEL``：日志级别，默认 ``INFO``。
- ``LOG_FILE_MAX_BYTES``：单文件轮转阈值，默认 50MB（loguru 的 ``rotation``）。
- ``LOG_RETENTION``：保留的历史文件数，默认 5 份（loguru 的 ``retention``）。
"""
import os
import sys
from pathlib import Path

from loguru import logger

# 默认日志根目录：<repo>/backend/logs（与 src/ 平级，随后端部署走）
# __file__ 在 src/engine/ 下，往上两级到 backend/
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"

# 日志目录/级别/轮转阈值均可通过环境变量覆盖（部署时指向挂载卷，如 /var/log/llm-modeler）
LOG_DIR = Path(os.getenv("LOG_DIR", str(_DEFAULT_LOG_DIR)))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE_MAX_BYTES = int(os.getenv("LOG_FILE_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MB
LOG_RETENTION = int(os.getenv("LOG_RETENTION", "5"))

# 控制台/文件统一格式（含脱敏后的真实消息；进程+线程便于排查并发请求）
_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{process}</cyan>:<cyan>{thread.name}</cyan> | "
    "<magenta>{name}</magenta> | <level>{message}</level>"
)

# 状态：防止 pytest/热重载时重复配置（多次 setup_logging 只配一次）
_configured = False


def _capture_stdlib_logging() -> None:
    """把标准库 logging 的日志（uvicorn/httpx/langgraph 等）收进 loguru 统一输出。

    对标 logback：第三方库不走我们的 appender 会漏到 stdout。机制：
    把标准库 root logger 的 handlers 替换成单个 InterceptHandler，转发给 loguru。

    ⚠ Python logging 语义：filters 只作用于**挂载它的 logger 自身**的 record
    （propagate 只向上传 handlers，不过父 logger 的 filters）。所以 RedactFilter
    若只挂 root.filters，httpx/uvicorn 等子 logger 的 record 根本不会过它。
    → 这里在 InterceptHandler.emit 里**显式先跑 RedactFilter 再转发**，
    保证接入 loguru 后脱敏对所有来源（含第三方）依然生效。
    """
    import logging

    from engine.logging_filter import RedactFilter

    class _InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # 先脱敏：RedactFilter 改写 record.msg 并清 args（与旧 basicConfig 路径一致）
            for f in logging.getLogger().filters:
                if isinstance(f, RedactFilter):
                    f.filter(record)
                    break
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # getMessage() 此时拿到的是脱敏后的最终文本
            logger.log(level, record.getMessage())

    logging.getLogger().handlers = [_InterceptHandler()]
    # 标准库 root 默认 WARNING，会把 INFO 的 access 日志在 logger 层拦掉；
    # 放低到与 loguru 同级别，让 InterceptHandler 全量收到，再由 loguru 的
    # sink level 做最终过滤（避免两级过滤口径不一致）。
    logging.getLogger().setLevel(LOG_LEVEL)
    # uvicorn 自带 access/error 两个 logger，propagate 默认 False——
    # 显式置 True 让它们也走 root 的 InterceptHandler，统一进 loguru
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).propagate = True


def setup_logging() -> None:
    """配置 loguru：控制台 + 轮转文件双输出，并接管标准库日志。

    幂等：重复调用不重复加 sink（避免 pytest/热重载/多进程初始化时翻倍）。

    Returns:
        None（副作用：全局日志输出通道）
    """
    global _configured
    if _configured:
        return
    _configured = True

    # 清掉默认的 stderr sink（避免只加文件不清理导致重复），再按需重配
    logger.remove()

    # 文件输出：按大小轮转 + 保留份数（对标 logback RollingFileAppender）。
    # rotation 用人类可读大小（"50 MB"），loguru 自动换算。
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.add(
        LOG_DIR / "app.log",
        rotation=f"{LOG_FILE_MAX_BYTES // (1024 * 1024)} MB",
        retention=LOG_RETENTION,
        level=LOG_LEVEL,
        encoding="utf-8",
        enqueue=True,        # 线程安全：工作线程（graph 线程池）也能安全写日志
        format=_FORMAT,
    )

    # 控制台输出（容器/开发场景，docker logs 可见）
    logger.add(
        sys.stderr,
        level=LOG_LEVEL,
        colorize=True,
        format=_FORMAT,
    )

    _capture_stdlib_logging()
