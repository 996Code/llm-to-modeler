"""knowledge_graph 运行时粘合层 —— 从 app.state 取平台组件 + 构建插件自己的存储。

【职责】
  把"平台组件(app.state 上的 settings_store / llm_client) + 插件自有存储
  (KGStore / Neo4jGraphStore / MilvusVectorStore)"收敛成一组简单取用函数,
  供 api.py / tasks.py 共用,避免各自拼装。

【连接生命周期】
  KGStore:懒建单例(按 DATABASE_PATH 指纹);
  Graph/Vector:走 graph_store / vector_store 里的指纹缓存单例——设置页热改
  连接配置后下一次取用即重建连接。
"""
import threading
from pathlib import Path
from typing import Any

from domains.knowledge_graph.store import KGStore
from services.pack_settings import PackSettingsReader

PACK_NAME = "knowledge_graph"

# 原始上传文件的落盘目录(相对工作目录,与 data/conversations.db 同级)
FILES_DIR_DEFAULT = "data/kg/files"

_kg_store: Any = None
_kg_store_fp: str = ""
_kg_store_lock = threading.Lock()


def settings_reader(app_state) -> PackSettingsReader:
    """插件配置读取器(每次读实时解析:设置页热改即时生效)。"""
    return PackSettingsReader(PACK_NAME, getattr(app_state, "settings_store", None))


def get_kg_store(app_state) -> KGStore:
    """元数据存储单例(与 conversations.db 同库)。"""
    global _kg_store, _kg_store_fp
    import os
    db_path = os.getenv("DATABASE_PATH", "data/conversations.db")
    with _kg_store_lock:
        if _kg_store is None or _kg_store_fp != db_path:
            _kg_store = KGStore(db_path)
            _kg_store_fp = db_path
        return _kg_store


def get_graph(app_state):
    """Neo4j 图存储(SDK 单例,经 stores 适配层注入 kg 前缀)。"""
    from domains.knowledge_graph import stores
    return stores.get_graph(app_state)


def get_vector(app_state):
    """Milvus 向量存储(同上)。"""
    from domains.knowledge_graph import stores
    return stores.get_vector(app_state)


def files_root() -> Path:
    import os
    root = Path(os.getenv("KG_FILES_DIR", FILES_DIR_DEFAULT))
    root.mkdir(parents=True, exist_ok=True)
    return root


def reset_runtime_cache() -> None:
    """测试辅助:清 KGStore 单例。"""
    global _kg_store, _kg_store_fp
    with _kg_store_lock:
        _kg_store, _kg_store_fp = None, ""
