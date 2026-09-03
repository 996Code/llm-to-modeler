"""PackState - pack 启停状态的运行时管理 + JSON 持久化。

【模块定位】
PACKS_ENABLED(env)只能"改配置重启生效"。本模块把启停状态变成运行时可变、
跨重启持久的数据,管理端(api/admin.py)的插件开关就是改这里的内存态 +
落盘,再由 services/pack_manager.py 热切换引擎装配。

【状态来源优先级】(首次构造时解析,之后以内存态为准)
  1. 状态文件存在 → 文件里的 enabled 集合(管理端操作过的就以此为准,
     此时 env PACKS_ENABLED 不再生效——避免"界面开了、重启又关回去")
  2. 状态文件不存在 + env PACKS_ENABLED 已配置 → env 作为初始默认值
  3. 状态文件不存在 + env 未配置 → 全部发现的 pack 都启用(向后兼容)

【持久化】
  - 文件格式:{"version": 1, "enabled": ["pack_a", ...]}
  - 原子写:先写同目录临时文件再 os.replace(崩溃不会留下半截 JSON)
  - 默认路径 data/pack_state.json(与 conversations.db 同目录,随 deploy/data
    bind mount 一起持久化);可用 PACK_STATE_PATH 覆盖
  - 与磁盘上已不存在的 pack(目录被删/改名)自动解耦:交集清洗 + 告警

【线程安全】
  threading.Lock 保护读改写。单进程 uvicorn 下足够;切换是低频管理操作。
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 状态文件 schema 版本(未来字段变更时做迁移判断)
_STATE_VERSION = 1


def env_pack_whitelist() -> Optional[Set[str]]:
    """读取 env PACKS_ENABLED 白名单;未配置返回 None(= 不限制)。

    与 domains._packs_whitelist 同逻辑。这里独立实现,保持 services 层
    不反向 import domains(services 只在 pack_manager 里按需 import domains)。
    """
    raw = os.getenv("PACKS_ENABLED", "").strip()
    if not raw:
        return None
    return {n.strip() for n in raw.split(",") if n.strip()}


class PackState:
    """pack 启停状态(内存态 + JSON 落盘)。

    【Java 类比】
    类似 Spring 的 RefreshScope 配置 Bean:字段可运行时刷新,
    每次刷新持久化到外部存储(这里是 JSON 文件),重启后还原。
    """

    def __init__(self, state_path: str, discovered: List[str]):
        """初始化状态。

        Args:
            state_path: 状态文件路径(父目录不存在会自动创建)。
            discovered: 当前扫描到的全部 pack 名(domains.scan_pack_dirs 的结果,
                不带 env 过滤——"全部启用"的默认值需要完整清单)。
        """
        self._path = Path(state_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._discovered: List[str] = sorted(set(discovered))

        persisted = self._read_file()
        if persisted is not None:
            self._source = "file"
            enabled: Set[str] = persisted
        else:
            env_names = env_pack_whitelist()
            if env_names is not None:
                self._source = "env"
                enabled = set(env_names)
            else:
                self._source = "all"
                enabled = set(self._discovered)

        # 交集清洗:状态里引用了磁盘上不存在的 pack(被删/改名)→ 丢弃并告警
        unknown = enabled - set(self._discovered)
        if unknown:
            logger.warning(f"pack 状态引用了未发现的 pack(已忽略): {sorted(unknown)}")
        self._enabled: Set[str] = enabled & set(self._discovered)

        logger.info(
            f"PackState initialized: source={self._source}, "
            f"enabled={sorted(self._enabled)} of {len(self._discovered)} discovered"
        )

    # ── 读 ──────────────────────────────────────────────

    @property
    def state_path(self) -> str:
        return str(self._path)

    @property
    def source(self) -> str:
        """初始状态来源:file(管理端落盘)/ env(PACKS_ENABLED)/ all(全量默认)。"""
        return self._source

    def discovered_names(self) -> List[str]:
        """全部已发现的 pack 名(含禁用的,排序稳定供 UI 展示)。"""
        with self._lock:
            return list(self._discovered)

    def enabled_names(self) -> Set[str]:
        """当前启用的 pack 名集合。"""
        with self._lock:
            return set(self._enabled)

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            return name in self._enabled

    def is_discovered(self, name: str) -> bool:
        with self._lock:
            return name in self._discovered

    def status(self) -> List[Dict[str, object]]:
        """逐 pack 的启停状态(管理端列表用)。"""
        with self._lock:
            return [
                {"name": n, "enabled": n in self._enabled}
                for n in self._discovered
            ]

    # ── 写 ──────────────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用一个 pack 并持久化。

        Args:
            name: pack 名(必须已发现,否则抛 KeyError)。
            enabled: True 启用 / False 禁用。

        Returns:
            状态是否发生变化(False = 本来就是目标状态,未落盘)。
        """
        with self._lock:
            if name not in self._discovered:
                raise KeyError(f"unknown pack: {name}")
            changed = (name in self._enabled) != enabled
            if changed:
                if enabled:
                    self._enabled.add(name)
                else:
                    self._enabled.discard(name)
                self._write_file_locked()
            return changed

    # ── 持久化 ──────────────────────────────────────────

    def _read_file(self) -> Optional[Set[str]]:
        """读状态文件;不存在或损坏返回 None(视为"从未落盘",走 env/all 默认)。

        文件损坏(半截 JSON)时告警并重置——状态文件只影响启停开关,
        重建成本低于人工修复。
        """
        if not self._path.exists():
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("enabled")
            if not isinstance(names, list):
                raise ValueError(f"invalid 'enabled' field: {type(names)}")
            return {str(n) for n in names}
        except Exception as e:
            logger.warning(f"pack 状态文件损坏,将按默认重新初始化({self._path}): {e}")
            return None

    def _write_file_locked(self):
        """落盘当前 enabled 集合(调用方须已持有锁)。原子写:tmp + os.replace。"""
        payload = {
            "version": _STATE_VERSION,
            "enabled": sorted(self._enabled),
        }
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
