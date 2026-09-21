"""PackState - pack 启停状态的运行时管理 + JSON 持久化。

【模块定位】
PACKS_ENABLED(env)只能"改配置重启生效"。本模块把启停状态变成运行时可变、
跨重启持久的数据,管理端(api/admin.py)的插件开关就是改这里的内存态 +
落盘,再由 services/pack_manager.py 热切换引擎装配。

【状态来源优先级】(首次构造时解析,之后以内存态为准)
  1. 状态文件存在 → 文件里的 enabled 集合 + 新发现 pack 自动并入
     (管理端操作过的就以此为准,此时 env PACKS_ENABLED 不再生效——
     避免"界面开了、重启又关回去";新部署的 pack 目录默认启用,
     与"缺省全启用"语义一致,新插件即插即用)
  2. 状态文件不存在 + env PACKS_ENABLED 已配置 → env 作为初始默认值
  3. 状态文件不存在 + env 未配置 → 全部发现的 pack 都启用(向后兼容)

【持久化】
  - 文件格式:{"version": 1, "enabled": ["pack_a", ...], "revision": N}
  - 原子写:先写同目录临时文件再 os.replace(崩溃不会留下半截 JSON)
  - 默认路径 data/pack_state.json(data/ 目录,随 deploy/data
    bind mount 一起持久化);可用 PACK_STATE_PATH 覆盖
  - 与磁盘上已不存在的 pack(目录被删/改名)自动解耦:交集清洗 + 告警
  - "新发现自动并入"只发生在构造时的一次性合并:文件里没有、磁盘上
    新出现的 pack 进 enabled;管理端显式禁用(set_enabled False)后,
    文件里就有了它的记录(在 enabled 外),重启不会再被并入

【线程安全】
  部署契约是单实例、单 Uvicorn worker。threading.Lock 保护进程内读改写；
  热切换总锁串行化管理操作。磁盘只负责跨重启恢复，不参与多进程协调。
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
        # _read_file 的产物(文件不存在/损坏时保持空默认)
        self._file_known: Set[str] = set()
        self._file_has_known = False
        # revision 仅用于运维诊断与格式兼容，不承担跨进程 CAS。
        self._revision: int = 0
        self._dirty = False

        persisted = self._read_file()
        if persisted is not None:
            self._source = "file"
            enabled: Set[str] = persisted
            # 新发现并入: 磁盘上新增的 pack 目录默认启用(即插即用)。
            # 判据 = known(文件记录过的全部包名, 含禁用)之外的 discovered。
            # 旧格式(v1 无 known 字段)的固有歧义:"曾显式禁用"与"从未安装"
            # 同形(都不在 enabled), 只能取其一——取"视为新装"(即插即用),
            # 因为新插件部署是每次发版的常态, 而"旧格式+曾禁用"是一次性
            # 迁移场景; 且首次落盘后 known 补全, 语义从此精确。
            legacy = not self._file_has_known
            new_packs = set(self._discovered) - persisted - self._known_names()
            if new_packs:
                logger.info(f"新发现 pack 自动启用: {sorted(new_packs)}")
                enabled = enabled | new_packs
            if legacy or new_packs:
                # 旧格式迁移/新包并入 → 立即落盘补全 known(此后语义精确)
                self._enabled = enabled & set(self._discovered)
                self._write_disk_state(self._enabled)
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

    def set_enabled(self, name: str, enabled: bool,
                    persist: bool = True) -> bool:
        """启用/禁用一个 pack 并持久化。

        Args:
            name: pack 名(必须已发现,否则抛 KeyError)。
            enabled: True 启用 / False 禁用。
            persist: True(默认)立即落盘;False 只改内存并标记 dirty，
                由调用方在 runtime commit 成功后
                调 persist() 落盘(三十六审 P1-A: toggle 的状态持久化
                后置到装配成功之后, 失败路径零落盘)。

        Returns:
            状态是否发生变化(False = 当前进程内存已经是目标值)。
        """
        with self._lock:
            if name not in self._discovered:
                raise KeyError(f"unknown pack: {name}")
            changed = (name in self._enabled) != enabled
            if changed:
                previous = set(self._enabled)
                previous_dirty = self._dirty
                if enabled:
                    self._enabled.add(name)
                else:
                    self._enabled.discard(name)
                self._dirty = True
                if persist:
                    try:
                        self._write_disk_state(self._enabled)
                    except Exception:
                        # 默认直写路径也保持内存/磁盘原子性；管理端事务使用
                        # persist=False，在外层 runtime commit 后单独 persist。
                        self._enabled = previous
                        self._dirty = previous_dirty
                        raise
                    else:
                        self._dirty = False
            return changed

    def discard_unpersisted(self) -> None:
        """内存已恢复旧值后，丢弃本次事务的未落盘标记。"""
        with self._lock:
            self._dirty = False

    def persist(self) -> None:
        """runtime commit 成功后，把当前内存状态原子落盘。"""
        with self._lock:
            if not self._dirty:
                return
            self._write_disk_state(self._enabled)
            self._dirty = False

    # ── 持久化 ──────────────────────────────────────────

    def _read_file(self) -> Optional[Set[str]]:
        """读状态文件;不存在或损坏返回 None(视为"从未落盘",走 env/all 默认)。

        文件损坏(半截 JSON)时告警并重置——状态文件只影响启停开关,
        重建成本低于人工修复。
        同时记录磁盘 revision(写回时递增的基准)。
        """
        if not self._path.exists():
            self._revision = 0
            return None
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("enabled")
            if not isinstance(names, list):
                raise ValueError(f"invalid 'enabled' field: {type(names)}")
            # 全量记录(含已禁用): 用于区分"从未见过的新 pack"与
            # "显式禁用过的 pack"——前者默认启用, 后者保持禁用
            known = data.get("known")
            self._file_has_known = isinstance(known, list)
            self._file_known = {str(n) for n in known} if self._file_has_known else set(names)
            # revision(旧格式无此字段 = 0)
            self._revision = int(data.get("revision") or 0)
            return {str(n) for n in names}
        except Exception as e:
            logger.warning(f"pack 状态文件损坏,将按默认重新初始化({self._path}): {e}")
            self._revision = 0
            return None

    def _known_names(self) -> Set[str]:
        """文件里出现过的全部 pack 名(旧格式无 known 字段 = enabled 集合本身)。"""
        return getattr(self, "_file_known", set())

    def _write_disk_state(self, desired: Set[str]) -> None:
        """把内存权威状态原子写回(临时文件 + os.replace)。"""
        new_enabled = set(desired) & set(self._discovered)
        known = set(self._file_known) | set(self._discovered)
        revision = self._revision + 1
        payload = {
            "version": _STATE_VERSION,
            "revision": revision,
            "enabled": sorted(new_enabled),
            "known": sorted(known),
        }
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
        self._revision = revision
        self._file_known = known
        self._file_has_known = True
