"""chatbi 插件运行时粘合层(模式照抄 knowledge_graph/runtime.py)。

职责: pack 独立 schema 的 PackRelationalDB 单例 + 设置读取器 + LLM 取用,
供 tools/api/tasks 共用,避免各自拼装。
"""
import logging
import threading
import zlib
from typing import Any

from sdk.relational_store import PackRelationalDB

logger = logging.getLogger(__name__)

PACK_NAME = "chatbi"

_db: PackRelationalDB | None = None
_db_lock = threading.Lock()


def get_pack_db() -> PackRelationalDB:
    """pack 关系型存储单例(schema=chatbi;引擎工厂经 main.py 装配注册)。

    首次构建即幂等建全量 pack 表(对标 KGStore 构造期 _init_db):
    全新部署用户第一个请求(列数据源)不能因表不存在而 500。
    """
    global _db
    with _db_lock:
        if _db is None:
            candidate = PackRelationalDB(PACK_NAME)
            _init_pack_schema(candidate)  # 十审 7.4: 失败不缓存半初始化实例
            _db = candidate               # 只有成功才赋值单例
        return _db


def _migrate_single_current(db: PackRelationalDB) -> None:
    """存量双 current 修复(独立入口保留兼容; 幂等; 表不存在时安全跳过).

    三十审起 _init_pack_schema 已把本迁移纳入 migrate_locked 的
    同一锁保护事务(_migrate_single_current_on_conn); 本函数保留
    供旧调用/测试单独触发。
    """
    with db.connect() as conn:
        _migrate_single_current_on_conn(conn)


def _init_pack_schema(db: PackRelationalDB) -> None:
    """幂等建 chatbi 全量表(数据源/语义层/检索栈/记忆/M4)。

    各栈 DDL 均为 CREATE TABLE IF NOT EXISTS, 重复执行无副作用;
    分散在各栈的懒建(如 datasources.init_store)保留作冗余兜底。
    二十九审 P2-C: pack DDL 主动串行化——事务级 advisory lock
    (key 按 pack 名派生, 各 pack 互不干扰)让并发实例排队执行整组
    迁移, 后到者全部 no-op; 此前只靠 DeadlockDetected 退避重试
    恢复(真实出现 3s 退避日志)。事务级锁在提交/回滚都自动释放,
    异常不泄漏; deadlock 重试保留作兜底。
    三十审 P1-A: 改走 db.migrate_locked()——此前为加锁绕过
    init_schema 直接用 connect() 执行 DDL, 但 connect() 只设置
    search_path 不创建 schema, 全新库上无前缀 CREATE TABLE 静默
    落入 public(真实 PG 复现)。migrate_locked 在同一锁保护事务内
    完成 CREATE SCHEMA + search_path 定向 + 全部迁移。
    三十审 P2/P3-D: _migrate_single_current 与唯一索引创建一并
    纳入同一锁保护迁移序列(此前在锁外, "整段迁移被串行化"的
    承诺不完整); retry 次数/退避配置化(PACK_DDL_RETRY_*).
    """
    from domains.chatbi.models import CHATBI_DDL
    from domains.chatbi.stores import CHATBI_RETRIEVAL_DDL
    from domains.chatbi.memory import CHATBI_MEMORY_DDL
    from domains.chatbi.m4 import M4_DDL
    from domains.chatbi.query_stats import QUERY_STATS_DDL
    from domains.chatbi.graph_infer import WATERMARK_DDL
    from sdk.env_config import parse_int_env, parse_float_env
    import time as _time
    # pack 名派生的 advisory lock key(固定 bigint, 仅用于 pack 迁移互斥)
    _lock_key = 0x7061636B0000 + (zlib.crc32(PACK_NAME.encode()) & 0xFFFF)
    # 三十一审 P1-A: 次数用严格 int parser(backoff 用 float)——
    # 此前共用无类型 float helper, 合法配置 5 被解析成 5.0 进
    # range() 抛 TypeError, 真实 Uvicorn 上 ChatBI 全部 API 500
    attempts = parse_int_env("PACK_DDL_RETRY_ATTEMPTS", 5, minimum=1)
    backoff_base = parse_float_env("PACK_DDL_RETRY_BACKOFF_SECONDS",
                                   3.0, minimum=0.0)
    ddl = (list(CHATBI_DDL) + list(CHATBI_RETRIEVAL_DDL)
           + list(CHATBI_MEMORY_DDL) + list(M4_DDL)
           + list(QUERY_STATS_DDL) + list(WATERMARK_DDL))

    def _migrate_all(conn):
        # 基础 DDL(全部 CREATE ... IF NOT EXISTS, 幂等)
        for stmt in ddl:
            conn.execute(stmt)
        # 十一审 7.1 P0: 先建表再做存量迁移(此前迁移在建表前,
        # 全新库查不存在的表直接 UndefinedTable 崩启动)
        _migrate_single_current_on_conn(conn)
        # 十二审 8.1 P0: 唯一索引必须在迁移之后创建——旧库有双
        # current 时先建索引会 UniqueViolation 阻断启动
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_chatbi_semantic_current "
            "ON chatbi_semantic_models(data_source_id) WHERE is_current = 1")

    for attempt in range(1, attempts + 1):
        try:
            db.migrate_locked(_lock_key, _migrate_all)
            return
        except Exception as e:
            if ("DeadlockDetected" in type(e).__name__
                    and attempt < attempts):
                wait = backoff_base * attempt
                logger.warning("启动建表死锁(多实例并发 DDL), %.1fs 后"
                               "重试(%d/%d): %s", wait, attempt, attempts, e)
                _time.sleep(wait)
                continue
            raise


def validate_pack_runtime_config() -> None:
    """pack 运行配置的启动期校验(三十二审 P2: fail-fast 名实相符)。

    此前 PACK_DDL_RETRY_* 只在首次访问 pack DB(_init_pack_schema)
    时才解析——非法值(如 attempts=1.9)让服务正常启动, 首次业务
    请求才 500, 与代码注释声称的"启动失败(fail-fast)"不符。
    本函数在 pack 装配阶段(create_registry)调用, 非法配置直接
    阻止 pack 就绪——错误在启动日志可见, 不等流量进来才发现。

    三十二审 P3: PostgreSQL 16 为硬性运行要求(租约脏数据隔离
    依赖 pg_input_is_valid, PG15 及以下无该函数且无法用
    `fn() OR TRUE` 绕过解析期 UndefinedFunction)。装配期探测
    目标库版本, 不满足直接阻止 pack 就绪——比名义 fallback
    (运行期静默退化成不安全分支)诚实且安全。
    """
    from sdk.env_config import parse_int_env, parse_float_env
    parse_int_env("PACK_DDL_RETRY_ATTEMPTS", 5, minimum=1)
    parse_float_env("PACK_DDL_RETRY_BACKOFF_SECONDS", 3.0, minimum=0.0)
    _require_pg16()


def _require_pg16() -> None:
    """校验目标 PostgreSQL >= 16(装配期; 不满足抛 PackIncompatibleError)。

    三十三审 P2: 版本探测与 schema 初始化拆开——此前 _require_pg16
    走 get_pack_db()(连带全量 schema 迁移), 任何连接/权限/DDL/
    迁移异常都被宽泛 except 当成"版本探测稍后再试"返回成功
    (真实复现: 迁移失败仍 validate_returned_success=True)。
    现在:
      - 只读版本检查走底层 engine(不触发 pack schema 初始化);
      - 版本查询本身失败 = fail-closed 抛错(平台 DB 在 lifespan
        前段已探活, 装配期再失败不是正常部署顺序, 不能当
        unknown 等价通过);
      - PG<16 抛 PackIncompatibleError(loader 不吞, 服务启动失败)。
    """
    from sdk.pack_api import PackIncompatibleError
    from sdk import relational_store as _rs
    try:
        engine = _rs.PackRelationalDB(PACK_NAME).engine
        with engine.connect() as conn:
            row = conn.execute(
                "SELECT current_setting('server_version_num') AS v"
            ).fetchone()
    except Exception as e:
        raise PackIncompatibleError(
            f"chatbi 无法确认 PostgreSQL 版本(数据库不可达/权限"
            f"不足): {e}——平台数据库应在启动前段已探活, 装配期"
            f"失败不是正常部署顺序, 终止启动(fail-closed)") from e
    ver = int(row["v"]) if row else 0
    if ver < 160000:
        raise PackIncompatibleError(
            f"chatbi 需要 PostgreSQL >= 16(当前 {ver // 10000}.{ver % 10000 // 100}, "
            f"server_version_num={ver})——租约脏数据隔离依赖 "
            f"pg_input_is_valid(PG16+)。请升级数据库或回退应用版本")


def _migrate_single_current_on_conn(conn) -> None:
    """存量双 current 修复(在迁移锁事务内执行; 幂等)."""
    dupes = conn.execute(
        "SELECT data_source_id, COUNT(*) AS c "
        "FROM chatbi_semantic_models WHERE is_current = 1 "
        "GROUP BY data_source_id HAVING COUNT(*) > 1").fetchall()
    for d in dupes:
        ds_id = d["data_source_id"]
        conn.execute(
            "UPDATE chatbi_semantic_models SET is_current = 0 "
            "WHERE data_source_id = ? AND is_current = 1 "
            "AND version < (SELECT MAX(version) FROM chatbi_semantic_models "
            "               WHERE data_source_id = ? AND is_current = 1)",
            (ds_id, ds_id))
        logger.warning("存量双 current 修复: ds=%s 收敛到最高版本", ds_id)


def get_settings_reader(ctx_or_state) -> Any:
    """插件配置读取器(设置页 > env > schema 默认;经 sdk.pack_api 门面)。"""
    from sdk.pack_api import settings_reader as _sdk_reader
    return _sdk_reader(ctx_or_state, PACK_NAME)


def get_llm(app_state) -> Any:
    """引擎 LLM 客户端的 pack 契约适配(后台任务/api 用)。

    引擎 chat 返回 str, pack 各栈按 (content, meta) 契约编写——
    统一经 LLMCompat 转换(见 llm_compat.py 模块文档的"unpack 症状")。"""
    from domains.chatbi.llm_compat import LLMCompat
    return LLMCompat(app_state.llm_client)


def reset_runtime_cache() -> None:
    """测试辅助/卸载钩子:清单例。"""
    global _db
    with _db_lock:
        _db = None
