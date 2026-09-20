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

【三十六审 P2-A: 磁盘权威 CAS】
  三十五审的"文件锁 + revision + 实例终身 _touched 合并"有两个确定性
  丢更新反例(审计实测复现):
    A. stale no-op: B 持旧内存(KG=enabled), 磁盘已被 A 改为 disabled;
       B 收到"启用 KG"→ 按旧内存判 changed=False 不落盘 → 磁盘仍 disabled,
       调用方却以为成功了。
    B. 历史 _touched 覆盖: B 曾禁用 KG(KG 永久进 B._touched), 新实例 A
       后来启用 KG(磁盘 enabled), B 再禁用无关 pack 时合并算法因 KG 在
       _touched 里拒绝读磁盘新值 → 把 KG 写回 disabled。
  根因: 以"实例内存"为权威做变更判断与合并。正确语义是**磁盘权威**:
  每次 set_enabled 都在 fcntl 文件锁内重读最新磁盘, 以磁盘判断 changed,
  只应用本次这一个 pack 的操作, revision+1 原子写回, 再同步本实例内存。
  本实例内存从此只是"磁盘的只读缓存"(每次写后刷新), 不再参与决策。

【线程安全】
  threading.Lock 保护读改写。单进程 uvicorn 下足够;切换是低频管理操作。
"""
import fcntl
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
        # 磁盘 revision(写回时递增; 内存只是缓存, 决策一律重读磁盘)
        self._revision: int = 0

        persisted = self._read_file()
        # 三十六审 P2-B: 注册本进程为状态文件持有者(多 worker 检测的
        # 数据源; toggle/recheck 前查 count_state_file_holders)
        # 三十八审 P3: 保存句柄(lifespan shutdown / 测试 teardown 显式 close)
        self.holder = register_state_holder(state_path)
        # 三十七审 P1-A: 读到磁盘状态时同步 _last_seen_disk——
        # persist() 的合并基准。此前只在 _read_disk_locked(写路径)里
        # 设置, 构造读文件后仍为 None → 首次 persist 把 desired 全量
        # 当"本实例变更", 无法表达删除(重启后首次 disable 被撤销)。
        if persisted is not None:
            self._last_seen_disk: Set[str] = set(persisted)
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
            persist: True(默认)立即落盘;False 只改内存并**记录本次精确
                操作**(name, enabled), 由调用方在 runtime commit 成功后
                调 persist() 落盘(三十六审 P1-A: toggle 的状态持久化
                后置到装配成功之后, 失败路径零落盘)。

        Returns:
            状态是否发生变化(False = 磁盘最新状态本来就是目标值)。

        三十六审 P2-A(磁盘权威): changed 的判定基于**文件锁内重读的
        最新磁盘状态**, 不是本实例内存——stale 实例的显式反向操作
        (磁盘 disabled、请求 enable)会被正确判为 changed 并落盘,
        不再出现"changed=False 但磁盘与请求相悖"的三方认知分裂。

        三十七审 P1-A: persist=False 时记录**本次精确操作**——persist()
        落盘不再从整份内存集合推断删除意图(重启后 _last_seen_disk 为
        None 的场景下, "desired 里存在"的项被当作 mine, 无法表达
        "本次禁用了谁", 磁盘随即反弹)。
        """
        with self._lock:
            if name not in self._discovered:
                raise KeyError(f"unknown pack: {name}")
            if persist:
                # 磁盘权威路径: 锁内重读 → 判定 → 应用单 pack → 写回
                return self._set_enabled_disk_authoritative(name, enabled)
            # 内存暂存路径(runtime commit 后由 persist() 统一落盘)
            changed = (name in self._enabled) != enabled
            if changed:
                if enabled:
                    self._enabled.add(name)
                else:
                    self._enabled.discard(name)
                # 三十七审 P1-A: 记录本次精确操作(persist 按操作落盘,
                # 不从内存集合推断)
                self._pending_ops: List[tuple] = getattr(
                    self, "_pending_ops", [])
                # 同 pack 重复操作只保留最后一个(净效果)
                self._pending_ops = [
                    op for op in self._pending_ops if op[0] != name]
                self._pending_ops.append((name, enabled))
            return changed

    def clear_pending_ops(self) -> None:
        """清空未提交的精确操作(三十八审 P1-A)。

        失败请求的回滚路径调用——反向 set_enabled 会把回滚操作也记进
        pending, 若不清空, 后续任何成功 persist 都会重放这些"失败请求
        的意图"(净效果虽是回滚态, 但语义上未提交意图不该跨请求存活)。
        """
        with self._lock:
            self._pending_ops = []

    def persist(self) -> None:
        """把 pending 的精确操作落盘(runtime commit 成功后调用)。

        三十七审 P1-A: 落盘单位是**本次操作**(name, enabled), 不是
        整份内存集合——在文件锁内重读磁盘, 逐条应用 pending 操作,
        revision+1 写回。他人对其他 pack 的写入不受影响(磁盘权威);
        本实例没有 pending 操作时不落盘(纯重装配场景零写入)。

        磁盘从未落盘(rev=0 且无 known)时以**本实例内存**为基准再应用
        操作——与 _set_enabled_disk_authoritative 的 base 语义一致:
        env/all 初始态就是权威起点, 否则首次 persist 会把"内存有、
        磁盘无"的 pack 全部落成禁用(空集起点 + 只减不增)。
        """
        with self._lock:
            ops = list(getattr(self, "_pending_ops", []))
            if not ops:
                return
            lock_path = self._path.with_suffix(".lock")
            with open(lock_path, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    disk_enabled, disk_known, disk_rev = (
                        self._read_disk_locked())
                    if disk_rev > 0 or disk_known:
                        base = set(disk_enabled)
                    else:
                        # 从未落盘: 本实例内存(env/all 起点)是权威基准
                        base = set(self._enabled)
                    new_enabled = set(base)
                    for name, enabled in ops:
                        if enabled:
                            new_enabled.add(name)
                        else:
                            new_enabled.discard(name)
                    self._write_disk_locked(
                        new_enabled, disk_known, disk_rev)
                    self._enabled = new_enabled & set(self._discovered)
                    self._pending_ops = []
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)

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

    # ── 磁盘权威写路径(三十六审 P2-A) ───────────────────

    def _set_enabled_disk_authoritative(self, name: str, enabled: bool) -> bool:
        """set_enabled 的磁盘权威实现(调用方须已持有线程锁)。

        在 fcntl 文件锁内: 重读最新磁盘 → 以磁盘判定 changed →
        只应用本次这一个 pack 的操作 → revision+1 原子写回 →
        同步本实例内存(内存 = 磁盘的只读缓存)。

        从未落盘的 pack(磁盘 known 里没有)以本实例内存为基准判定
        changed——首次 set_enabled 前磁盘无记录, "内存初始态"就是
        权威起点(env/all 默认), 不能按"磁盘空集"判成无变化。
        """
        lock_path = self._path.with_suffix(".lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                disk_enabled, disk_known, disk_rev = self._read_disk_locked()
                if name in disk_known or disk_rev > 0:
                    base = disk_enabled
                else:
                    # 从未落盘: 本实例内存(env/all 起点)是权威基准
                    base = set(self._enabled)
                changed = (name in base) != enabled
                if not changed:
                    # 基准已是目标状态: 只刷新内存缓存(他人可能已改过
                    # 其他 pack), 不落盘不递增 revision。
                    # 三十七审 P2: 磁盘从未落盘(rev=0 且无 known)时
                    # 内存是权威起点, 不能同步成空磁盘(否则 no-op 把
                    # enabled 清空)——保持内存不变。
                    if disk_rev > 0 or disk_known:
                        self._sync_memory_from_disk(disk_enabled)
                    return False
                new_enabled = set(base)
                if enabled:
                    new_enabled.add(name)
                else:
                    new_enabled.discard(name)
                self._write_disk_locked(new_enabled, disk_known, disk_rev)
                self._enabled = new_enabled & set(self._discovered)
                return True
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _write_disk_state(self, desired: Set[str]) -> None:
        """把期望的 enabled 集合落盘(调用方须已持有线程锁)。

        磁盘权威合并: 以锁内重读的磁盘为基准, 应用 desired 相对
        "本实例上次见过的磁盘状态"的变更(= 本实例的显式操作),
        其余 pack 保持磁盘最新值——本实例没动过的 pack 永远不覆盖
        他人的写入(三十六审反例 B 的根因即旧算法用终身 _touched
        判断"动过", 历史触碰会永久屏蔽磁盘新值)。

        上次见过的磁盘状态 = 读盘**前**的 _last_seen_disk 记忆
        (注意必须在 _read_disk_locked 刷新它之前取——读盘会把
        last_seen 更新为当前磁盘, 若在刷新后取, 他人刚写入的
        变更会被误算成本实例的变更而反向覆盖)。
        """
        lock_path = self._path.with_suffix(".lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                # 读盘前快照"上次见过的磁盘"(本实例变更的基准)
                prev_seen = getattr(self, "_last_seen_disk", None)
                disk_enabled, disk_known, disk_rev = self._read_disk_locked()
                if prev_seen is None:
                    # 首次写(构造后未见过磁盘, 如 env/all 起点):
                    # desired 全量作为本实例变更
                    mine = set(desired)
                else:
                    # 本实例自上次见盘以来的净变更(显式操作过的 pack)
                    mine = {n for n in set(desired) ^ set(prev_seen)
                            if n in self._discovered}
                new_enabled = set(disk_enabled)
                for n in mine:
                    if n in desired:
                        new_enabled.add(n)
                    else:
                        new_enabled.discard(n)
                self._write_disk_locked(new_enabled, disk_known, disk_rev)
                self._enabled = new_enabled & set(self._discovered)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _read_disk_locked(self) -> tuple:
        """读最新磁盘状态(调用方须已持有 fcntl 文件锁 + 线程锁)。

        Returns:
            (enabled 集合, known 集合, revision)。文件不存在/损坏时
            返回 (空集, 空集, 0)——首次落盘场景。
        """
        if not self._path.exists():
            self._last_seen_disk: Set[str] = set()
            return set(), set(), 0
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            enabled = {str(n) for n in data.get("enabled") or []}
            known = {str(n) for n in data.get("known") or []} or set(enabled)
            rev = int(data.get("revision") or 0)
        except Exception as e:
            logger.warning(f"pack 状态文件读取失败(按空状态处理): {e}")
            self._last_seen_disk = set()
            return set(), set(), 0
        self._last_seen_disk = set(enabled)
        return enabled, known, rev

    def _write_disk_locked(self, new_enabled: Set[str], disk_known: Set[str],
                           disk_rev: int) -> None:
        """原子写回(调用方须已持有 fcntl 文件锁 + 线程锁)。"""
        known = set(new_enabled) | set(self._discovered) | set(disk_known)
        revision = max(disk_rev, getattr(self, "_revision", 0)) + 1
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
        self._last_seen_disk = set(new_enabled)

    def _sync_memory_from_disk(self, disk_enabled: Set[str]) -> None:
        """把磁盘状态同步进内存缓存(调用方须已持有锁)。"""
        self._enabled = set(disk_enabled) & set(self._discovered)
        self._last_seen_disk = set(disk_enabled)


def count_state_file_holders(state_path: str) -> int:
    """统计当前持有状态文件 fcntl 锁能力的活跃实例数(多 worker 检测)。

    三十六审 P2-B: 文件一致 ≠ 各 worker 运行态一致——多 worker 时
    管理请求只落到一个 worker, 其他 worker 的 registry/nodes/
    handlers/routes 不会自动重装配。动态 pack 管理只支持单 worker;
    检测方式 = 在状态文件旁维护一个"实例心跳文件": 每个进程启动时
    注册一个带 PID 的槽位, 退出时注销(atexit + 心跳超时兜底)。
    槽位数 > 1 时管理端 toggle/recheck 拒绝(503)。

    心跳文件格式(每行一个): {"id": 实例UUID, "ts": epoch_seconds}
    判活: ts 距今 < STALE_SECONDS(纯心跳新鲜度)。

    三十九审 P1-C: 槽位键从 PID 改为**每实例随机 UUID**——容器/Pod
    的主进程 PID 通常都是 1, 同 PID 会互相覆盖槽位(fail-open);
    os.kill 也无法探测其他 PID namespace 的进程。UUID 键 + 心跳
    时间判活对同机多进程与多容器(共享卷)同样有效。
    """
    reg_path = Path(state_path).with_suffix(".holders")
    if not reg_path.exists():
        return 0
    import time
    now = time.time()
    alive = 0
    try:
        with open(reg_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = float(entry.get("ts") or 0)
                except (ValueError, TypeError):
                    continue
                if now - ts > _HOLDER_STALE_SECONDS:
                    continue
                alive += 1
        return alive
    except OSError:
        # 三十七审 P2(fail-closed): 检测失效不能降级成"没有他人"——
        # 返回 -1, 调用方对负数一律拒绝管理操作
        logger.error(f"pack 状态持有者检测失败(fail-closed): {reg_path}")
        return -1


# 心跳过期阈值: 实例每 10s 刷新一次 ts, 60s 未刷新视为已死
_HOLDER_STALE_SECONDS = 60

# 已注册的 holder 线程(三十七审 P2: 按 (pid, state_path) 去重——
# 同进程多次构造 PackState(测试/临时实例)不再各起一条永久心跳线程)
_HOLDER_THREADS: Dict[str, "threading.Thread"] = {}


class StateHolderHandle:
    """holder 生命周期句柄(三十八审 P3 / 三十九审 P2-B / 四十审 P2)。

    close(): 引用计数减一 → 最后一个引用时立即注销槽位
    (_write_slot(unregister=True)) + 停心跳线程 + 从线程表移除 +
    注销**共享的** atexit callback(进程退出不再重建目录); 幂等。

    四十审 P2(callback 所有权): atexit callback 绑定到**共享线程对象**
    (thread._shared_atexit_cb), 每个 thread_key 只注册一次——此前每个
    handle 各持一个 cb, 非最后 close 不注销自己的, 残留 cb 在进程退出
    时 mkdir 重建已删除的目录。现在 handle 不再各自持 cb, 最后一个
    close 注销线程对象上的唯一 cb。
    """

    def __init__(self, thread_key: str, stop_event, thread,
                 unregister_slot=None):
        self._thread_key = thread_key
        self._stop_event = stop_event
        self._thread = thread
        self._unregister_slot = unregister_slot   # 注销槽位的回调
        self._closed = False
        with _HOLDER_LOCK:
            # 引用计数: 同路径共享线程的活跃 handle 数
            _HOLDER_REFCOUNT[thread_key] = (
                _HOLDER_REFCOUNT.get(thread_key, 0) + 1)

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return   # 幂等
        self._closed = True
        with _HOLDER_LOCK:
            remaining = _HOLDER_REFCOUNT.get(self._thread_key, 0) - 1
            if remaining > 0:
                # 还有其他活跃 handle 共享这条心跳线程——只减引用
                _HOLDER_REFCOUNT[self._thread_key] = remaining
                return
            _HOLDER_REFCOUNT.pop(self._thread_key, None)
            # 四十一审 P2: 持锁期间标记 stopping 并从线程表移除——
            # 此前 stop/join/移表在锁外, close 释放锁后 register 仍能
            # 看到"alive"的旧线程并复用, close 随后 stop 它, 产生
            # 持引用但线程已死/槽位已清的孤儿句柄。表先移除,
            # register 在锁内就只会新建完整线程。
            _HOLDER_THREADS.pop(self._thread_key, None)
            self._thread._holder_stopping = True
        # 注销槽位(立即, 不等进程退出; 锁外——内部有自己的文件锁)
        if self._unregister_slot is not None:
            try:
                self._unregister_slot(unregister=True)
            except OSError:
                logger.warning("pack 状态持有者注销失败", exc_info=True)
        # 注销共享 atexit callback(绑在线程对象上, 每个 key 只有一个)
        shared_cb = getattr(self._thread, "_shared_atexit_cb", None)
        if shared_cb is not None:
            _atexit_mod.unregister(shared_cb)
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)


# 同路径 handle 引用计数 + 并发保护(四十审 P2: register/close 竞态)
_HOLDER_REFCOUNT: Dict[str, int] = {}
_HOLDER_LOCK = threading.Lock()

import atexit as _atexit_mod


def register_state_holder(state_path: str) -> "StateHolderHandle":
    """注册本实例为状态文件持有者(启动时调用一次)。

    写入 {id: 实例UUID, ts} 槽位并启动后台心跳线程(每 10s 刷新 ts);
    atexit 注销。多实例检测(count_state_file_holders)据此拒绝动态管理。

    三十七审 P2: 注册失败抛 OSError(fail-closed)。
    三十八审 P3: 返回 handle(close 停线程/注销/移除线程表)。
    三十九审 P1-C: 槽位键 = 每实例随机 UUID(容器/Pod 主进程 PID 都是 1,
    同 PID 会互相覆盖; os.kill 也探不到其他 PID namespace)——hostname/
    PID 仅作诊断字段。三十九审 P2-B: close 有引用计数/即时注销/
    atexit.unregister/幂等; 同进程同路径仍只起一条心跳线程
    (thread_key 用 UUID, 每个实例一个——测试同进程多次构造时
    复用进程级首个 UUID 的线程)。
    """
    import atexit
    import time
    import uuid as _uuid

    reg_path = Path(state_path).with_suffix(".holders")
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    # 实例身份: 进程级单例 UUID(同进程多次构造共享, 跨进程/容器唯一)
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        _INSTANCE_ID = str(_uuid.uuid4())
    instance_id = _INSTANCE_ID
    pid = os.getpid()
    thread_key = f"{instance_id}:{state_path}"

    def _write_slot(unregister: bool = False) -> None:
        # 注册/注销失败上抛(fail-closed); 心跳刷新失败只记日志
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(state_path).with_suffix(".lock")
        try:
            with open(lock_path, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    lines: List[str] = []
                    if reg_path.exists():
                        with open(reg_path, encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    e = json.loads(line)
                                    if str(e.get("id") or "") != instance_id:
                                        lines.append(line)
                                except (ValueError, TypeError):
                                    continue
                    if not unregister:
                        lines.append(json.dumps({
                            "id": instance_id,
                            "ts": time.time(),
                            "pid": pid,          # 诊断字段(不参与判活)
                            "host": _hostname(),
                        }))
                    tmp = reg_path.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + ("\n" if lines else ""))
                    os.replace(tmp, reg_path)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except OSError:
            if unregister:
                logger.warning("pack 状态持有者注销失败", exc_info=True)
            else:
                raise

    _write_slot()
    stop_event = threading.Event()

    def _heartbeat() -> None:
        import time as _t
        while not stop_event.wait(10):
            try:
                _write_slot()
            except OSError:
                logger.warning("pack 状态持有者心跳刷新失败", exc_info=True)

    # 同实例(进程)同路径只起一条心跳线程; 已注册时返回共享 handle
    # (引用计数语义: 每个 handle close 一次, 最后一个才真正停)。
    # 锁只保护线程表/引用计数的读写——handle 构造在锁外(其 __init__
    # 也要拿同一把不可重入锁, 持锁构造会死锁)
    with _HOLDER_LOCK:
        existing = _HOLDER_THREADS.get(thread_key)
        if (existing is not None and existing.is_alive()
                and not getattr(existing, "_holder_stopping", False)):
            reuse = existing
        else:
            reuse = None
            t = threading.Thread(target=_heartbeat, daemon=True,
                                 name=f"pack-state-holder-{pid}")
            t._stop_event = stop_event
            # 四十审 P2: atexit callback 绑定共享线程对象——每个
            # thread_key 只注册一次, 最后一个 close 注销它(此前每个
            # handle 各持一个, 非最后 close 残留自己的 cb, 退出时
            # 重建已删除目录)
            t._shared_atexit_cb = _write_slot
            _atexit_mod.register(_write_slot, unregister=True)
            t.start()
            _HOLDER_THREADS[thread_key] = t
    target_thread = reuse if reuse is not None else t
    return StateHolderHandle(
        thread_key, target_thread._stop_event, target_thread,
        unregister_slot=_write_slot)


# 实例身份单例(三十九审 P1-C)
_INSTANCE_ID = None


def _hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except OSError:
        return ""
