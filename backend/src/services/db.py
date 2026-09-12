"""存储引擎 —— PostgreSQL 唯一后端(psycopg3 连接池 + 占位符翻译)。

【模块定位】
各 Store(ConversationStore / TaskStore / PackSettingsStore / KGStore)的
连接层。平台已收敛为 PG-only:DATABASE_URL 必填(缺省 fail-closed,启动即
报错并指引配置),业务 SQL 保持 SQLite 风格(``?`` 占位符)单源书写,
``_PgConnProxy`` 在执行期翻译为 psycopg 的 ``%s``——个别方言点
(jsonb 路径取值/ILIKE/RETURNING)留在各 Store 里的小分支。

历史:曾是 SQLite/PG 双后端(SQLite 为零依赖默认);收敛决策后 SQLite
路径移除,存量数据迁移见 scripts/migrate_sqlite_to_pg.py。

【Java 类比】
PgEngine ≈ HikariCP DataSource;``with db.connect() as conn`` ≈
try-with-resources 借还连接(池借池还,事务语义"成功提交/异常回滚")。

【方言铁律】
- 占位符翻译只做 ``sql.replace("?", "%s")``:SQL 文本里 ``?`` 只允许作
  占位符出现(LIKE 模式等含 ``%`` 的文本必须走参数绑定,不能内联——
  psycopg 带参执行会扫描 SQL 里的 ``%``)。
- 行工厂用 dict_row:``row["col"]`` / ``dict(row)`` / ``.get()`` 与
  原 sqlite3.Row 访问模式同构,Store 的 _row_to_* 映射零改动。
- 时间戳沿用 ISO 字符串存 TEXT(与历史 SQLite 数据零漂移,迁移脚本
  原样拷贝),不在存储层做 TIMESTAMPTZ 转换。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


def resolve_database_url(explicit: Optional[str] = None) -> Optional[str]:
    """解析 PG 连接串:显式参数 > DATABASE_URL env;空串视为未配置。"""
    url = (explicit or os.getenv("DATABASE_URL", "") or "").strip()
    return url or None


def require_database_url() -> str:
    """PG-only 必填校验(fail-closed)。

    缺 DATABASE_URL 时启动即报错——宁可拒绝启动也不能静默跑出"无处落库"
    的服务。错误信息带配置指引(.env.example)与本地快速起库命令。
    """
    url = resolve_database_url()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未配置:平台存储为 PostgreSQL 必填(fail-closed)。"
            "在 .env 配置 DATABASE_URL=postgresql://user:pass@host:5432/db"
            "(模板见 .env.example);本地快速起库:"
            "docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev postgres:16-alpine。"
            "存量 SQLite 数据迁移见 backend/scripts/migrate_sqlite_to_pg.py。"
        )
    return url


class _PgConnProxy:
    """psycopg 连接的轻代理:execute/executemany 时翻译占位符,其余透传。

    Store 代码只通过 ``conn.execute(sql, params)`` / ``conn.executemany``
    持久化,不直接持有 cursor/连接对象——代理面因此只有两个方法。

    占位符翻译的边界纪律:
    - 仅在 ``params is not None`` 时做 ``?`` → ``%s`` 替换。无参调用
      (DDL 等)不替换——psycopg 无参路径不扫描 ``%``;若有人误把带 ``?``
      占位符的 SQL 无参调用,PG 会把 ``?`` 当语法错误响亮报出,而不是
      静默改写字面 ``?``(评审 Minor:无条件替换曾会把 SQL 常量里的
      字面 ``?`` 静默改成 ``%s`` 造成数据损坏)。
    - SQL 文本里不允许内联含 ``%`` 的文本(LIKE 模式等必须走参数绑定,
      psycopg 带参执行会扫描 SQL 里的 ``%``)。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, params: Optional[Any] = None) -> Any:
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql.replace("?", "%s"), params)

    def executemany(self, sql: str, params_seq: Any, **kwargs: Any) -> Any:
        # sqlite3.Connection.executemany 存在而 psycopg3.Connection 没有
        # (那是 Cursor 的方法)——KGStore.replace_chunks 等批量路径依赖它
        with self._conn.cursor() as cur:
            return cur.executemany(sql.replace("?", "%s"), params_seq, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class PgEngine:
    """PostgreSQL 引擎(psycopg3 连接池:借还连接,事务自动提交/回滚)。

    池参数经 PG_POOL_MIN/PG_POOL_MAX 可调,默认 2/8——管理端+对话流
    的并发画像(同步调用、毫秒级操作)足够,过大反而占 PG 连接配额。

    故障态护栏(评审 Important:默认 30s 池超时会把单请求失败放大成
    全服务冻结,必须收紧):
      - 池借出超时 5s(PG_POOL_TIMEOUT 可调):不可达/耗尽时快速失败;
      - connect_timeout=3s:建连黑洞快速失败;
      - statement_timeout=10s:单条 SQL 黑洞快速失败。

    启动探活:构造期做一次真实 checkout + SELECT 1(池 open=True 只起
    后台 worker,不等待也不报错——真正的连接错误要到首次借出才暴露,
    若无探活,启动表现为 30s 假死后抛不带根因的 PoolTimeout)。
    """

    dialect = "postgres"

    def __init__(self, dsn: str, min_size: Optional[int] = None, max_size: Optional[int] = None):
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row

        min_size = min_size if min_size is not None else int(os.getenv("PG_POOL_MIN", "2"))
        max_size = max_size if max_size is not None else int(os.getenv("PG_POOL_MAX", "8"))
        checkout_timeout = float(os.getenv("PG_POOL_TIMEOUT", "5"))
        self.dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=checkout_timeout,          # 借出超时:默认 30s 收紧到 5s
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 3,           # 建连黑洞快速失败
                "options": "-c statement_timeout=10000",  # 单条 SQL 护栏
            },
            open=True,  # 显式声明(psycopg_pool 3.3+ 默认值将翻转,防未来踩坑)
        )
        # 启动探活:借一条连接跑 SELECT 1,失败包装成带指引的 RuntimeError
        # (根因在 psycopg.pool 的 error 日志行,一并提示运维看日志)
        try:
            with self._pool.connection(timeout=checkout_timeout) as conn:
                conn.execute("SELECT 1")
        except Exception as e:
            self._pool.close()
            raise RuntimeError(
                f"PostgreSQL 不可达或凭据错误(DSN: ...{dsn.split('@')[-1]}): {e}\n"
                "请检查 DATABASE_URL 与 PG 服务状态;认证/网络根因见本进程日志中"
                " psycopg.pool 的 error 行。"
            ) from e
        logger.info("PgEngine: pool ready (min=%d max=%d, 探活通过)", min_size, max_size)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            yield _PgConnProxy(conn)

    def close(self) -> None:
        self._pool.close()


# 同 DSN 的 PgEngine 共享一个池:main.py 装配 4 个 Store + KG runtime
# 懒建单例,若各开各池就是 5×min_size 条常驻连接——按 DSN 记账共享,
# 全进程一个池(引用计数归零前不 close;进程退出自然释放)。
_pg_engine_cache: dict = {}


def get_pg_engine(database_url: Optional[str] = None) -> PgEngine:
    """取共享引擎(必填校验走 require_database_url)。"""
    dsn = require_database_url() if not database_url else resolve_database_url(database_url) or require_database_url()
    eng = _pg_engine_cache.get(dsn)
    if eng is None:
        logger.info("storage backend: PostgreSQL (%s)", dsn.split("@")[-1])
        eng = PgEngine(dsn)
        _pg_engine_cache[dsn] = eng
    return eng
