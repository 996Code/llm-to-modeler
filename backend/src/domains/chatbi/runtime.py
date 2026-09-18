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
    """存量双 current 修复(幂等; 表不存在时安全跳过)."""
    """十审 7.4: 存量双 current 修复——建唯一索引前, 收敛 current 到最高版本.

    此前版本允许产生双 is_current=1; 直接 CREATE UNIQUE INDEX 会失败.
    迁移规则: 每数据源保留 MAX(version) 的 current, 其余清零, 记日志.
    """
    with db.connect() as conn:
        # 找有多个 current 的数据源
        dupes = conn.execute(
            "SELECT data_source_id, COUNT(*) AS c "
            "FROM chatbi_semantic_models WHERE is_current = 1 "
            "GROUP BY data_source_id HAVING COUNT(*) > 1").fetchall()
        for d in dupes:
            ds_id = d["data_source_id"]
            # 保留 MAX(version), 其余清零
            conn.execute(
                "UPDATE chatbi_semantic_models SET is_current = 0 "
                "WHERE data_source_id = ? AND is_current = 1 "
                "AND version < (SELECT MAX(version) FROM chatbi_semantic_models "
                "               WHERE data_source_id = ? AND is_current = 1)",
                (ds_id, ds_id))
            import logging
            logging.getLogger(__name__).warning(
                "存量双 current 修复: ds=%s 收敛到最高版本", ds_id)


def _init_pack_schema(db: PackRelationalDB) -> None:
    """幂等建 chatbi 全量表(数据源/语义层/检索栈/记忆/M4)。

    各栈 DDL 均为 CREATE TABLE IF NOT EXISTS, 重复执行无副作用;
    分散在各栈的懒建(如 datasources.init_store)保留作冗余兜底。
    二十九审 P2-C: pack DDL 主动串行化——事务级 advisory lock
    (key 按 pack 名派生, 各 pack 互不干扰)让并发实例排队执行整组
    迁移, 后到者全部 no-op; 此前只靠 DeadlockDetected 退避重试
    恢复(真实出现 3s 退避日志)。事务级锁在提交/回滚都自动释放,
    异常不泄漏; deadlock 重试保留作兜底。
    """
    from domains.chatbi.models import CHATBI_DDL
    from domains.chatbi.stores import CHATBI_RETRIEVAL_DDL
    from domains.chatbi.memory import CHATBI_MEMORY_DDL
    from domains.chatbi.m4 import M4_DDL
    from domains.chatbi.query_stats import QUERY_STATS_DDL
    from domains.chatbi.graph_infer import WATERMARK_DDL
    import time as _time
    # pack 名派生的 advisory lock key(固定 bigint, 仅用于 pack 迁移互斥)
    _lock_key = 0x7061636B0000 + (zlib.crc32(PACK_NAME.encode()) & 0xFFFF)
    ddl = (list(CHATBI_DDL) + list(CHATBI_RETRIEVAL_DDL)
           + list(CHATBI_MEMORY_DDL) + list(M4_DDL)
           + list(QUERY_STATS_DDL) + list(WATERMARK_DDL))
    for attempt in range(5):
        try:
            with db.connect() as conn:
                # 事务级 advisory lock: 事务结束自动释放(异常也释放)
                conn.execute("SELECT pg_advisory_xact_lock(?)",
                             (_lock_key,))
                for stmt in ddl:
                    conn.execute(stmt)
            break
        except Exception as e:
            if "DeadlockDetected" in type(e).__name__ and attempt < 4:
                wait = 3 * (attempt + 1)
                logger.warning("启动建表死锁(多实例并发 DDL), %ds 后重试(%d/5): %s",
                               wait, attempt + 1, e)
                _time.sleep(wait)
                continue
            raise
    _migrate_single_current(db)  # 建表后修复存量双 current(幂等)
    # 十二审 8.1 P0: 唯一索引必须在迁移之后创建——CHATBI_DDL 不含它,
    # 旧库有双current时先建索引会 UniqueViolation 阻断启动
    for attempt in range(5):
        try:
            with db.connect() as conn:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chatbi_semantic_current "
                    "ON chatbi_semantic_models(data_source_id) WHERE is_current = 1")
            break
        except Exception as e:
            if "DeadlockDetected" in type(e).__name__ and attempt < 4:
                _time.sleep(3 * (attempt + 1))
                continue
            raise


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
