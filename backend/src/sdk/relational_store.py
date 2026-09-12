"""PackRelationalDB —— SDK 通用关系型存储(PostgreSQL,pack 独立 schema)。

【模块定位】
插件的关系型存储通道:pack 声明自己的表,SDK 负责建 schema、跑 DDL、
提供与平台同语义的连接(共享连接池 + 事务"成功提交/异常回滚")。
补齐"插件唯一合法通道"清单中关系型这一块——此前 LLM(ctx.llm_client)、
上游(ctx.asset_client)、图(graph_store)、向量(vector_store)皆有,
唯 pack 需要 PG 表时只能伸手进平台 services 层(违规:domains → services)。
knowledge_graph 的 KGStore 是第一个参考实现;后续 chatbi 等插件直接取用。

【命名空间隔离】(与 graph_store/vector_store 的隔离哲学配套)
  - 每个 pack 一个 PG schema,名字 = pack 名(目录名),物理隔离:
    knowledge_graph → schema "knowledge_graph",chatbi → schema "chatbi"。
  - DDL 不带 schema 前缀:连接期以事务级 set_config('search_path',
    '<pack>,public') 定向,表自动落在 pack schema;事务结束自动还原
    (池归还后无跨 pack 污染)。public 仍可达(只读平台表的场景)。
  - pack 名必须是合法 PG 标识符(^[a-z][a-z0-9_]*$):schema 名无法参数化,
    严格白名单校验是 SQL 注入的唯一防线,入口 fail-fast。

【连接管理】
复用平台共享连接池(services.db.PgEngine,按 DSN 单例)——pack 不自建池、
不直连 psycopg;引擎工厂由宿主装配期注册(依赖倒置,模式同 sdk.pack_api:
sdk 不在 import 期依赖平台层,宿主未注册时请求期延迟兜底)。
每次 connect() = 池借一条连接 + 事务级 search_path 定向;借还/事务/占位符
翻译(``?``→``%s``)语义与平台 Store 完全一致(同一 _PgConnProxy)。

【Java 类比】
PackRelationalDB ≈ 为每个插件准备独立 DatabaseSchema 的 DataSource 门面;
init_schema ≈ Flyway baseline(幂等 CREATE SCHEMA IF NOT EXISTS + DDL)。

【测试】
与平台 Store 同库同池可直接跑(见 tests/test_pg_backend.py 的 KG 路径);
隔离语义由 conftest 的 schema 感知清表保障。
"""
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

logger = logging.getLogger(__name__)

# pack 名(= schema 名)白名单:小写字母开头,仅小写字母/数字/下划线。
# schema 名无法参数化,这是防 SQL 注入的唯一防线,校验必须 fail-fast。
_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 宿主注册的引擎工厂:(database_url=None) → engine(见 services.db.get_pg_engine)。
# 依赖倒置:sdk 不 import 平台层;未注册时请求期延迟兜底(模式同 sdk.pack_api)。
_engine_factory = None


def register_engine_factory(factory) -> None:
    """宿主装配期注册引擎工厂(main.py 调用一次,生产实现 = services.db.get_pg_engine)。"""
    global _engine_factory
    _engine_factory = factory


def _resolve_engine(database_url: Optional[str] = None) -> Any:
    if _engine_factory is not None:
        return _engine_factory(database_url)
    # 兜底:独立运行(迁移脚本/测试)未经 main.py 装配时,请求期延迟取平台实现
    from services.db import get_pg_engine
    return get_pg_engine(database_url)


class PackRelationalDB:
    """pack 的关系型存储句柄(独立 schema + 共享池)。

    用法(pack 内):

        from sdk.relational_store import PackRelationalDB

        db = PackRelationalDB("my_pack")
        db.init_schema([  # 幂等:CREATE SCHEMA IF NOT EXISTS + 逐条 DDL
            "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, ...)",
        ])
        with db.connect() as conn:          # 事务级 search_path = my_pack,public
            conn.execute("INSERT INTO items (...) VALUES (?, ?)", (...))

    SQL 写法与平台 Store 同规:``?`` 占位符(代理翻译)、LIKE 模式走参数、
    时间戳 ISO 字符串存 TEXT。
    """

    def __init__(self, pack_name: str, database_url: Optional[str] = None):
        if not _PACK_NAME_RE.match(pack_name or ""):
            raise ValueError(
                f"pack 名必须是 ^[a-z][a-z0-9_]*$ 的 PG 标识符(收到: {pack_name!r})——"
                "它会被用作 schema 名,无法参数化,此校验是注入的唯一防线")
        self.pack_name = pack_name
        self.schema = pack_name
        self.engine = _resolve_engine(database_url)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """借一条池连接并定向到本 pack 的 schema(事务级,结束自动还原)。"""
        with self.engine.connect() as conn:
            # set_config 第 3 参 true = 事务级:借还/回滚后自动失效,
            # 池归还不残留跨 pack 的 search_path 污染
            conn.execute(
                "SELECT set_config('search_path', ?, true)",
                (f"{self.schema},public",),
            )
            yield conn

    def init_schema(self, ddl_statements: List[str]) -> None:
        """幂等建 schema + 表(pack schema 内,DDL 不需要 schema 前缀)。"""
        with self.engine.connect() as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        with self.connect() as conn:
            for stmt in ddl_statements:
                conn.execute(stmt)
        logger.info("PackRelationalDB[%s]: schema ready (%d 条 DDL)",
                    self.pack_name, len(ddl_statements))
