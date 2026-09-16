"""chatbi 插件运行时粘合层(模式照抄 knowledge_graph/runtime.py)。

职责: pack 独立 schema 的 PackRelationalDB 单例 + 设置读取器 + LLM 取用,
供 tools/api/tasks 共用,避免各自拼装。
"""
import threading
from typing import Any

from sdk.relational_store import PackRelationalDB

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
            _db = PackRelationalDB(PACK_NAME)
            _init_pack_schema(_db)
        return _db


def _init_pack_schema(db: PackRelationalDB) -> None:
    """幂等建 chatbi 全量表(数据源/语义层/检索栈/记忆/M4)。

    各栈 DDL 均为 CREATE TABLE IF NOT EXISTS, 重复执行无副作用;
    分散在各栈的懒建(如 datasources.init_store)保留作冗余兜底。
    """
    from domains.chatbi.models import CHATBI_DDL
    from domains.chatbi.stores import CHATBI_RETRIEVAL_DDL
    from domains.chatbi.memory import CHATBI_MEMORY_DDL
    from domains.chatbi.m4 import M4_DDL
    from domains.chatbi.query_stats import QUERY_STATS_DDL
    from domains.chatbi.graph_infer import WATERMARK_DDL
    db.init_schema(list(CHATBI_DDL) + list(CHATBI_RETRIEVAL_DDL)
                   + list(CHATBI_MEMORY_DDL) + list(M4_DDL)
                   + list(QUERY_STATS_DDL) + list(WATERMARK_DDL))


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
