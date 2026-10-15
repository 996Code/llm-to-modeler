"""平台嵌入缓存(LRU+TTL,进程内)——重复文本零出站。

两级缓存蓝图的进程内层;Redis 层暂不实现(依赖未装,单进程部署
进程内已覆盖主收益,见蓝图 P1#8 遗留记录)。
"""
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple


class EmbedCache:
    """线程安全 LRU+TTL 缓存:get 命中即续命(move_to_end),
    put 超上限淘汰最旧(popitem(last=False)),过期项惰性删除。"""

    def __init__(self, max_entries: int = 4096, ttl_seconds: int = 86400):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._data: "OrderedDict[str, Tuple[float, List]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[List]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, val = item
            if time.monotonic() - ts > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return val

    def put(self, key: str, vectors: List) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), vectors)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


def embed_cache_key(model: str, mode: str, texts: List[str]) -> str:
    """缓存键:model+mode+文本列表——模型切换/查询文档分侧不串味。"""
    h = hashlib.sha256()
    h.update((model or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update((mode or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update("\x1f".join(texts).encode("utf-8", "ignore"))
    return h.hexdigest()
