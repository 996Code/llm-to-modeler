"""metrology_assistant 运行时粘合层(模式照抄 chatbi/runtime.py)。

职责: pack 独立 schema 的 PackRelationalDB 单例 + 场景配置读取,
供 tools/api 共用,避免各自拼装。

P1 工程化(设计 §8.1/§8.3):场景/槽位/规则/来源映射从代码常量
迁入 assistant_config 表——DB 唯一事实源,管理页维护,运行时读 DB;
YAML 只留插件声明与出厂默认(首启种子,已有配置不覆盖)。
"""
import logging
import threading
import zlib

from sdk.relational_store import PackRelationalDB

logger = logging.getLogger(__name__)

PACK_NAME = "metrology_assistant"

_db: PackRelationalDB | None = None
_db_lock = threading.Lock()

# 场景配置缓存:配置读取走"每请求读 DB"太重(每轮对话至少读一次),
# 但配置变更必须热生效(设计 §8.1"管理页保存即生效")。折中:进程内
# 缓存 + revision 比对——读缓存前比 DB 当前 revision,变了才重载。
# revision 是单调 int,比对成本一次索引查询,可接受。
_config_cache: dict | None = None
_config_cache_rev: int = -1
# 兜底知识库名(配置文档顶层 fallback_kb;空串=未配置,未命中直接引导)
_config_fallback_kb: str = ""
_config_cache_lock = threading.Lock()


def get_pack_db() -> PackRelationalDB:
    """pack 关系型存储单例(schema=metrology_assistant)。

    首次构建即幂等建全量 pack 表;失败不缓存半初始化实例(照抄
    chatbi 同款约定)。
    """
    global _db
    with _db_lock:
        if _db is None:
            candidate = PackRelationalDB(PACK_NAME)
            _init_pack_schema(candidate)
            _db = candidate
        return _db


def _init_pack_schema(db: PackRelationalDB) -> None:
    """幂等建 metrology_assistant 全量表(配置/flow)。

    DDL 全部 CREATE TABLE IF NOT EXISTS,重复执行无副作用;
    advisory lock key 按 pack 名派生,与 chatbi/kg 的锁互不干扰。
    """
    from domains.metrology_assistant.store import METROLOGY_DDL
    # pack 名派生的 advisory lock key(固定 bigint, 仅用于 pack 迁移互斥)
    _lock_key = 0x7061636B0000 + (zlib.crc32(PACK_NAME.encode()) & 0xFFFF)

    def _migrate_all(conn):
        for stmt in METROLOGY_DDL:
            conn.execute(stmt)

    db.migrate_locked(_lock_key, _migrate_all)


def reset_runtime_cache() -> None:
    """测试辅助:清 DB 单例与配置缓存。"""
    global _db, _config_cache, _config_cache_rev, _config_fallback_kb
    with _db_lock:
        _db = None
    with _config_cache_lock:
        _config_cache, _config_cache_rev = None, -1
        _config_fallback_kb = ""


def cached_config() -> dict | None:
    """当前缓存的场景表(无缓存 None)。"""
    with _config_cache_lock:
        return _config_cache


def cached_revision() -> int:
    """缓存对应的配置 revision。"""
    with _config_cache_lock:
        return _config_cache_rev


def set_cached_config(table: dict, revision: int, fallback_kb: str = "") -> None:
    """写入缓存(scenario_config 装载成功后调用)。"""
    global _config_cache, _config_cache_rev, _config_fallback_kb
    with _config_cache_lock:
        _config_cache = table
        _config_cache_rev = int(revision)
        _config_fallback_kb = str(fallback_kb or "")


def cached_fallback_kb() -> str:
    """缓存对应的兜底知识库名(空串=未配置)。"""
    with _config_cache_lock:
        return _config_fallback_kb


def invalidate_config_cache() -> None:
    """管理页保存成功后失效缓存(下一次请求重读 DB)。"""
    global _config_cache, _config_cache_rev, _config_fallback_kb
    with _config_cache_lock:
        _config_cache, _config_cache_rev = None, -1
        _config_fallback_kb = ""
