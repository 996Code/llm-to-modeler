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
    """pack 关系型存储单例(schema=chatbi;引擎工厂经 main.py 装配注册)。"""
    global _db
    with _db_lock:
        if _db is None:
            _db = PackRelationalDB(PACK_NAME)
        return _db


def get_settings_reader(ctx_or_state) -> Any:
    """插件配置读取器(设置页 > env > schema 默认;经 sdk.pack_api 门面)。"""
    from sdk.pack_api import settings_reader as _sdk_reader
    return _sdk_reader(ctx_or_state, PACK_NAME)


def get_llm(app_state) -> Any:
    """引擎 LLM 客户端(会话上下文之外的后台任务/api 用)。"""
    return app_state.llm_client


def reset_runtime_cache() -> None:
    """测试辅助/卸载钩子:清单例。"""
    global _db
    with _db_lock:
        _db = None
