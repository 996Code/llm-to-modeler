"""TaskManager - 通用后台批量任务框架(调度 / 生命周期 / 进度 / SSE 发布)。

【模块定位】
平台的"任务容器"。handler(任务具体做什么)由 pack 在装配时注册
(pack.py 可选钩子 register_tasks(registry)),本模块零任务类型知识——
与 ToolRegistry 之于工具的关系完全同构。

【核心机制】
  - 线程池执行(TASK_WORKERS,默认 2);任务函数是同步的,内部调 LLM/
    写库都是同步 API,与引擎节点同一执行模型。
  - queue_key 串行:同 key 的任务 FIFO 串行(如 queue_key=kb_id,同库导入
    不并发写图),不同 key 并行;无 key 的任务互不阻塞。
  - 协作式取消:cancel() 置标志,handler 在检查点调 check_cancel() 抛
    TaskCancelled;pending 期的任务直接落 cancelled。
  - 持久化:每次状态/进度变更同步落 TaskStore,服务重启后遗留任务标
    interrupted(不自动续跑,由 pack 用幂等重跑承接)。
  - SSE 发布:进度/日志/状态变更同时推给内存订阅者(api/tasks.py 的
    events 端点);日志本体持久化在 task_logs,断线可按 id 游标补齐。

【Java 类比】
TaskManager ≈ 线程池版 Spring @Async + TaskExecutor;TaskHandle ≈
JobExecutionContext(handler 通过它回写进度/日志/检查取消)。
"""
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from services.task_store import FINAL_STATUSES, TaskStore

logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    """任务被取消(handler 在检查点抛出,由 _run 统一收尾为 cancelled)。"""


class TaskHandle:
    """handler 与框架交互的句柄(进度上报 / 日志 / 取消检查)。"""

    def __init__(self, manager: "TaskManager", task_id: str, payload: Dict[str, Any]):
        self._manager = manager
        self.task_id = task_id
        self.payload = payload or {}

    def set_progress(self, percent: int, message: str = "") -> None:
        """上报进度(0-100 夹取;持久化 + SSE 双路)。"""
        pct = max(0, min(100, int(percent)))
        self._manager._store.update_task(
            self.task_id, progress=pct, progress_message=message or ""
        )
        self._manager._publish(self.task_id, {
            "event": "progress",
            "data": {"taskId": self.task_id, "progress": pct, "message": message or ""},
        })

    def log(self, message: str, level: str = "info", **data) -> None:
        """写一条任务日志(持久化 + SSE;**data 进结构化附加字段)。"""
        log_id, created_at = self._manager._store.append_log(
            self.task_id, level=level, message=message, data=data or None
        )
        self._manager._publish(self.task_id, {
            "event": "log",
            "data": {"id": log_id, "taskId": self.task_id, "level": level,
                     "message": message, "data": data or None,
                     "createdAt": created_at},
        })

    @property
    def cancelled(self) -> bool:
        """是否已被要求取消(不抛异常的只读探测)。"""
        return self._manager._is_cancel_requested(self.task_id)

    def check_cancel(self) -> None:
        """取消检查点:已被要求取消则抛 TaskCancelled。

        handler 应在自然的批次/块边界调用(如每个 chunk 处理前),
        形成协作式取消。
        """
        if self.cancelled:
            raise TaskCancelled(f"task {self.task_id} cancelled")


class TaskManager:
    """后台任务的调度与生命周期管理。"""

    def __init__(self, store: TaskStore, max_workers: Optional[int] = None):
        self._store = store
        if max_workers is None:
            try:
                max_workers = max(1, int(os.getenv("TASK_WORKERS", "2")))
            except ValueError:
                max_workers = 2
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="bgtask"
        )

        self._handlers: Dict[str, Callable[[TaskHandle], Any]] = {}
        self._handler_meta: Dict[str, Dict[str, str]] = {}  # type -> {packName}

        self._lock = threading.Lock()
        self._pending: List[str] = []            # FIFO 等待队列(按提交顺序)
        self._task_keys: Dict[str, str] = {}     # task_id -> effective queue key
        self._active_keys: set = set()           # 占用中的串行键
        self._running_count = 0
        self._cancel_flags: set = set()          # 被要求取消的 running 任务
        self._listeners: Dict[str, List["queue.Queue"]] = {}
        # 终态监听者(任务到达 succeeded/failed/cancelled 时回调,收 task dict)。
        # pack 用它释放"提交期登记、但 handler 可能根本没执行"的资源
        # (如 pending 期被取消的任务——finally 不生效,只能靠这里兜底)。
        self._terminal_listeners: List[Callable[[Dict[str, Any]], None]] = []

    # ── handler 注册(装配期调用) ───────────────────────────

    @property
    def store(self) -> TaskStore:
        """持久化层只读出口(api 层用它查任务/日志,不摸私有成员)。"""
        return self._store

    def register(self, task_type: str, handler: Callable[[TaskHandle], Any],
                 pack_name: str = "") -> None:
        """注册任务类型。重名覆盖(热切换后 pack 重新注册是正常路径)。"""
        self._handlers[task_type] = handler
        self._handler_meta[task_type] = {"packName": pack_name}
        logger.debug(f"task handler registered: {task_type} (pack={pack_name})")

    def reset_handlers(self) -> None:
        """清空全部 handler(每次 assemble 前调用——禁用的 pack 类型随之失效)。"""
        self._handlers.clear()
        self._handler_meta.clear()

    def registered_types(self) -> List[Dict[str, str]]:
        """已注册任务类型清单(GET /api/tasks/types)。"""
        return [
            {"type": t, "packName": meta.get("packName", "")}
            for t, meta in sorted(self._handler_meta.items())
        ]

    def add_terminal_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """注册终态监听者:任务到达终态(任何路径,含 pending 期取消)时回调。

        回调收完整 task dict(含 payload),供 pack 做资源回收(如在途防重
        表的释放)。回调异常只记日志,不影响任务收尾与其他监听者。
        """
        self._terminal_listeners.append(callback)

    def _notify_terminal(self, task_id: str) -> None:
        """通知终态监听者(锁外调用;任务收尾的最后一环)。"""
        if not self._terminal_listeners:
            return
        task = self._store.get_task(task_id)
        if task is None:
            return
        for cb in list(self._terminal_listeners):
            try:
                cb(task)
            except Exception:
                logger.exception(f"terminal listener failed for task {task_id}")

    # ── 提交与调度 ─────────────────────────────────────────

    def submit(
        self,
        task_type: str,
        payload: Optional[Dict[str, Any]] = None,
        title: str = "",
        pack_name: str = "",
        queue_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """提交任务:落库 pending → 入队 → 尝试调度。返回任务 dict。

        Raises:
            KeyError: 任务类型未注册(pack 被禁用/拼错名)。
        """
        if task_type not in self._handlers:
            raise KeyError(f"未注册的任务类型: {task_type}")
        task = self._store.create_task(
            task_type, pack_name=pack_name, title=title,
            payload=payload, queue_key=queue_key,
        )
        with self._lock:
            self._pending.append(task["id"])
            # 无 queue_key 的任务用专属键:永不与其他任务互斥
            self._task_keys[task["id"]] = queue_key or f"__solo__{task['id']}"
            self._dispatch_locked()
        return task

    def _dispatch_locked(self) -> None:
        """把等待队列头的任务尽可能派发给线程池(调用方须持锁)。

        派发条件:该任务的串行键未被占用 且 并发未满。同键任务天然
        按提交顺序串行(队头阻塞时,后面的同键任务也过不了"键未占用"检查)。
        """
        for task_id in list(self._pending):
            if self._running_count >= self._max_workers:
                break
            key = self._task_keys.get(task_id)
            if key is None or key in self._active_keys:
                continue
            self._pending.remove(task_id)
            self._active_keys.add(key)
            self._running_count += 1
            self._executor.submit(self._run, task_id, key)

    def _run(self, task_id: str, effective_key: str) -> None:
        """任务执行包装:状态迁移 + 异常兜底 + 收尾派发(SSE 终态事件在内)。

        整个方法体都在 try/finally 保护下——store 异常(SQLite busy/磁盘满)
        也必须走 _finish 释放串行键与并发额度,否则该键后续任务永久 pending、
        泄漏满 WORKERS 次后整个框架停摆。
        """
        from datetime import datetime, timezone
        try:
            task = self._store.get_task(task_id)
            handler = self._handlers.get(task["taskType"]) if task else None
            if task is None or handler is None:
                # 理论小概率:提交后 handler 被热切换清掉 → 按失败收尾
                self._store.update_task(
                    task_id, status="failed",
                    error="任务类型已不可用(插件被禁用?)",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return

            self._store.update_task(
                task_id, status="running", started_at=datetime.now(timezone.utc).isoformat()
            )
            self._publish(task_id, {"event": "status", "data": {"taskId": task_id, "status": "running"}})

            handle = TaskHandle(self, task_id, task.get("payload") or {})
            try:
                result = handler(handle)
                self._store.update_task(
                    task_id, status="succeeded",
                    result=result if isinstance(result, (dict, list)) else {"value": result},
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                self._publish(task_id, {"event": "status", "data": {"taskId": task_id, "status": "succeeded"}})
            except TaskCancelled:
                self._store.update_task(
                    task_id, status="cancelled",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                self._publish(task_id, {"event": "status", "data": {"taskId": task_id, "status": "cancelled"}})
            except Exception as e:
                logger.exception(f"task {task_id} ({task['taskType']}) failed")
                self._store.update_task(
                    task_id, status="failed", error=str(e),
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                self._store.append_log(task_id, level="error", message=f"任务失败: {e}")
                self._publish(task_id, {"event": "status", "data": {"taskId": task_id, "status": "failed", "error": str(e)}})
        finally:
            with self._lock:
                self._cancel_flags.discard(task_id)
            self._finish(task_id, effective_key)
            self._notify_terminal(task_id)

    def _finish(self, task_id: str, effective_key: str) -> None:
        """收尾:释放串行键/并发额度,派发下一个等待任务。"""
        with self._lock:
            self._active_keys.discard(effective_key)
            self._running_count = max(0, self._running_count - 1)
            self._task_keys.pop(task_id, None)
            self._dispatch_locked()

    # ── 取消 ───────────────────────────────────────────────

    def cancel(self, task_id: str) -> Optional[Dict[str, Any]]:
        """请求取消。pending → 直接 cancelled;running → 置协作标志。

        Returns:
            最新任务 dict;任务不存在返回 None。

        【锁纪律】_publish 内部会抢 self._lock(取订阅者快照),因此这里
        决不能在持锁状态下发布事件——先在锁内完成队列/标志变更,出锁后
        再落库 + 发布(否则自锁死锁,threading.Lock 不可重入)。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return None
        if task["status"] in FINAL_STATUSES:
            return task  # 已终态,幂等返回

        was_pending = False
        with self._lock:
            if task_id in self._pending:
                # 还没起跑:直接收尾为 cancelled
                self._pending.remove(task_id)
                self._task_keys.pop(task_id, None)
                was_pending = True
            else:
                # running:置标志,等 handler 到检查点自杀
                self._cancel_flags.add(task_id)

        if was_pending:
            from datetime import datetime, timezone
            self._store.update_task(
                task_id, status="cancelled",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            self._publish(task_id, {"event": "status", "data": {"taskId": task_id, "status": "cancelled"}})
            self._notify_terminal(task_id)
        else:
            # 竞态复核:读到非终态到拿锁之间,任务可能恰好跑完(_run 的 finally
            # 已清标志)。此时丢弃我们刚加的孤儿标志并原样返回——不覆盖终态
            # 任务的进度文案,也不在 _cancel_flags 里留永不清理的项。
            latest = self._store.get_task(task_id)
            if latest and latest["status"] in FINAL_STATUSES:
                with self._lock:
                    self._cancel_flags.discard(task_id)
                return latest
            self._store.update_task(task_id, progress_message="等待任务在检查点响应取消…")
        return self._store.get_task(task_id)

    def _is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancel_flags

    # ── SSE 订阅 ───────────────────────────────────────────

    def subscribe(self, task_id: str) -> tuple:
        """订阅任务事件流。Returns: (snapshot_task, snapshot_logs, q)。

        先注册监听者再拍快照,保证不丢事件;代价是日志可能重复
        (快照里一份、队列里一份),由 SSE 端按日志 id 去重。
        """
        q: "queue.Queue" = queue.Queue(maxsize=1000)
        with self._lock:
            self._listeners.setdefault(task_id, []).append(q)
        task = self._store.get_task(task_id)
        logs = self._store.list_logs(task_id, after_id=0, limit=500) if task else []
        return task, logs, q

    def unsubscribe(self, task_id: str, q: "queue.Queue") -> None:
        with self._lock:
            listeners = self._listeners.get(task_id)
            if listeners and q in listeners:
                listeners.remove(q)
            if listeners is not None and not listeners:
                self._listeners.pop(task_id, None)

    def _publish(self, task_id: str, item: Dict[str, Any]) -> None:
        """把事件投给该任务的全部订阅者(满队列丢帧——SSE 有日志游标兜底)。"""
        with self._lock:
            listeners = list(self._listeners.get(task_id) or [])
        for q in listeners:
            try:
                q.put_nowait(item)
            except queue.Full:
                logger.warning(f"task {task_id} 的 SSE 订阅队列满,丢一帧(可按日志游标补齐)")

    # ── 生命周期 ───────────────────────────────────────────

    def recover_on_startup(self) -> List[str]:
        """启动恢复:遗留 active 任务标记 interrupted。"""
        return self._store.mark_interrupted_on_startup()

    def close(self) -> None:
        """停机:请求取消全部 running/pending 任务后关闭线程池。

        线程池线程非 daemon(解释器退出会 join),长任务(如 LLM 导入)会把
        退出挂死到任务自然结束——所以先给所有在跑任务置取消标志,让它们在
        下一个检查点(批次边界)自行中止,线程随即归还;仍在途的单次 LLM
        HTTP 调用不受影响(最多等它返回)。没等到检查点的任务由下次启动的
        recover 标 interrupted,pack 幂等重跑承接。
        """
        try:
            tasks, _ = self._store.list_tasks(status="running", limit=1000)
            running_ids = [t["id"] for t in tasks]
        except Exception:
            logger.exception("停机时查询运行中任务失败(跳过取消标记)")
            running_ids = []
        if running_ids:
            with self._lock:
                for tid in running_ids:
                    self._cancel_flags.add(tid)
        self._executor.shutdown(wait=False)
