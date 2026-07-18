"""ConversationManager — 多轮/压缩/存储。

阶段 0:空壳。阶段 4 实现 append/load/save/list_meta/compress。
当前实际存储仍由 services/conversation_store.py 负责。
"""
from typing import Any


class ConversationManager:
    """阶段 0 占位。阶段 4 实现 append-only 事件流 + 压缩 sidechain。"""

    def __init__(self, store: Any = None):
        self._store = store  # 委托给现有 ConversationStore
