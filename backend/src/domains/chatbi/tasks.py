"""chatbi 后台任务 handler(平台任务框架;pack 经 register_tasks 注册)。

任务类型:
  chatbi.scan_datasource   数据源扫描(信息模式内省→LLM 富化→语义层版本落库),
                           带 scan_status/progress 进度回写(数据源行)
  chatbi.refresh_semantics 语义层自动刷新(按 settings.metadata_refresh_hours 调度;
                           引擎任务框架当前无定时器,由 api 的手动触发 + 宿主
                           调度接入,handler 随时可用)

移植来源: chat-bi backend/app/services/metadata_refresher.py (307 行) +
semantic_scanner 的进度回写机制。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

PACK_NAME = "chatbi"


def register_tasks(manager, app_state=None) -> None:
    """向平台任务框架注册本 pack 的任务 handler。

    app_state 经闭包捕获(模式照抄 knowledge_graph/tasks.py)——
    TaskHandle 只有 payload/进度上报, 平台组件(LLM 客户端)从装配期捕获。

    另起元数据定时刷新守护线程(对标源 APScheduler 每 6h):
    周期 = 设置 metadata_refresh_hours(0 = 关闭, 缺省 6);每轮经任务框架
    submit 走统一通道(去重/任务中心可观测), 不直接调 handler。"""
    def _scan(handle):
        return _task_scan_datasource(handle, app_state)

    def _refresh(handle):
        return _task_refresh_semantics(handle, app_state)

    def _consolidate(handle):
        payload = handle.payload or {}
        return _task_consolidate_memories(
            handle, app_state, data_source_id=payload.get("data_source_id"))

    manager.register("chatbi.scan_datasource", _scan, pack_name=PACK_NAME)
    manager.register("chatbi.refresh_semantics", _refresh, pack_name=PACK_NAME)
    manager.register("chatbi.memory.consolidate", _consolidate, pack_name=PACK_NAME)
    logger.info("chatbi tasks registered: scan_datasource / refresh_semantics / memory.consolidate")
    _start_refresh_scheduler(manager, app_state)


_refresh_thread = None
_refresh_stop = None


def _start_refresh_scheduler(manager, app_state) -> None:
    """元数据定时刷新线程(进程级单例;unload 由 pack 生命周期终止进程,
    线程随进程退出——平台无 pack 级线程取消钩子, daemon 即可)。"""
    global _refresh_thread, _refresh_stop
    if _refresh_thread is not None and _refresh_thread.is_alive():
        return
    import threading

    _refresh_stop = threading.Event()

    def _loop():
        # 双周期任务: 元数据刷新(metadata_refresh_hours, 缺省 6h, 0=关) +
        # 健康巡检(health_check_interval_seconds, 缺省 5min, 对标源
        # APScheduler datasource_health 间隔)。周期每轮重读设置(管理端
        # 热改即时生效);检查粒度取 30s 轮询。
        _loop._last_refresh = _now_ts()   # 启动即视为已刷新(避免重启风暴)
        _loop._last_health = 0.0
        _loop._last_purge = 0.0   # 留存清理按小时级执行(四审 5.4: 每 30s 一次 DELETE 过频)
        _loop._last_gc = 120.0    # 二十审 9.5: 启动 ~2min 后先跑一轮 GC(清历史孤儿)
        while not _refresh_stop.wait(30):
            now = _now_ts()
            from domains.chatbi.runtime import get_pack_db as _get_db
            try:
                _lease_db = _get_db()
            except Exception as e:   # db 不可用 → 本轮跳过定时动作
                logger.warning("调度器取 pack db 失败(本轮跳过): %s", e)
                continue
            # 统计留存清理(每小时一次; 设置 retention 缺省 90 天, 0=关)
            if now - _loop._last_purge >= 3600:
                _loop._last_purge = now
                try:
                    retention = int(_load_settings(app_state)
                                    .get("query_stats_retention_days", 90))
                    if retention > 0:
                        from domains.chatbi.query_stats import purge_stats
                        # 十八审 6.4: 跨进程租约——多 worker 只有一个执行
                        if acquire_lease(_lease_db, "purge_stats", ttl_seconds=900):
                            purged = purge_stats(_lease_db, retention)
                            if purged:
                                logger.info("查询统计留存清理: 删除 %d 条(>%d天)", purged, retention)
                except Exception as e:
                    logger.warning("查询统计清理失败(下轮重试): %s", e)
            # 索引台账 GC(二十审 9.5: 周期回收, 不再只依赖新版本发布事件;
            # 租约互斥; grace/TTL/保留代际来自正式配置)
            if now - _loop._last_gc >= 3600:
                _loop._last_gc = now
                try:
                    if acquire_lease(_lease_db, "index_gc", ttl_seconds=900):
                        # 二十审 8.1: 此前引用未定义的 runtime 且异常被
                        # 裸 except 吞掉——周期 GC 从未真正执行
                        _gc_store = None
                        try:
                            from domains.chatbi import stores as _cb_stores
                            _gc_store = _cb_stores.get_vector(app_state)
                        except Exception as e:
                            logger.warning("周期索引 GC 取向量设施失败"
                                           "(本轮跳过): %s", e)
                        if _gc_store is not None:
                            try:
                                _run_periodic_index_gc(_lease_db, app_state,
                                                       _gc_store)
                            except Exception as e:
                                logger.error("周期索引 GC 执行失败: %s", e)
                except Exception as e:
                    logger.warning("周期索引 GC 失败(下轮重试): %s", e)
            # 健康巡检: 设置周期(缺省 300s)
            try:
                health_iv = int(_load_settings(app_state)
                                .get("health_check_interval_seconds", 300))
            except Exception:
                health_iv = 300
            if health_iv > 0 and now - _loop._last_health >= health_iv:
                _loop._last_health = now
                try:
                    from domains.chatbi import datasources as ds_mod
                    # 十八审 6.4: 租约防止多 worker 重复计数停用
                    if acquire_lease(_lease_db, "health_check",
                                     ttl_seconds=max(120, health_iv // 2)):
                        ds_mod.check_all_health(
                            _lease_db,
                            max_failures=int(_load_settings(app_state)
                                             .get("health_check_max_failures", 3)))
                except Exception as e:
                    logger.warning("健康巡检失败(下轮重试): %s", e)
            # 元数据刷新: 设置周期
            try:
                hours = float(_load_settings(app_state).get("metadata_refresh_hours", 6))
            except Exception:
                hours = 6
            # TODO(soak-diag): 定位 refresh 未触发, 收完撤
            if hours <= 0:
                continue
            if now - _loop._last_refresh < hours * 3600:
                continue
            # 十八审 6.4: 跨进程租约——同时到期的多个 worker 只有一个提交;
            # 未抢到的也推进本地时钟(该周期由持有者负责)
            if not acquire_lease(_lease_db, "refresh_semantics",
                                 ttl_seconds=900):
                _loop._last_refresh = now
                continue
            try:
                manager.submit("chatbi.refresh_semantics", payload={},
                               dedupe_key="chatbi:refresh:all")
                _loop._last_refresh = now   # 九审 7.4: submit成功后才推进
                _loop._refresh_fail_count = 0  # 十审 7.5: 成功清零(连续计数)
                logger.info("元数据定时刷新已提交 (周期 %gh)", hours)
            except Exception as e:
                # 九审 7.4: 失败短退避(下一个30s tick重试), 不丢完整周期
                _loop._refresh_fail_count = getattr(_loop, '_refresh_fail_count', 0) + 1
                if _loop._refresh_fail_count <= 10:  # 最多 ~5分钟 内重试
                    logger.warning("元数据定时刷新提交失败(第%d次, 下tick重试): %s",
                                   _loop._refresh_fail_count, e)
                else:
                    _loop._last_refresh = now  # 放弃本周期
                    logger.error("元数据定时刷新连续失败10次, 跳过本周期: %s", e)

    _refresh_thread = threading.Thread(target=_loop, name="chatbi-metadata-refresh",
                                       daemon=True)
    _refresh_thread.start()
    logger.info("chatbi scheduler started: health (from settings) + metadata refresh (from settings)")


def stop_refresh_scheduler() -> None:
    """停止调度线程(pack unload 时调用; 九审 7.4 生命周期完整化)."""
    global _refresh_thread, _refresh_stop
    if _refresh_stop is not None:
        _refresh_stop.set()
    if _refresh_thread is not None and _refresh_thread.is_alive():
        _refresh_thread.join(timeout=5)
        # 十一审 7.7: join 后确认退出——未退出保留引用, 拒绝新 scheduler
        if _refresh_thread.is_alive():
            logger.error('chatbi scheduler 5s 内未退出——保留引用, 拒绝新 scheduler')
            return  # 不清引用(防空线程+新线程双跑)
    _refresh_thread = None
    _refresh_stop = None


def _now_ts() -> float:
    import time
    return time.monotonic()   # 九审 7.4: 单调时钟, 不受 wall clock 跳变影响


# ── 十八审 6.4: 定时任务跨进程租约(多 worker 唯一性) ──────────────

_SCHEDULER_HOLDER = None


def _scheduler_holder() -> str:
    """本进程的租约持有者标识(主机:进程)。"""
    global _SCHEDULER_HOLDER
    if _SCHEDULER_HOLDER is None:
        import os
        import socket
        import uuid
        _SCHEDULER_HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    return _SCHEDULER_HOLDER


def acquire_lease(db, task_type: str, holder: str | None = None,
                  ttl_seconds: int = 300) -> bool:
    """抢占/续期租约(十九审 6.6: 过期判定用数据库时钟, 不信本地钟)。

    expires_at 存 epoch 毫秒文本, 数值比较在 SQL 侧完成:
      - 无记录 → 插入, 本持有者获得;
      - 持有者是本人 → 无条件续期(heartbeat 同原语);
      - 持有者是他人且未过期 → 不动, 返回 False;
      - 已过期(DB 时钟判定) → 抢占。
    任何异常返回 False(租约不可用时宁可跳过, 不重复执行)。
    """
    holder = holder or _scheduler_holder()
    try:
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chatbi_scheduler_leases ("
                "task_type TEXT PRIMARY KEY, holder TEXT NOT NULL, "
                "expires_at TEXT NOT NULL)")
            # 二十审 9.6 + 二十一审 5.6: 旧部署 ISO 租约的迁移全程用
            # 数据库时钟(此前 datetime.now().astimezone() 又依赖了工作
            # 进程本地钟)。三步:
            #   1) 可解析且未过期 → 原地转 epoch 保留持有权(旧实例继续
            #      有效, 新实例只能等它过期, 不双执行);
            #   2) 可解析且已过期 → 删除;
            #   3) 不可解析 → 保留并告警(fail-closed, 不猜测)。
            conn.execute(
                "UPDATE chatbi_scheduler_leases SET expires_at = "
                "(extract(epoch FROM expires_at::timestamptz)*1000)::bigint::text "
                "WHERE expires_at !~ '^[0-9]+$' "
                "  AND expires_at ~ '^\\d{4}-' "
                "  AND expires_at::timestamptz > now()")
            conn.execute(
                "DELETE FROM chatbi_scheduler_leases "
                "WHERE expires_at !~ '^[0-9]+$' "
                "  AND expires_at ~ '^\\d{4}-' "
                "  AND expires_at::timestamptz <= now()")
            _unparsable = conn.execute(
                "SELECT task_type, expires_at FROM chatbi_scheduler_leases "
                "WHERE expires_at !~ '^[0-9]+$'").fetchall()
            for row in _unparsable or []:
                logger.error("租约 %s 的过期时间不可解析(%r), fail-closed "
                             "保留——请人工核查", row["task_type"],
                             row["expires_at"])
            # now 由数据库给出并直接参与比较(跨主机时钟偏差免疫)
            conn.execute(
                "WITH n AS (SELECT (extract(epoch FROM now())*1000)::bigint AS nowms) "
                "INSERT INTO chatbi_scheduler_leases (task_type, holder, expires_at) "
                "SELECT ?, ?, ((SELECT nowms FROM n) + ?)::text FROM n "
                "ON CONFLICT (task_type) DO UPDATE SET "
                "holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at "
                "WHERE chatbi_scheduler_leases.holder = EXCLUDED.holder "
                "   OR (chatbi_scheduler_leases.expires_at ~ '^[0-9]+$' "
                "       AND chatbi_scheduler_leases.expires_at::bigint "
                "           <= (SELECT nowms FROM n))",
                (task_type, holder, int(ttl_seconds) * 1000))
            row = conn.execute(
                "SELECT holder FROM chatbi_scheduler_leases WHERE task_type = ?",
                (task_type,)).fetchone()
        return bool(row and row["holder"] == holder)
    except Exception as e:
        logger.warning("租约 %s 获取失败(跳过本轮防重复): %s", task_type, e)
        return False


def release_lease(db, task_type: str, holder: str) -> bool:
    """释放本人持有的租约(任务终态 finally 调用)。仅删自己的行。"""
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM chatbi_scheduler_leases "
                "WHERE task_type = ? AND holder = ?", (task_type, holder))
            return bool(getattr(cur, "rowcount", 0))
    except Exception as e:
        logger.warning("租约 %s 释放失败(等 TTL 过期): %s", task_type, e)
        return False


class RunLease:
    """执行期分布式租约(十九审 6.4 / 二十审 9.2): claim → 独立心跳 → release。

    二十审整改:
      - 心跳改为**独立后台线程**(TTL/3 间隔续租), 不依赖业务步骤回调——
        单步执行超过 TTL 时租约也能保活; 续约失败(被接管/DB 故障)时置
        self.owned=False, 写前 fencing 检查据此拒绝继续写入;
      - release 终止心跳线程并删租约行(终态 finally)。
    """

    def __init__(self, db, task_type: str, holder: str,
                 ttl_seconds: int = 900):
        self.db = db
        self.task_type = task_type
        self.holder = holder
        self.ttl_seconds = ttl_seconds
        self.acquired = False
        self._hb_stop: threading.Event | None = None
        self._hb_thread: threading.Thread | None = None

    def acquire(self) -> bool:
        self.acquired = acquire_lease(self.db, self.task_type, self.holder,
                                      ttl_seconds=self.ttl_seconds)
        if self.acquired:
            self._start_heartbeat()
        return self.acquired

    def _start_heartbeat(self):
        self._hb_stop = threading.Event()
        interval = max(1.0, self.ttl_seconds / 3.0)

        def _beat():
            while not self._hb_stop.wait(interval):
                try:
                    ok = acquire_lease(self.db, self.task_type, self.holder,
                                       ttl_seconds=self.ttl_seconds)
                except Exception as e:
                    logger.error("执行期租约续租异常(视为失租): %s", e)
                    ok = False
                if not ok:
                    self.acquired = False
                    logger.error("执行期租约 %s 丢失(被其他实例接管)——"
                                 "写前 fencing 将拒绝后续写入: %s",
                                 self.task_type, self.holder)
                    return

        self._hb_thread = threading.Thread(target=_beat, daemon=True,
                                           name=f"lease-hb:{self.task_type}")
        self._hb_thread.start()

    @property
    def owned(self) -> bool:
        """快速判定: 本地心跳标记(二十审 10: 进程暂停窗口内可能过期,
        关键写入的强检查用 assert_owned())."""
        return self.acquired

    def assert_owned(self) -> bool:
        """数据库层 fencing(二十审 10): 当前 holder 仍是本持有者且未过期。

        本地 bool 在进程暂停/心跳延迟窗口内可能失真——关键写入(语义
        落库/索引发布/报告保存)前用 DB 实查。查询失败按"不持有"处理
        (fail-closed: 宁可不写也不能在失租窗口双写)。
        """
        if not self.acquired:
            return False
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT holder FROM chatbi_scheduler_leases "
                    "WHERE task_type = ? AND holder = ? "
                    "AND expires_at ~ '^[0-9]+$' "
                    "AND expires_at::bigint > "
                    "(extract(epoch FROM now())*1000)::bigint",
                    (self.task_type, self.holder)).fetchone()
            ok = row is not None
            if not ok:
                logger.error("fencing: 租约 %s 已易主/过期(本地标记=%s)——"
                             "拒绝写入", self.task_type, self.holder)
                self.acquired = False
            return ok
        except Exception as e:
            logger.error("fencing 检查失败(按失租处理): %s", e)
            self.acquired = False
            return False

    def execute_if_owned(self, write_fn):
        """事务级 fencing(二十三审: 行锁版): SELECT ... FOR UPDATE。

        二十二审的普通 SELECT 在 READ COMMITTED 下不锁租约行——
        审计已复现: 检查通过后 write_fn 内暂停, 新 writer 从另一连接
        UPDATE holder 提交, 旧 writer 恢复后 stale 写入仍落库。
        FOR UPDATE 行锁使接管方的 UPDATE 阻塞到本事务提交/回滚为止,
        窗口关闭。注意: write_fn 必须短小(持锁期间接管方在等)。
        返回 (ok, write_result)。
        """
        if not self.acquired:
            return False, None
        # write_fn 的异常(如 VersionConflictError)**原样上抛**——
        # 吞掉会破坏调用方的冲突重试; with 块异常退出自动回滚,
        # 半途写入不会残留
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT holder FROM chatbi_scheduler_leases "
                "WHERE task_type = ? AND holder = ? "
                "AND expires_at ~ '^[0-9]+$' "
                "AND expires_at::bigint > "
                "(extract(epoch FROM now())*1000)::bigint "
                "FOR UPDATE",
                (self.task_type, self.holder)).fetchone()
            if row is None:
                logger.error("事务级 fencing: 租约 %s 已易主——写入被拒",
                             self.task_type)
                self.acquired = False
                return False, None
            return True, write_fn(conn)

    def heartbeat(self) -> bool:
        """同步续租点(兼容既有调用); 返回 False = 租约已丢。"""
        if not self.acquired:
            return False
        if acquire_lease(self.db, self.task_type, self.holder,
                         ttl_seconds=self.ttl_seconds):
            return True
        logger.error("执行期租约 %s 丢失(被其他实例接管)——任务应中止: %s",
                     self.task_type, self.holder)
        self.acquired = False
        return False

    def release(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5)
            self._hb_thread = None
        if self.acquired:
            release_lease(self.db, self.task_type, self.holder)
            self.acquired = False


def _run_periodic_index_gc(db, app_state, store) -> int:
    """对所有 scope 执行索引台账 GC(二十审 9.5: 周期触发 + 配置化参数)。

    store: ChatBIVectorStore 实例(调度器闭包的 app_state 可取时传入;
    None = 向量设施不可用, 本轮跳过——台账清理无向量可删但仍应推进?)。
    向量不可用时跳过整轮: 只删台账不删向量会造成"GC 后向量复活"的
    不一致, 宁可等下一轮。
    """
    if store is None:
        return 0
    from domains.chatbi import stores as _stores
    settings = _load_settings(app_state)
    keep = int(settings.get("index_gc_keep_generations", 2))
    grace = int(settings.get("index_gc_published_grace_seconds", 600))
    ttl = int(settings.get("index_gc_unfinished_ttl_seconds", 86400))
    deleted_total = 0
    with db.connect() as conn:
        scopes = [r["scope_id"] for r in conn.execute(
            "SELECT scope_id FROM chatbi_data_sources "
            "WHERE is_active = 1 AND scope_id != ''").fetchall()]
        rows = conn.execute(
            "SELECT DISTINCT scope FROM chatbi_index_revisions").fetchall()
    all_scopes = sorted(set(scopes) | {r["scope"] for r in rows})
    for sc in all_scopes:
        try:
            deleted_total += _stores.gc_index_builds(
                db, sc, store, keep_generations=keep,
                published_grace_seconds=grace,
                unfinished_ttl_seconds=ttl)
        except Exception as e:
            logger.warning("周期 GC scope=%s 失败: %s", sc[:8], e)
    if deleted_total:
        logger.info("周期索引 GC: 回收 %d 条向量", deleted_total)
    return deleted_total


class IndexRebuildDegraded(RuntimeError):
    """扫描/语义已成功落库, 但索引重建失败(二十三审 5.2)。

    与结构性失败的区别: 数据源终态是 done_with_warning 而非 failed——
    语义版本可用, 检索暂用旧索引, 下轮 refresh 的 pointer-lag 自愈
    可补建。任务终态仍为 failed(错误可见), 但外层 except 不得把
    数据源行已写好的 warning 状态覆盖成 failed。
    """


def _task_scan_datasource(handle, app_state=None) -> dict:
    """扫描任务入口。payload: {datasource_id, rescan: bool}。"""
    from domains.chatbi import runtime, semantic, datasources
    payload = handle.payload or {}
    ds_id = payload.get("datasource_id")
    if not ds_id:
        raise ValueError("payload 缺 datasource_id")

    db = runtime.get_pack_db()
    llm = runtime.get_llm(app_state) if app_state else None
    info = datasources.get_datasource(db, ds_id, decrypt=True)
    if not info:
        raise ValueError(f"数据源不存在: {ds_id}")

    settings = _load_settings(app_state)

    # 二十审 9.2: scan/refresh 共用同一数据源写租约键——同一数据源
    # 任意时刻只允许一个语义写任务(跨操作互斥, 不只同类互斥)
    lease = RunLease(db, f"run:semantic_write:{ds_id}",
                     holder=f"task:{handle.task_id}", ttl_seconds=900)
    if not lease.acquire():
        from services.task_manager import PermanentTaskError
        raise PermanentTaskError(
            f"数据源 {info.name} 已有扫描/刷新任务在另一实例执行中"
            f"(执行期租约冲突)——请等待其完成后再试")

    def progress(pct: int, stage: str):
        # 双路进度: 任务中心(handle.set_progress) + 数据源行(ChatBI 语义)
        try:
            handle.set_progress(pct, stage)
        except Exception:
            pass
        datasources.update_datasource(db, ds_id, scan_progress=pct, scan_stage=stage)
        # 扫描阶段推进即续租; progress 异常会被 scan_datasource 吞掉,
        # 所以租约丢失的硬检查点在扫描完成后的写阶段前(见 lease.acquired)
        lease.heartbeat()

    handle.log(f"开始扫描数据源: {info.name} ({info.db_type} {info.host}:{info.port}/{info.database})")
    datasources.update_datasource(db, ds_id, scan_status="scanning",
                                  scan_progress=0, scan_stage="开始扫描", scan_error="")
    try:
        # 十三审 7.1 P0: scan 加 CAS——persist=False + 外层 save 传 expected_version
        # (此前 persist=True 内部 save 不带 expected, 后台扫描可静默覆盖管理员编辑)
        from domains.chatbi.graph_infer import VersionConflictError
        _pre = semantic.load_content(db, ds_id)
        _pre_version = _pre[1] if _pre and _pre[0] is not None else 0  # 首次=0
        content = semantic.scan_datasource(
            llm=llm, db=db, connect_info=_connect_info(info),
            infer_metrics=bool(settings.get("scan_metric_inference", True)),
            progress_cb=progress, datasource_id=ds_id,
            persist=False)  # 不在内部保存——由外部 CAS 保存
        # 写前 fencing(二十审 9.2/10): DB 实查——本地 bool 在进程暂停
        # 窗口内可能失真, 落库前必须以数据库 holder 为准
        if not lease.assert_owned():
            from services.task_manager import PermanentTaskError
            raise PermanentTaskError(
                "扫描执行期租约被其他实例接管(本任务停滞超时)——中止落库")
        # 十四审 7.2: rescan 用来源感知 merge(不是 refresh 粗 merge)——
        # refresh 的 _merge_content 旧值全覆盖会吞掉重扫的新 LLM 名/指标/
        # 问题(重扫的核心目的就是重新富化); rescan 按 source 分治:
        #   manual/人工标注 → 保留旧值; auto(LLM/规则推断) → 允许新值替换
        # 十七审 7.6: merge 报告(停用/冲突)进任务结果 + 持久化供页面展示
        merge_report = _new_report()
        if _pre and _pre[0] is not None:
            content = _merge_rescan(_pre[0], content, report=merge_report)
        try:
            # 二十三审 8: 语义落库与租约行锁同事务(fenced)——检查后失租的
            # 写入被数据库拒绝; VersionConflictError 等领域异常原样上抛
            # 走下方冲突重试
            _ok, version = lease.execute_if_owned(
                lambda conn: semantic.save_content(
                    db, ds_id, content, source="scan",
                    expected_version=_pre_version, conn=conn))
            if not _ok:
                from services.task_manager import PermanentTaskError
                raise PermanentTaskError(
                    "语义落库被事务级 fencing 拒绝(租约已失)——中止")
        except VersionConflictError:
            handle.log("扫描版本冲突(扫描期间有并发语义写入)——"
                       "基于最新版重试一次")
            _retry = semantic.load_content(db, ds_id)
            _re_scan = semantic.scan_datasource(
                llm=llm, db=db, connect_info=_connect_info(info),
                infer_metrics=bool(settings.get("scan_metric_inference", True)),
                progress_cb=progress, datasource_id=ds_id, persist=False)
            if _retry and _retry[0] is not None:
                merge_report = _new_report()  # 以最终生效的 merge 报告为准
                content = _merge_rescan(_retry[0], _re_scan, report=merge_report)
                version = semantic.save_content(db, ds_id, content,
                                                source="scan",
                                                expected_version=_retry[1])
            else:
                raise
        # 十八审 6.5/十九审 6.6: 每次 merge 后都写报告(含空报告)——干净刷新
        # 清掉旧告警, 报告版本与语义版本对齐; 保存失败时任务结果可见
        report_state = save_merge_report(db, ds_id, version, merge_report)
        if merge_report.get("requires_review"):
            _log_report(handle, merge_report, f"重扫 v{version}")
        if report_state == REPORT_FAILED:
            handle.log(f"⚠ merge 报告保存失败(语义 v{version} 已生效)——"
                       f"待复核清单可能与当前版本不一致")
        # 向量索引重建 (对标 _run_scan_background 尾部 rebuild_index;
        # 失败降级不阻塞——RAG 检索可用全表降级路径)
        if not lease.assert_owned():
            from services.task_manager import PermanentTaskError
            raise PermanentTaskError("索引重建前 fencing 失败(租约已失)——中止")
        indexed = 0
        index_status = "ok"
        index_warning = None
        try:
            from domains.chatbi import indexing, stores
            store = stores.get_vector(app_state)
            embedder = stores.get_embedder(llm)
            rb = indexing.guarded_rebuild(content, ds_id, store, embedder, db=db,
                                           expected_version=version)  # 十三审7.3
            if rb is not None and getattr(rb, "error", None):
                raise RuntimeError(rb.error)
            indexed = rb.indexed_count
        except Exception as e:
            logger.warning("向量索引重建失败(降级, 不阻塞扫描): %s", e)
            index_status = "degraded"
            index_warning = f"扫描完成但索引重建失败——RAG 检索将降级, 重扫可修复: {str(e)[:120]}"
            handle.log(index_warning)
        scan_stage = f"完成: {len(content.models)} 张表, 索引 {indexed} 条"
        if index_warning:
            scan_stage += f" ⚠ {index_warning}"   # 持久化到数据源行, scan-status 轮询可返回(八审 6.5)
        datasources.update_datasource(
            db, ds_id, scan_status="done", scan_progress=100,
            scan_stage=scan_stage,
            scanned_at=_now_iso())
        handle.log(f"扫描完成: {len(content.models)} 张表, 向量索引 {indexed} 条"
                   + (" (索引降级)" if index_status != "ok" else ""))
        # 二十审 7.4 根本修复: 重扫后索引失败 = 新语义落库但检索仍在
        # 用旧版本索引(漂移)——与 refresh 同口径 ok=False, 任务中心如实
        # 可见, 修复后下一轮 refresh 的 pointer-lag 自愈可补建
        # 二十二审 7 根本修复: 索引失败 = 任务终态必须 failed。
        # 此前正常 return {ok:false}——TaskManager 对正常返回一律标
        # succeeded, 任务中心仍显示"已成功 100%", 失败只藏在 result
        # JSON 里(审计: 不要把错误埋在 result)。语义已落库, 在异常
        # 信息里说明状态与后果, 数据源行写 done_with_warning(区分
        # 纯结构扫描失败与索引降级)。
        _idx_ok = index_status in ("ok", "skipped")
        if not _idx_ok:
            _detail = (f"扫描完成: {len(content.models)} 张表已入库, 但索引"
                       f"重建失败——RAG 检索仍在用旧版本索引"
                       f"({index_warning or '见任务日志'})。"
                       f"手动重扫或等待下轮 refresh 自动补建可修复")
            datasources.update_datasource(
                db, ds_id, scan_status="done_with_warning",
                scan_progress=100, scan_stage=_detail[:480],
                scan_error=index_warning or "索引重建失败")
            raise IndexRebuildDegraded(_detail)
        return {"models": len(content.models), "indexed": indexed,
                "ok": True, "index_rebuild": index_status,
                # 十七审 7.6: 停用/冲突清单进任务结果(任务中心可见)
                **({"dropped_items": merge_report["dropped_items"],
                    "conflicts": merge_report["conflicts"],
                    "requires_review": True}
                   if merge_report.get("requires_review") else {}),
                **({"report_warning": True}
                   if report_state == REPORT_FAILED else {}),
                "datasource_id": ds_id}
    except IndexRebuildDegraded:
        # 数据源行已是 done_with_warning(语义可用+修复指引)——
        # 外层不得覆盖成 failed(二十三审 5.2: 此前 RuntimeError 走
        # 通用分支, warning 态永远到不了生产终态)
        raise
    except Exception as e:
        datasources.update_datasource(db, ds_id, scan_status="failed",
                                      scan_error=str(e)[:500])
        raise
    finally:
        lease.release()   # 十九审 6.4: 终态释放(成功/失败都执行)


def _task_consolidate_memories(handle, app_state=None, data_source_id=None) -> dict:
    """记忆整理任务入口(对标源 POST /memory/consolidate)。

    LLM 合并去重碎片记忆;原记忆标记 consolidated 隐藏(可追溯),
    linkage 结构化类型跳过。进度经任务中心 SSE 透出;关键节点写任务日志
    (任务中心"日志"页签不再空白)。

    闭环接线(复核报告 P0): 整理完成后按涉及的每个数据源无条件调用
    sync_linkage_to_graph——linkage 共现反哺图谱不再只藏在元数据刷新里;
    同步失败记入任务日志与结果(degraded 可见), 不静默。

    data_source_id: 只整理该数据源的记忆(None = 全部, 仍按 scope 分组)。
    """
    from domains.chatbi import memory, runtime
    llm = runtime.get_llm(app_state) if app_state else None
    if llm is None:
        raise ValueError("LLM 客户端不可用——记忆整理需要 LLM(检查插件依赖配置)")
    db = runtime.get_pack_db()

    def progress(pct: int, stage: str):
        try:
            handle.set_progress(pct, stage)
        except Exception:
            pass

    scope_text = f" (仅数据源 {data_source_id})" if data_source_id else ""
    handle.log(f"记忆整理开始{scope_text}: 按 用户+数据源 分组, 过滤已整理/结构化(linkage)条目")
    result = memory.consolidate_memories(
        llm, db, on_progress=progress, data_source_id=data_source_id)
    # 整理结果写任务日志(用户在任务中心能直接看到"整理了几条/合并成几条")
    detail = result.get("detail") or ""
    handle.log(
        f"整理完成: 合并为 {result.get('consolidated', 0)} 条精炼记忆, "
        f"参与 {result.get('total', 0)} 条原始记忆。{detail}")
    if result.get("consolidated"):
        handle.log("原始记忆已标记为'已整理'(默认隐藏, 记忆页勾选'显示已整理'可查看)")

    # ── linkage → 图谱反哺: 目标 = 显式 ds ∪ 候选 scope ∪ linkage distinct ds ──
    # (三审 P0: 不能只取"成功产出合并记忆"的组——无产出/失败/仅 linkage
    #  时再次提交整理任务必须仍能触发同步, 重试语义才真实成立)
    graph_sync = []
    from domains.chatbi.memory import get_memory_store
    involved_ds = _graph_sync_targets(get_memory_store(db), result,
                                      data_source_id=data_source_id)
    for ds_id in sorted(involved_ds):
        try:
            from domains.chatbi.graph_infer import sync_linkage_to_graph
            res = sync_linkage_to_graph(
                db, get_memory_store(db), ds_id,
                rebuild_index=_make_index_rebuilder(app_state, ds_id),
                **_linkage_sync_kwargs(app_state))
            idx_state = (res or {}).get("index_rebuild", "ok")
            if str(idx_state).startswith("degraded"):
                handle.log(f"linkage 图谱同步完成 (ds={ds_id[:8]}…): "
                           f"版本已写, 但索引重建{idx_state}——RAG 检索可能滞后, 手动重扫可修复")
            else:
                handle.log(f"linkage 图谱同步完成 (ds={ds_id[:8]}…): {res}")
            graph_sync.append({"data_source_id": ds_id, "ok": True,
                               "index_rebuild": idx_state, "result": res})
        except Exception as e:
            logger.warning("整理后 linkage 图谱同步失败 (ds=%s): %s", ds_id, e)
            handle.log(f"linkage 图谱同步失败 (ds={ds_id[:8]}…, 降级): {e}")
            graph_sync.append({"data_source_id": ds_id, "ok": False, "error": str(e)[:200]})
    if not involved_ds:
        handle.log("无图谱同步目标(无候选记忆且无 linkage 记录)")
    result["graph_sync"] = graph_sync
    return result


def _task_refresh_semantics(handle, app_state=None) -> dict:
    """自动刷新入口(对标源 metadata_refresher.refresh_all_metadata):

    与全量扫描(scan_datasource)的语义差异:
      - 只做结构内省(llm=None), 不跑 LLM 富化——刷新是高频低价值操作,
        LLM 非确定性输出会产伪版本 + 全量调用成本;
      - 保留人工/LLM 标注(_merge_content: 新结构权威, 旧标注优先);
      - 跳过从未扫描过的数据源(首次扫描必须手动——需要 LLM 富化);
      - 内容指纹未变 → save_content 幂等不落新版本;
      - 落了新版本 → 重建向量索引(版本与索引不漂移)。
    """
    from domains.chatbi import runtime, semantic, datasources, indexing
    db = runtime.get_pack_db()
    settings = _load_settings(app_state)
    results = []
    # 十九审 6.4: 执行期分布式租约——全量刷新绑 task id, 每个数据源
    # 迭代续租; 超时被接管即中止(避免与接管者重复执行)。手工触发与
    # 调度触发共用本 claim(手工入口经 TaskManager 落到同一 handler)。
    lease = RunLease(db, "run:refresh_semantics",
                     holder=f"task:{handle.task_id}", ttl_seconds=900)
    if not lease.acquire():
        from services.task_manager import PermanentTaskError
        raise PermanentTaskError(
            "元数据刷新已在另一实例执行(执行期租约冲突)——本任务退出")
    try:
        return _refresh_all_datasources(handle, app_state, db, settings,
                                        results, lease)
    finally:
        lease.release()   # 终态释放(成功/失败都执行)


def _refresh_all_datasources(handle, app_state, db, settings, results,
                             lease) -> dict:
    """全量刷新主体(全局调度租约已持有; 每数据源迭代头 heartbeat)。

    二十审 9.2: 每个数据源的写操作额外持有 `run:semantic_write:{ds}`
    写租约(与 scan 共键)——同一数据源的 refresh 与 scan 跨实例互斥。
    写租约被占(该 ds 正被 scan/其他实例处理)→ 本轮跳过该数据源,
    不视为整体失败。
    """
    from domains.chatbi import runtime, semantic, datasources, indexing
    for row in datasources.list_datasources(db, active_only=True):
        if not lease.heartbeat():
            from services.task_manager import PermanentTaskError
            raise PermanentTaskError(
                "刷新执行期租约被其他实例接管(本任务停滞超时)——中止剩余数据源")
        # list_datasources 不带 decrypt(password_plain 为空)——同健康巡检
        # 的既有教训: 不解密直连必失败(fe_sendauth: no password supplied)。
        # 此前定时刷新自迁移以来一直在任务层静默失败(脚本验证绕过了本层)。
        info = datasources.get_datasource(db, row.id, decrypt=True) or row
        write_lease = RunLease(db, f"run:semantic_write:{info.id}",
                               holder=f"task:{handle.task_id}", ttl_seconds=900)
        if not write_lease.acquire():
            results.append({"datasource_id": info.id, "ok": True,
                            "skipped": "write_lease_busy",
                            "detail": "该数据源正被其他写任务处理, 本轮跳过"})
            continue
        try:
            if not write_lease.assert_owned() or not lease.assert_owned():
                results.append({"datasource_id": info.id, "ok": False,
                                "error": "刷新写前 fencing 失败(租约已失)"})
                continue
            # 十一审 7.2 P0: content 和 revision 必须同一次读取——
            # 此前 current 用 load_current_content(只有content), 版本号
            # 在扫描后另查, 管理员扫描期间写入会产生"旧content+新版本号"
            _loaded = semantic.load_content(db, info.id)
            current, current_version = (_loaded if _loaded[0] is not None
                                        else (None, None))
            if current is None:
                # 从未扫描过: 首次扫描必须手动(需要 LLM 富化), 跳过
                results.append({"datasource_id": info.id, "ok": True,
                                "skipped": "never_scanned"})
                continue
            # 纯结构扫描(无 LLM, 不落库) → 合并旧标注/指标/关系 → 一次落版本。
            # persist=False: scan_datasource 内部 save 会先落一个"裸结构"
            # 中间版本——指纹基准被污染(每周期净增 2 版本), 且裸结构短暂
            # 成为 is_current(用户此刻查询丢失全部中文标注)。
            new_content = semantic.scan_datasource(
                llm=None, db=db, connect_info=_connect_info(info),
                infer_metrics=bool(settings.get("scan_metric_inference", True)),
                datasource_id=info.id, persist=False)
            merge_report = _new_report()  # 十七审 7.6: 停用/冲突清单
            merged = _merge_content(current, new_content, report=merge_report)
            # 十一审 7.2: 直接用同快照的 current_version, 不再另查
            prev_db_version = current_version
            # 十审 7.1: 传 expected_version——此前 refresh 不带版本前置,
            # 旧快照可静默覆盖管理员的新编辑(报告复现: Admin-A v2 被
            # Refresh-from-v1 v3 覆盖)。冲突时基于最新版重试一次。
            from domains.chatbi.graph_infer import VersionConflictError as _VCE
            # 廉价早退(真正的保证在下方 fenced 落库: 租约行锁+写入同事务)
            if not write_lease.assert_owned():
                results.append({"datasource_id": info.id, "ok": False,
                                "error": "写前 fencing 失败(数据源写租约已失)"
                                         "——本数据源本轮未写入"})
                continue
            try:
                # 二十三审 8: fenced 落库——租约行锁与语义写入同事务,
                # 失租写入被数据库拒绝; VCE 原样上抛走重试
                _ok, version = write_lease.execute_if_owned(
                    lambda conn: semantic.save_content(
                        db, info.id, merged, source="refresh",
                        expected_version=prev_db_version, conn=conn))
                if not _ok:
                    results.append({"datasource_id": info.id, "ok": False,
                                    "error": "写前 fencing 失败(数据源写租约已失)"
                                             "——本数据源本轮未写入"})
                    continue
            except _VCE:
                # 冲突: 有并发人工/图谱写入——重新加载 current, 用未污染
                # 的 new_content(扫描结果)重新 merge(十一审 7.2: 不能复用
                # 旧 merged——merge 会原地修改 new_content, 旧的关系/指标
                # 已被污染, 复用可能复活管理员刚删除的内容)
                logger.info("刷新版本冲突 ds=%s, 基于最新版重试", info.id)
                _retry = semantic.load_content(db, info.id)
                if _retry and _retry[0] is not None:
                    _re_scanned = semantic.scan_datasource(
                        llm=None, db=db, connect_info=_connect_info(info),
                        infer_metrics=bool(settings.get("scan_metric_inference", True)),
                        datasource_id=info.id, persist=False)
                    merge_report = _new_report()  # 以最终生效的 merge 报告为准
                    _retry_merged = _merge_content(_retry[0], _re_scanned,
                                                   report=merge_report)
                    version = semantic.save_content(
                        db, info.id, _retry_merged, source="refresh",
                        expected_version=_retry[1])
                else:
                    raise  # 无法重试(无 current)
            # 十八审 6.5/十九审 6.6/二十审 9.4/二十二审 8: 每次 merge 后
            # 都写报告(含空); 写入与数据源写租约同事务 fencing(失租拒写);
            # 仅 failed 告警——superseded 是正常乱序让路
            report_state = save_merge_report_fenced(
                write_lease, db, info.id, version, merge_report)
            if merge_report.get("requires_review"):
                _log_report(handle, merge_report, f"刷新 v{version}")
            if report_state == REPORT_FAILED:
                handle.log(f"⚠ merge 报告保存失败 ds={info.id[:8]}… "
                           f"(语义 v{version} 已生效)——待复核清单可能过期")
            struct_changed = (prev_db_version is None
                              or version != prev_db_version)
            evolve = _evolve_graph(db, info.id, merged, app_state=app_state)
            # 十审 7.3: error/conflict 检查必须在 unchanged 快路之前——
            # 此前 unchanged 放在前面, evolve 返回 error+versions_written=0
            # 时错误被吞掉, 刷新仍报 ok=true(八轮修好的传播再次失效)
            if evolve.get("index_rebuild") == "error":
                results.append({"datasource_id": info.id, "ok": False,
                                "error": f"图谱演进失败: {evolve.get('error', '')}"})
                continue
            if evolve.get("index_rebuild") == "conflict":
                results.append({"datasource_id": info.id, "ok": False,
                                "conflict": True,
                                "error": "图谱演进乐观锁冲突(有并发语义写入), "
                                         "下轮刷新自动重试"})
                continue
            # 九审 7.6: 结构与图谱都无变化 → 跳过全量重建(240周期不再重建)
            # 二十审 P0-2 自愈: 跳过前必须检查 active 指针是否落后——
            # 此前 v4/r3 漂移后, 后续 unchanged 刷新全部走快路, 漂移
            # 永不自愈。落后 → 不跳过, 走下方对齐当前版本的补建。
            if not struct_changed and evolve.get("versions_written", 0) == 0:
                _cur_rev = None
                try:
                    # 二十二审 6: 此前从 DataSourceInfo.scope_id 读——生产
                    # dataclass 根本没有该字段, 恒得 None → 每轮误判漂移
                    # 全量重建。scope 的正规契约是 stores.get_scope。
                    from domains.chatbi.stores import (get_scope,
                                                       get_active_doc_id)
                    import re as _re_l
                    _sc = get_scope(db, info.id)
                    if _sc:
                        _doc = get_active_doc_id(db, _sc)
                        _m = (_re_l.match(r"schema_r(\d+)", _doc or "")
                              if _doc else None)
                        _cur_rev = int(_m.group(1)) if _m else None
                except Exception as _e:
                    logger.warning("pointer lag 检查失败(按需补建处理): %s", _e)
                if _cur_rev is not None and _cur_rev >= version:
                    results.append({"datasource_id": info.id, "version": version,
                                    "ok": True, "changed": False,
                                    "detail": "结构无变化且无新图谱证据, 跳过索引重建"})
                    continue
                logger.warning("检测到索引漂移 semantic v%s vs active r%s——"
                               "本轮补建(ds=%s)", version, _cur_rev, info.id[:8])
                handle.log(f"⚠ 检测到索引漂移(语义 v{version}, 索引 r{_cur_rev})"
                           f"——自动补建当前版本索引")
            # 六审 P1.C 修复: 演化可能写出 v+2/v+3——最终索引必须对齐
            # 演化后的 current, 不再用演化前的 merged 覆盖(旧关系内容)
            # 6.7: 同一次读取获得 content+version(防并发窗口 content 与
            # version 来自不同版本); load_content 返回 (content, version) 二元组
            from domains.chatbi import semantic as _sem
            _loaded = _sem.load_content(db, info.id)
            if _loaded and _loaded[0] is not None:
                final_content, final_version = _loaded
            else:
                final_content, final_version = merged, version
            index_status = "ok"
            index_warning = None
            try:
                from domains.chatbi import stores as cb_stores
                llm = runtime.get_llm(app_state) if app_state else None
                if llm is not None:
                    _rb = indexing.guarded_rebuild(
                        content=final_content, data_source_id=info.id,
                        store=cb_stores.get_vector(app_state),
                        embedder=cb_stores.get_embedder(llm), db=db,
                        expected_version=final_version)  # 十二审8.2
                    if _rb is not None and getattr(_rb, "error", None):
                        raise RuntimeError(f"索引重建失败: {_rb.error}")
                else:
                    index_status = "skipped"
            except Exception as e:
                # 二十审 7.4: 索引落后不能伪装纯成功——任务日志可见 +
                # 数据源结果 ok=False(任务中心不再是"已成功 100%")
                logger.warning("刷新后索引重建失败: %s", e)
                handle.log(f"⚠ 索引重建失败 ds={info.id[:8]}…: {str(e)[:160]}")
                index_status = "degraded"
                index_warning = f"语义已写 v{final_version}, 索引落后——手动重扫可修复"
            # 6.4 修复: 索引期间若 current 被并发变更(人工编辑/回滚), 刚建的
            # 索引已过期——重载最新版本补建一次, 仍失败则明确 conflict
            try:
                _recheck = _sem.load_content(db, info.id)
                if _recheck and _recheck[0] is not None and _recheck[1] != final_version:
                    logger.warning("索引构建期间 current 并发变更 v%s→v%s, 重载重建",
                                   final_version, _recheck[1])
                    final_content, final_version = _recheck
                    _rb = indexing.guarded_rebuild(
                        content=final_content, data_source_id=info.id,
                        store=cb_stores.get_vector(app_state),
                        embedder=cb_stores.get_embedder(llm), db=db,
                        expected_version=final_version)  # 十二审8.2
                    if _rb is not None and getattr(_rb, "error", None):
                        index_status = "conflict"
                        index_warning = (f"并发语义变更(v{final_version}), "
                                         f"索引对齐失败——重扫可修复")
            except Exception as e:
                index_status = "conflict"
                index_warning = f"并发校验失败: {str(e)[:120]}"
            if index_warning:
                logger.warning("刷新索引降级 ds=%s: %s", info.id, index_warning)
            # 二十审 7.4 根本修复: 索引 degraded = 本轮"语义已发布但
            # 检索仍在用旧索引"——这不是成功, ok=False 让任务中心如实
            # 显示失败(此前只加日志, 任务仍是"已成功 100%"= 修表面)
            _idx_ok = index_status in ("ok", "skipped")
            results.append({"datasource_id": info.id, "version": final_version,
                            "models": len(final_content.models),
                            "ok": _idx_ok,
                            **({} if _idx_ok else {
                                "error": index_warning or "索引重建失败"}),
                            "index_rebuild": index_status,
                            **({"index_warning": index_warning} if index_warning else {}),
                            # 十七审 7.6: 停用/冲突清单进任务结果
                            **({"dropped_items": merge_report["dropped_items"],
                                "conflicts": merge_report["conflicts"],
                                "requires_review": True}
                               if merge_report.get("requires_review") else {}),
                            **({"report_warning": True}
                               if report_state == REPORT_FAILED else {})})
        except Exception as e:
            logger.warning("自动刷新失败 %s: %s", info.name, e)
            results.append({"datasource_id": info.id, "ok": False, "error": str(e)[:200]})
        finally:
            write_lease.release()   # 二十审 9.2: 每数据源写租约终态释放
    # 九审 7.5: 汇总 partial failure——有失败时任务不能显示 succeeded
    failed = [r for r in results if not r.get("ok")]
    summary = {"refreshed": results,
               "total": len(results),
               "succeeded": len(results) - len(failed),
               "failed": len(failed)}
    if failed:
        summary["failed_details"] = [
            {"datasource_id": r["datasource_id"], "error": r.get("error", "")}
            for r in failed]
        # 十审 7.8: 抛之前逐条写任务日志——结构化详情在任务中心可见
        # (TaskManager 对异常只存 error string, 不保存局部 summary)
        for r in failed:
            handle.log(f"刷新失败 ds={r['datasource_id'][:8]}…: {r.get('error', '')}")
        handle.log(f"汇总: {len(results) - len(failed)}/{len(results)} 成功, "
                   f"{len(failed)} 失败")
        # 抛出让任务标 failed(平台 TaskManager 对异常写 status=failed)
        raise RuntimeError(
            f"元数据刷新: {len(failed)}/{len(results)} 个数据源失败 — "
            + "; ".join(r.get("error", "")[:60] for r in failed[:3]))
    return summary




# ══ 十七审: 共用结构验证器 + merge 报告(refresh/rescan 复用) ══
# 十六审的教训: 简单正则只能识别最规范的表达式(CASE/condition/引号
# JOIN/calculated 全部漏过), 集合去重把优先级做反(自动项压过人工项)。
# 本版原则:
#   1. 表达式引用提取换 sqlglot AST(解析失败 fail-closed 停用+复核);
#   2. 关系 ON 复用 graph_edit.parse_on_conditions(与人工增删同一解析器,
#      引号/多列 JOIN 天然一致);
#   3. 同名/同端点冲突时有效人工项优先, 自动项被压制;
#   4. 失效人工项停用进待复核清单(dropped_items), 不静默回退自动版本。

import re as _re

_MANUAL_SOURCES = ('manual', 'manual_edit')   # 旧数据 manual 兼容


class _Unparseable(Exception):
    """表达式无法解析(fail-closed 信号)。"""


def _new_report() -> dict:
    """merge 报告骨架(任务结果与语义页面共用结构)。"""
    return {"dropped_items": [], "conflicts": [], "requires_review": False}


def _report_drop(report, kind, table, name, reason):
    """记录一个被停用的语义项(人工项必须可见; 自动项 debug 级)。"""
    report["dropped_items"].append(
        {"kind": kind, "table": table, "name": name, "reason": reason})
    report["requires_review"] = True


def _report_conflict(report, table, manual, auto, detail):
    """记录一个人工/自动属性冲突(人工已保留, 管理员可复核)。

    十八审 6.6: 字段与 dropped_items 统一(kind/name/reason)——前端同一
    模板渲染, 不再出现空 kind 和 "table." 残缺定位; manual/auto 额外保留
    供详情展示。
    """
    report["conflicts"].append({
        "kind": "relationship_conflict", "table": table,
        "name": manual, "reason": detail,
        "manual": manual, "auto": auto})
    report["requires_review"] = True


def _validate_structure(new_content):
    """构造新结构的表→列映射(校验旧语义引用的物理基准)。"""
    return {m.name: {c.name for c in m.columns} for m in new_content.models}


def _expr_column_refs(expr: str) -> set:
    """sqlglot 提取表达式列引用 → {(table|None, column)}。

    bare 引用(如 SUM(amount) 的 amount)table=None, 归属指标所在表;
    限定引用(orders.amount)按 table 归属校验。解析失败抛 _Unparseable
    ——调用方 fail-closed(停用+待复核), 不允许"解析不了就放行"。
    """
    import sqlglot
    from sqlglot import exp as _sql_exp
    try:
        tree = sqlglot.parse_one(expr, read="postgres")
    except Exception as e:
        raise _Unparseable(f"{type(e).__name__}: {str(e)[:80]}")
    return {(c.table or None, c.name) for c in tree.find_all(_sql_exp.Column)}


def _refs_valid(refs, table_name, table_cols, all_tables):
    """列引用集合是否全部仍存在; 失效返回原因串, 有效返回 None。"""
    for table, col in refs:
        if table is None or table == table_name:
            if col not in table_cols:
                return f"missing_column: {col}"
        else:
            other = all_tables.get(table)
            if other is None or col not in other:
                return f"missing_column: {table}.{col}"
    return None


def _metric_valid(metric, table_name, table_cols, all_tables,
                  metric_names) -> str | None:
    """指标结构校验。composite: factor 闭包; single: formula+condition 引用。

    metric_names: 该表当前合并结果中的指标名集合(composite 校验基准)。
    """
    if metric.type == "composite" or metric.factor_metric_names:
        missing = [n for n in (metric.factor_metric_names or [])
                   if n not in metric_names]
        if missing:
            return f"missing_factor_metrics: {','.join(missing)}"
        return None
    for expr, label in ((metric.formula, "formula"), (metric.condition, "condition")):
        if not expr:
            continue
        try:
            refs = _expr_column_refs(expr)
        except _Unparseable:
            return f"unparseable_{label}"
        err = _refs_valid(refs, table_name, table_cols, all_tables)
        if err:
            return f"{label} {err}"
    return None


def _calc_valid(cf, table_name, table_cols, all_tables) -> str | None:
    """计算字段公式校验(行级表达式, 引用本表/跨表列)。"""
    try:
        refs = _expr_column_refs(cf.formula)
    except _Unparseable:
        return "unparseable_formula"
    return _refs_valid(refs, table_name, table_cols, all_tables)


def _rel_identity_and_valid(rel, table_name, new_content, all_tables):
    """关系校验 + 规范端点标识。

    校验复用 graph_edit.parse_on_conditions(人工增删关系的同一解析器):
    引号标识符、多列 AND JOIN 与图谱编辑口径天然一致。

    Returns:
        (identity, error): identity 为规范化端点元组(解析成功时);
        error 非空 = 失效原因(此时 identity 为 None)。
    """
    from domains.chatbi.graph_edit import parse_on_conditions, GraphEditError
    if rel.target_model not in all_tables:
        return None, f"missing_table: {rel.target_model}"
    if not (rel.on or "").strip():
        return None, "missing_on"
    try:
        conds = parse_on_conditions(new_content, table_name,
                                    rel.target_model, rel.on or "")
    except GraphEditError as e:
        return None, str(e)
    identity = (rel.target_model, tuple(sorted(
        (c["left_table"], c["left_column"], c["right_table"], c["right_column"])
        for c in conds)))
    return identity, None


def _merge_metrics(old_m, new_m, all_tables, new_content, report,
                   keep_unregenerated_auto: bool) -> None:
    """指标合并(十七审 P0 7.1 + P1 7.3)。

    优先级(同名冲突时):
      1. 有效人工(manual/manual_edit)——最高, 压制同名自动项;
      2. 新扫描项(规则 simple + LLM 产物);
      3. 旧 composite(人工优先于 auto)——factor 闭包对合并后名集校验;
      4. 旧 auto simple 未再生项——仅 refresh 保留(刷新不跑 LLM, 不保留
         会丢 LLM/运行时产物); rescan 的 LLM 全量重推, 不保留避免累积。

    失效人工项停用进 dropped_items; 其名字仍占位(同名自动项不得静默
    顶替——宁缺毋滥, 管理员在复核清单里决定)。
    """
    table_name = new_m.name
    table_cols = all_tables.get(table_name, set())
    old_metrics = list(old_m.metrics or [])
    old_by_name = {m.name: m for m in old_metrics}
    manual_names = {m.name for m in old_metrics
                    if (getattr(m, 'source', '') or '') in _MANUAL_SOURCES}
    new_names = {m.name for m in (new_m.metrics or [])}

    merged: list = []
    taken: set = set()

    def _is_composite(m):
        return m.type == "composite" or bool(m.factor_metric_names)

    # ── 1) 有效人工 simple 优先 ──
    for met in old_metrics:
        src = (getattr(met, 'source', '') or '')
        if src not in _MANUAL_SOURCES or _is_composite(met):
            continue
        err = _metric_valid(met, table_name, table_cols, all_tables, ())
        if err:
            _report_drop(report, "metric", table_name, met.name, err)
            logger.warning("merge: 停用失效人工指标 %s (table=%s): %s",
                           met.name, table_name, err)
        else:
            merged.append(met)
            taken.add(met.name)

    # ── 2) 新扫描项(人工占位名/已取名压制; 扫描内部同名保留首个) ──
    for nm in (new_m.metrics or []):
        if nm.name in manual_names or nm.name in taken:
            continue
        # 二十审 P0-1: 同名同式规则指标的语义标注原子继承。
        # refresh(llm=None) 的规则指标基于退化列名生成("amount合计");
        # 列中文标注在 merge 里恢复了, 但新指标对象不回溯——同名旧项
        # 的中文名被静默顶替(真实环境 104 处退化)。同名+同公式+同
        # 条件+同类型 = 同一指标的新结构副本, display_name/description/
        # co_occurrence 是语义标注, 必须随旧项继承; 公式/条件/类型
        # 变化则属于真实结构变化, 用新值并记入复核清单(不静默)。
        old_counterpart = old_by_name.get(nm.name)
        if old_counterpart is not None:
            if getattr(old_counterpart, 'co_occurrence', 0):
                nm.co_occurrence = old_counterpart.co_occurrence
            same_expr = ((old_counterpart.formula or '') == (nm.formula or '')
                         and (old_counterpart.condition or None)
                         == (nm.condition or None)
                         and (old_counterpart.type or 'single')
                         == (nm.type or 'single'))
            if same_expr:
                if old_counterpart.display_name:
                    nm.display_name = old_counterpart.display_name
                if old_counterpart.description:
                    nm.description = old_counterpart.description
            elif (getattr(old_counterpart, 'source', '')
                  in _MANUAL_SOURCES) or old_counterpart.display_name:
                # 旧项有语义标注但表达式变了 → 管理员可见, 不静默
                _report_conflict(
                    report, new_m.name, old_counterpart.name, nm.name,
                    f"同名指标表达式变化: 旧「{old_counterpart.display_name}"
                    f"」({old_counterpart.formula}) → 新「{nm.display_name}」"
                    f"({nm.formula}), 已采用新公式")
        merged.append(nm)
        taken.add(nm.name)

    # ── 3) 旧 composite(人工排前: 同名时人工版本胜出) ──
    # 十八审 P0-1: 此前守卫 `met.name in manual_names` 对人工 composite 恒真
    # (它的名字一开始就被收进 manual_names)——自己跳过自己, refresh/rescan
    # 静默删除页面创建的人工复合指标且不进复核。manual_names 只用于压制
    # 同名 auto 项, 不得压制人工 composite 本身。
    old_comps = [m for m in old_metrics if _is_composite(m)]
    old_comps.sort(key=lambda m: 0 if (getattr(m, 'source', '') or '')
                   in _MANUAL_SOURCES else 1)
    for met in old_comps:
        src = (getattr(met, 'source', '') or '')
        is_manual = src in _MANUAL_SOURCES
        if met.name in taken:
            continue  # 同名已被占(人工 simple/新扫描/先前 composite)
        if not is_manual and met.name in manual_names:
            continue  # 同名人工占位(即使其已停用), auto 版本让路
        err = _metric_valid(met, table_name, table_cols, all_tables, taken)
        if err:
            if is_manual:
                _report_drop(report, "metric", table_name, met.name, err)
                logger.warning("merge: 停用失效人工 composite %s (table=%s): %s",
                               met.name, table_name, err)
            else:
                logger.debug("merge: 丢弃闭包失效的 auto composite %s: %s",
                             met.name, err)
            continue
        merged.append(met)
        taken.add(met.name)

    # ── 4) 旧 auto simple 未再生(仅 refresh) ──
    if keep_unregenerated_auto:
        for met in old_metrics:
            src = (getattr(met, 'source', '') or '')
            if (src in _MANUAL_SOURCES or _is_composite(met)
                    or met.name in taken or met.name in manual_names
                    or met.name in new_names):
                continue
            if src in ("rule_inferred", "foreign_key"):
                continue  # 规则产物未再生 = 规则不再命中(结构变化), 丢弃
            err = _metric_valid(met, table_name, table_cols, all_tables, taken)
            if err:
                logger.debug("merge: 丢弃失效旧 auto 指标 %s: %s", met.name, err)
                continue
            merged.append(met)
            taken.add(met.name)

    new_m.metrics = merged


def _merge_relationships(old_m, new_m, all_tables, new_content, report) -> None:
    """关系合并(十七审 P0 7.2)。

    优先级(等价端点冲突时):
      1. 有效人工(manual/manual_edit)——名称/JOIN 类型/基数全保留;
      2. 新扫描 FK/命名关系——与人工等价时被压制; JOIN 类型或基数不同
         时记录 conflicts(人工已保留, 管理员可见, 不静默选择);
      3. 旧 ai_inferred/演化学习关系未再生——结构仍有效则保留
         (refresh 不重跑演化, 丢弃会丢学习成果);
      4. 旧 foreign_key 未再生——丢弃(内省产物, 未再生 = 数据库已删该 FK)。
    """
    table_name = new_m.name
    merged: list = []
    ids: set = set()

    # ── 1) 有效人工优先(端点占位) ──
    for rel in (old_m.relationships or []):
        src = (getattr(rel, 'source', '') or '')
        if src not in _MANUAL_SOURCES:
            continue
        identity, err = _rel_identity_and_valid(
            rel, table_name, new_content, all_tables)
        if err:
            _report_drop(report, "relationship", table_name, rel.name, err)
            logger.warning("merge: 停用失效人工关系 %s (table=%s): %s",
                           rel.name, table_name, err)
            continue
        if identity in ids:
            continue  # 人工内部等价重复, 保留首个
        merged.append(rel)
        ids.add(identity)
    manual_by_id = {_rel_identity_and_valid(r, table_name, new_content,
                                            all_tables)[0]: r
                    for r in merged}

    # ── 2) 新扫描项: 人工等价压制 + 属性冲突可见 ──
    for nr in (new_m.relationships or []):
        identity, err = _rel_identity_and_valid(
            nr, table_name, new_content, all_tables)
        if err or identity is None:
            continue  # 扫描项解析失败罕见(自身刚生成)
        if identity in manual_by_id:
            mr = manual_by_id[identity]
            if ((mr.join_type or None) != (nr.join_type or None)
                    or (mr.type or None) != (nr.type or None)):
                _report_conflict(
                    report, table_name, mr.name, nr.name,
                    f"ON 等价但属性不同: 人工 {mr.join_type}/{mr.type} vs "
                    f"自动 {nr.join_type}/{nr.type}(已保留人工配置)")
                logger.info("merge: 人工关系 %s 与自动 %s 端点等价但属性不同"
                            "(保留人工, 已记入复核)", mr.name, nr.name)
            continue
        if identity in ids:
            continue  # 与其他自动项等价(保留首个)
        merged.append(nr)
        ids.add(identity)

    # ── 3/4) 旧 auto 关系未再生项 ──
    for rel in (old_m.relationships or []):
        src = (getattr(rel, 'source', '') or '')
        if src in _MANUAL_SOURCES:
            continue
        identity, err = _rel_identity_and_valid(
            rel, table_name, new_content, all_tables)
        if err or identity is None or identity in ids:
            continue
        if src == "foreign_key":
            continue  # FK 内省产物: 未再生 = 数据库已删该外键
        # ai_inferred/name_pattern 等演化学习关系: 结构仍有效则保留
        merged.append(rel)
        ids.add(identity)

    new_m.relationships = merged


def _merge_calculated_fields(old_m, new_m, all_tables, report) -> None:
    """计算字段: 保留旧的, 但公式引用失效的停用+待复核(十七审 7.5)。"""
    table_name = new_m.name
    table_cols = all_tables.get(table_name, set())
    kept = []
    for cf in (getattr(old_m, 'calculated_fields', None) or []):
        err = _calc_valid(cf, table_name, table_cols, all_tables)
        if err:
            _report_drop(report, "calculated_field", table_name, cf.name, err)
            logger.warning("merge: 停用失效计算字段 %s (table=%s): %s",
                           cf.name, table_name, err)
            continue
        kept.append(cf)
    new_m.calculated_fields = kept


def _apply_manual_annotation(old_item, new_item, table_level: bool) -> None:
    """manual_edit 的原子保留: 值+来源+置信度一起迁移(十五审 P0 教训)。"""
    if old_item.display_name:
        new_item.display_name = old_item.display_name
    if old_item.description is not None:
        new_item.description = old_item.description
    if not table_level and old_item.semantic_type:
        new_item.semantic_type = old_item.semantic_type
    new_item.source = old_item.source
    if getattr(old_item, 'confidence', None) is not None:
        new_item.confidence = old_item.confidence


def _merge_content(old, new, report=None):
    """结构刷新合并(refresh 专用; llm=None 的定时结构扫描)。

    语义: 新物理结构权威(增删表列), 旧标注按来源分治保留——
      manual_edit: 原子保留(值+来源+置信度);
      db_comment: 用本次数据库注释(comment 变更生效, 值+来源原子更新;
                  注释被删 → 退化新扫描值 + 复核条目);
      auto_inferred: 旧 LLM 展示值保留(refresh 不重跑 LLM), 但新库新增
                  注释时新注释覆盖(值+来源一起换, 禁止错配)。
    指标/关系/计算字段: 经结构校验后人工优先(见 _merge_metrics 等)。
    """
    if report is None:
        report = _new_report()
    all_tables = _validate_structure(new)
    old_models = {m.name: m for m in old.models}
    for new_m in new.models:
        old_m = old_models.get(new_m.name)
        if old_m is None:
            continue  # 新表: 用扫描退化值, 等下次 LLM 富化

        # ── 表级: 按来源分治(十七审 7.4) ──
        old_src = (getattr(old_m, 'source', '') or '')
        new_src = (getattr(new_m, 'source', '') or '')
        if old_src in _MANUAL_SOURCES:
            _apply_manual_annotation(old_m, new_m, table_level=True)
        elif old_src == 'db_comment':
            if new_src != 'db_comment':
                # 注释被删除: 退化到新扫描值(表名/auto), 记复核
                _report_drop(report, "db_comment_removed", new_m.name,
                             new_m.name, "数据库表注释已被删除, 展示名退化为表名")
        else:  # auto_inferred 等
            if new_src == 'db_comment':
                pass  # 新增注释覆盖旧 auto 值(值+来源用新扫描, 原子)
            else:
                # 旧 LLM 值原子保留(display+source+confidence 一起)
                if old_m.display_name:
                    new_m.display_name = old_m.display_name
                if old_m.description is not None:
                    new_m.description = old_m.description
                new_m.source = old_m.source
                if old_m.confidence is not None:
                    new_m.confidence = old_m.confidence

        # ── 列级: 同一来源分治 ──
        old_cols = {c.name: c for c in old_m.columns}
        for c in new_m.columns:
            old_c = old_cols.get(c.name)
            if old_c is None:
                continue
            o_src = (getattr(old_c, 'source', '') or '') or ''
            n_src = (getattr(c, 'source', '') or '') or ''
            if o_src in _MANUAL_SOURCES:
                _apply_manual_annotation(old_c, c, table_level=False)
            elif o_src == 'db_comment':
                if n_src != 'db_comment':
                    _report_drop(report, "db_comment_removed", new_m.name,
                                 c.name, f"列 {c.name} 的数据库注释已被删除")
            else:
                if n_src == 'db_comment':
                    pass  # 新增注释覆盖
                else:
                    if old_c.display_name:
                        c.display_name = old_c.display_name
                    if old_c.description is not None:
                        c.description = old_c.description
                    if old_c.semantic_type:
                        c.semantic_type = old_c.semantic_type
                    c.source = old_c.source
                    if old_c.confidence:
                        c.confidence = old_c.confidence

        # ── 指标/关系/计算字段: 人工优先 + 结构校验 + 未再生 auto 保留 ──
        _merge_metrics(old_m, new_m, all_tables, new, report,
                       keep_unregenerated_auto=True)
        _merge_relationships(old_m, new_m, all_tables, new, report)
        _merge_calculated_fields(old_m, new_m, all_tables, report)

    new.sample_questions = old.sample_questions or new.sample_questions
    return new


def _merge_rescan(old, new, report=None):
    """重扫专用 merge(全量 LLM 重推)。

    与 refresh 的差异:
      - LLM 富化/指标/示例问题全量重跑 → auto 值用新扫描的(重扫目的),
        未再生的旧 auto 指标不保留(避免非确定性累积);
      - manual_edit 标注原子保留;
      - db_comment 由扫描重读, 注释变更自然生效;
      - 指标/关系同样人工优先 + 结构校验(共用 _merge_metrics 等)。
    """
    if report is None:
        report = _new_report()
    all_tables = _validate_structure(new)
    old_models = {m.name: m for m in old.models}

    for new_m in new.models:
        old_m = old_models.get(new_m.name)
        if old_m is None:
            continue

        # ── 表级: manual_edit 原子保留; 其余(auto/db_comment)用新扫描值 ──
        old_src = (getattr(old_m, 'source', '') or '')
        if old_src in _MANUAL_SOURCES:
            _apply_manual_annotation(old_m, new_m, table_level=True)

        # ── 列级: 同构 ──
        old_cols = {c.name: c for c in old_m.columns}
        for c in new_m.columns:
            old_c = old_cols.get(c.name)
            if old_c is None:
                continue
            if ((getattr(old_c, 'source', '') or '')
                    in _MANUAL_SOURCES):
                _apply_manual_annotation(old_c, c, table_level=False)

        # ── 指标/关系/计算字段: 人工优先 + 结构校验 ──
        _merge_metrics(old_m, new_m, all_tables, new, report,
                       keep_unregenerated_auto=False)
        _merge_relationships(old_m, new_m, all_tables, new, report)
        _merge_calculated_fields(old_m, new_m, all_tables, report)

    # 示例问题: 用新生成(重扫目的)
    return new


# ── merge 报告持久化(十七审 7.6: 任务结果 + 语义页面可见) ──────────

# 报告保存结果三态(二十审 9.4): 只有 failed 触发告警;
# superseded 是正常的乱序让路(旧任务报告被新版本淘汰), 只记审计
REPORT_SAVED = "saved"
REPORT_SUPERSEDED = "superseded"
REPORT_FAILED = "failed"


def save_merge_report_fenced(lease, db, data_source_id: str, version: int,
                             report: dict) -> str:
    """merge 报告的租约同事务写入(二十二审 8)。

    持有 write lease 时: 检查与写入同事务(失租拒写, TOCTOU 关闭)。
    无 lease(API 人工路径/测试)退化为普通三态保存。
    """
    if lease is None:
        return save_merge_report(db, data_source_id, version, report)
    lease.db = lease.db or db   # 保险: 测试构造可能未填 db

    def _write(conn):
        import json
        cur = conn.execute(
            "INSERT INTO chatbi_merge_reports "
            "(data_source_id, version, report, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (data_source_id) DO UPDATE SET "
            "version = EXCLUDED.version, report = EXCLUDED.report, "
            "updated_at = EXCLUDED.updated_at "
            "WHERE EXCLUDED.version >= chatbi_merge_reports.version",
            (data_source_id, version,
             json.dumps(report, ensure_ascii=False), _now_iso()))
        if getattr(cur, "rowcount", 0):
            return REPORT_SAVED
        row = conn.execute(
            "SELECT version FROM chatbi_merge_reports "
            "WHERE data_source_id = ?", (data_source_id,)).fetchone()
        return (REPORT_SAVED
                if row and int(row["version"]) == version
                else REPORT_SUPERSEDED)

    ok, result = lease.execute_if_owned(_write)
    if ok:
        return result
    logger.warning("merge 报告被事务级 fencing 拒绝(ds=%s v%s)",
                   data_source_id, version)
    return REPORT_FAILED

def save_merge_report(db, data_source_id: str, version: int,
                      report: dict) -> str:
    """落 merge 报告(语义页面 review 接口的数据源)。

    十九审 6.2: UPSERT 带 version 守卫——报告必须单调不倒退。语义保存
    与报告写入是两条语句, 慢的旧任务(v11)可能在 v12 报告之后落库;
    无守卫时页面会回退显示过期告警。同版本(>=)允许覆盖——重试路径
    以修正后的报告刷新自身版本是合法的。

    Returns:
        REPORT_SAVED=写入生效;
        REPORT_SUPERSEDED=被更高版本正常淘汰(乱序让路, 非失败);
        REPORT_FAILED=数据库异常(调用方应告警)。
    """
    import json
    try:
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO chatbi_merge_reports "
                "(data_source_id, version, report, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (data_source_id) DO UPDATE SET "
                "version = EXCLUDED.version, report = EXCLUDED.report, "
                "updated_at = EXCLUDED.updated_at "
                "WHERE EXCLUDED.version >= chatbi_merge_reports.version",
                (data_source_id, version, json.dumps(report, ensure_ascii=False),
                 _now_iso()))
            # DO UPDATE ... WHERE 不命中时行被跳过: psycopg rowcount=0
            # → 乱序旧版本让路(二十审 9.4: 这是正常并发结果, 非失败)
            if getattr(cur, "rowcount", 0):
                return REPORT_SAVED
            # rowcount 不可靠的驱动: 回读确认版本归属
            row = conn.execute(
                "SELECT version FROM chatbi_merge_reports "
                "WHERE data_source_id = ?", (data_source_id,)).fetchone()
            return (REPORT_SAVED
                    if row and int(row["version"]) == version
                    else REPORT_SUPERSEDED)
    except Exception as e:
        logger.warning("merge 报告落库失败(ds=%s v%s): %s",
                       data_source_id, version, e)
        return REPORT_FAILED


def get_merge_report(db, data_source_id: str):
    """读最近一次 merge 报告; 无记录或结构损坏 → None。"""
    import json
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT version, report FROM chatbi_merge_reports "
                "WHERE data_source_id = ?", (data_source_id,)).fetchone()
        if not row:
            return None
        return {"version": int(row["version"]),
                "report": json.loads(row["report"])}
    except Exception:
        return None


def _log_report(handle, report: dict, version_label: str) -> None:
    """merge 报告的关键内容写任务日志(任务中心可见)。"""
    dropped = report.get("dropped_items") or []
    conflicts = report.get("conflicts") or []
    for item in dropped:
        handle.log(f"⚠ 停用 {item['kind']} {item['table']}.{item['name']}: "
                   f"{item['reason']}")
    for c in conflicts:
        handle.log(f"⚠ 关系属性冲突 {c['table']}: {c['detail']}")
    if dropped or conflicts:
        handle.log(f"共停用 {len(dropped)} 项 / 属性冲突 {len(conflicts)} 项"
                   f"({version_label})——详情见语义层页面「待复核」提示")


def _make_index_rebuilder(app_state, datasource_id: str):
    """构造图谱演化的索引重建回调(I2: 演化落新版本后索引不漂移)。

    apply_confidence_updates 以关键字调用 rebuild_index(content=..., data_source_id=...);
    向量设施不可用时返回 None(调用方按契约跳过重建, 只记 debug)。
    """
    try:
        from domains.chatbi import runtime, stores as cb_stores, indexing
        llm = runtime.get_llm(app_state) if app_state else None
        if llm is None:
            return None
        store = cb_stores.get_vector(app_state)
        embedder = cb_stores.get_embedder(llm)
        db = runtime.get_pack_db()
    except Exception as e:
        logger.warning("索引重建回调构造失败(演化后跳过重建): %s", e)
        return None

    def _rebuild(content, data_source_id, **_kw):
        # 十三审 7.3: 从 kwargs 取 expected_version(apply 传入了 new_version)
        result = indexing.guarded_rebuild(
            content=content, data_source_id=data_source_id,
            store=store, embedder=embedder, db=db,
            expected_version=_kw.get('expected_version'))
        # rebuild_index 的失败契约是返回 RebuildResult(error=...) 而非抛
        # 异常(五审 5.2)——error 非空时 raise, 让调用方的 except/
        # on_index_error 降级路径真实生效, 不再谎报成功
        if result is not None and getattr(result, "error", None):
            raise RuntimeError(f"索引重建失败: {result.error}")
        return result
    return _rebuild


def _linkage_sync_kwargs(app_state) -> dict:
    """linkage→图谱同步的阈值参数(设置页 > graph_core 默认常量)。

    复核报告 P2: 共现阈值/新表对阈值/置信度增量/发现开关原先硬编码
    常量, 不同数据规模只能改代码——统一收敛到设置链。
    """
    s = _load_settings(app_state)
    return {
        "co_occurrence_threshold": int(s.get(
            "graph_linkage_co_occurrence_threshold", 3)),
        "new_pair_threshold": int(s.get(
            "graph_linkage_new_pair_threshold", 5)),
        "confidence_boost": float(s.get(
            "graph_linkage_confidence_boost", 0.1)),
        "discover_new_pairs": bool(s.get(
            "graph_linkage_discover_new_pairs", True)),
    }


def _graph_sync_targets(store, result: dict, data_source_id=None) -> set:
    """计算图谱同步的目标数据源集合(三审 P0: 重试语义必须真实可用)。

    组成(并集):
      - 显式指定的 data_source_id —— 无论本次是否产生合并记忆都同步;
      - 本次参与整理的全部候选 scope(不只是成功产出组——首次失败/
        无产出时再次提交仍能命中同一批数据源);
      - 全局整理(未指定 ds)时: linkage 记忆的 distinct 数据源——
        linkage 永远不参与合并, 但它是图谱反哺的直接数据源,
        只有 linkage 也要同步(源: "数据源只有 linkage 记忆时提交
        整理仍会执行图谱同步"验收场景)。
    """
    targets: set = set()
    if data_source_id:
        targets.add(data_source_id)
    for s in result.get("candidate_scopes") or result.get("scopes") or []:
        if s.get("data_source_id"):
            targets.add(s["data_source_id"])
    if not data_source_id:
        # 全局整理: linkage 的 distinct ds 一并纳入(linkage 永不参与合并
        # 也永不被标记整理, 无需 consolidated 过滤)
        try:
            for m in store.list_memories():
                if m.get("type") == "linkage" and m.get("data_source_id"):
                    targets.add(m["data_source_id"])
        except Exception as e:
            logger.warning("linkage 数据源清单读取失败(跳过该来源): %s", e)
    return targets


def _evolve_graph(db, datasource_id: str, content, app_state=None) -> dict:
    """图谱置信度演化 (SEM-003;graph_infer 移植栈的接线点):
    查询历史(fewshot 示例 SQL) → 频繁 JOIN 表对挖掘 → confidence 提升 →
    乐观锁写回语义层新版本。

    六审 P1 重构: 两类信号(linkage + implicit)不再各自独立写版本——
    先聚合到同一 current 快照, 一次乐观锁写入; 索引重建由调用方在
    演化完成后统一执行(用最终 current, 不再用演化前的旧内容)。

    Returns:
        {"versions_written": int, "index_rebuild": str}
    """
    from domains.chatbi.graph_infer import (mine_implicit_relationships,
                                            apply_confidence_updates,
                                            sync_linkage_to_graph)
    from domains.chatbi import semantic as semantic_mod
    result = {"versions_written": 0, "index_rebuild": "skipped"}

    try:
        from domains.chatbi import stores as cb_stores
        history = [row["sql"] for row in
                   cb_stores.list_fewshot_examples(db, datasource_id)
                   if row.get("sql")]
    except Exception as e:
        logger.warning("查询历史读取失败(跳过演化): %s", e)
        return result

    # ── 聚合信号: linkage 共现 + implicit mining, 基于同一 current ──
    # 六审 P1.B: 此前 linkage 写完新版本后, implicit 仍用旧 existing
    # 计算——可能把刚提升的 confidence 写低。现在先取 linkage 的
    # 目标值, 再取 implicit 建议, 合并后一次写入。
    from domains.chatbi.memory import get_memory_store
    from domains.chatbi import semantic as semantic_mod
    from domains.chatbi.graph_infer import (linkage_memories_to_cooccurrence,
                                            _compute_confidence_updates,
                                            _discover_new_pairs,
                                            ensure_watermark_schema,
                                            set_watermark)
    kwargs = _linkage_sync_kwargs(app_state)
    cooccurrence = linkage_memories_to_cooccurrence(
        get_memory_store(db), datasource_id)

    # 重载 current(不依赖调用方传入的 content——可能已过时)
    current_content = semantic_mod.load_current_content(db, datasource_id)
    if current_content is None:
        return result
    existing = [r for m in current_content.models for r in m.relationships]

    all_updates: dict = {}
    all_new_pairs: list = []
    watermarks_to_set: list = []  # [(pair, signal, evidence)]

    try:
        ensure_watermark_schema(db)
    except Exception:
        pass  # 水位表建失败退化为无水位(全量计算, 不崩溃)

    # 信号 1: linkage 共现 → 水位感知 boost(七审 6.2:
    # 保留"共现达阈值即提升"的原语义 + 同证据不重复消费)
    if cooccurrence:
        link_updates, link_wm = _compute_confidence_updates(
            cooccurrence=cooccurrence,
            existing_relationships=existing,
            co_occurrence_threshold=kwargs["co_occurrence_threshold"],
            confidence_boost=kwargs["confidence_boost"],
            db=db, data_source_id=datasource_id, signal="linkage")
        all_updates.update(link_updates)
        watermarks_to_set.extend((p, "linkage", e) for p, e in link_wm.items())
        if kwargs["discover_new_pairs"]:
            all_new_pairs.extend(_discover_new_pairs(
                cooccurrence=cooccurrence,
                existing_relationships=existing,
                new_pair_threshold=kwargs["new_pair_threshold"]))

    # 信号 2: implicit mining(历史 SQL) → 水位感知(七审 6.3)
    # 七审 6.1 修复: 返回类型是 dict[pair, float], 用 .items() 遍历
    suggestions = mine_implicit_relationships(
        history, existing, db=db, data_source_id=datasource_id)
    for pair, conf in suggestions.items():
        if pair in all_updates:
            all_updates[pair] = max(all_updates[pair], conf)  # 取高不取低
        else:
            all_updates[pair] = conf
    # implicit 的水位也更新(证据量 = 达标历史的当前计数)
    from collections import Counter as _Counter
    import re as _re
    _table_re = _re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", _re.IGNORECASE)
    _known_names = set()
    for r in existing:
        f = r.name.split("_to_")[0] if "_to_" in r.name else ""
        if f:
            _known_names.add(f)
            _known_names.add(r.target_model)
    _implicit_co = _Counter()
    for sql in history:
        _tabs = set(_table_re.findall(sql))
        for t1 in _known_names:
            for t2 in _known_names:
                if t1 < t2 and t1 in _tabs and t2 in _tabs:
                    _implicit_co[(t1, t2)] += 1
    for pair, cnt in _implicit_co.items():
        watermarks_to_set.append((pair, "implicit", cnt))

    # 十七审: 臆造 ON 源头过滤——implicit_mining 的新表对在 evolve 侧就用
    # 当前结构校验端点列, 全无效时不进 apply(否则 apply 收不到任何有效
    # 变更会 raise"无有效更新内容"→ 刷新误报失败; 且坏关系写进内容会被
    # 下轮 merge 丢弃, 形成"挖→丢→再挖"的无限版本膨胀)。
    if all_new_pairs:
        from domains.chatbi.graph_infer import _implicit_on_valid
        _cols = {m.name: {c.name for c in m.columns}
                 for m in current_content.models}
        _before = len(all_new_pairs)
        all_new_pairs = [p for p in all_new_pairs
                         if _implicit_on_valid(_cols, p[0][0], p[0][1])]
        if len(all_new_pairs) < _before:
            logger.info("隐式挖掘: %d 个新表对因臆造 ON 列不存在被过滤",
                        _before - len(all_new_pairs))

    if not all_updates and not all_new_pairs:
        # 无更新也推进水位(避免下轮重复计算同量证据); 失败显式记录
        # (八审 6.1: 无语义变更所以无重复消费风险, 但要可观测)
        for pair, sig, ev in watermarks_to_set:
            try:
                set_watermark(db, datasource_id, pair, sig, ev)
            except Exception as e:
                logger.warning("水位推进失败(无语义变更, 影响有限): %s", e)
        return result  # 无新证据 → 不写版本(幂等)

    # 乐观锁: 读当前版本号
    current_version = None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT version FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND is_current = 1",
                (datasource_id,)).fetchone()
            current_version = row["version"] if row else None
    except Exception as e:
        logger.warning("乐观锁版本读取失败(降级为不校验): %s", e)

    # 不传 rebuild_index——索引由调用方在全部演化完成后统一重建(六审P1.C)。
    # 水位经 pending_watermarks 与语义版本同事务原子提交(八审 6.1)。
    from domains.chatbi.graph_infer import VersionConflictError
    try:
        apply_confidence_updates(
            db, datasource_id, all_updates,
            new_pairs=all_new_pairs or None,
            expected_version=current_version,
            pending_watermarks=[(pair, sig, ev)
                                for pair, sig, ev in watermarks_to_set])
        result["versions_written"] = 1
        result["index_rebuild"] = "deferred"  # 调用方负责最终重建
        logger.info("图谱演化: %d updates, %d new_pairs (ds=%s)",
                    len(all_updates), len(all_new_pairs), datasource_id)
    except VersionConflictError as e:
        logger.warning("图谱演化版本冲突(有并发写入, 不覆盖): %s", e)
        result["index_rebuild"] = "conflict"
    except Exception as e:
        # 编程错误让任务失败可见(七审 6.4: 不再统称"可能版本冲突")
        logger.exception("图谱演化写入异常: %s", e)
        result["index_rebuild"] = "error"
        result["error"] = str(e)[:200]
    return result


def _connect_info(info) -> dict:
    kw = info.connection_kwargs()
    kw["db_type"] = info.db_type
    return kw


def _load_settings(app_state) -> dict:
    """读 pack 设置(设置页保存值 > env > schema 默认)。

    经 runtime.settings_reader(sdk.pack_api 门面)——不走 services 直连,
    且必须携带 settings_store:管理端热改的保存值要让后台任务即时生效。
    """
    try:
        from domains.chatbi import runtime
        return dict(runtime.get_settings_reader(app_state).all())
    except Exception as e:
        # 十九审: 此前 AttributeError 被静默吞掉——runtime 模块只有
        # get_settings_reader, settings_reader 属性不存在, 导致所有
        # 后台任务设置(刷新周期/健康间隔/检索参数)永远走缺省
        logger.warning("读 pack 设置失败, 全部走缺省: %s", e)
        return {}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
