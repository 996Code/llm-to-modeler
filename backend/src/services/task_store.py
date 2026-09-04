"""TaskStore - 后台批量任务的 SQLite 持久化(tasks / task_logs 两张表)。

【模块定位】
通用任务框架的 DAO 层。平台只管"任务的通用生命周期与日志",任务做什么
(handler 逻辑)由 pack 注册——本模块与 pack_manager 一样零领域知识。

【两张表】
  tasks      —— 任务主体:类型/所属插件/状态机/进度/结果/错误/时间戳
  task_logs  —— 任务日志:追加写,供任务中心抽屉回放 + SSE 断线补齐

【status 状态机】
  pending → running → succeeded | failed | cancelled
  pending → cancelled                (还没起跑就被取消)
  running|pending → interrupted      (进程重启时的遗留任务,标记后可手动重提)

【Java 类比】
TaskStore ≈ @Repository;tasks 表 ≈ Spring Batch 的 JOB_EXECUTION,
task_logs ≈ STEP_EXECUTION 的日志表。同 ConversationStore 一样每次方法
新开连接 + WAL,不做跨方法事务。
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 终态集合(SSE 流据此判断"可以收尾断流")
FINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}

# 活动态集合(重启恢复时需要被打成 interrupted 的范围)
ACTIVE_STATUSES = {"pending", "running"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    """tasks 行 → 驼峰 dict(API 直接可用;payload/result 保持 JSON 对象)。"""
    d = dict(row)
    return {
        "id": d["id"],
        "taskType": d["task_type"],
        "packName": d["pack_name"] or "",
        "title": d["title"] or "",
        "status": d["status"],
        "progress": int(d["progress"] or 0),
        "progressMessage": d["progress_message"] or "",
        "queueKey": d["queue_key"] or "",
        "payload": json.loads(d["payload"]) if d.get("payload") else None,
        "result": json.loads(d["result"]) if d.get("result") else None,
        "error": d["error"] or "",
        "createdAt": d["created_at"],
        "startedAt": d.get("started_at"),
        "finishedAt": d.get("finished_at"),
    }


def _row_to_log(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    return {
        "id": int(d["id"]),
        "taskId": d["task_id"],
        "level": d["level"] or "info",
        "message": d["message"] or "",
        "data": json.loads(d["data"]) if d.get("data") else None,
        "createdAt": d["created_at"],
    }


class TaskStore:
    """tasks / task_logs 的 DAO(与 conversations.db 同库)。"""

    def __init__(self, db_path: str):
        from services.conversation_store import DEFAULT_DB_PATH
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"TaskStore initialized: {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """幂等建表。"""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,            -- 任务 ID(UUID)
                    task_type TEXT NOT NULL,        -- 类型(如 kg.import_document,pack 注册)
                    pack_name TEXT,                 -- 所属插件(任务中心过滤/展示)
                    title TEXT DEFAULT '',          -- 人读标题(列表展示)
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,     -- 0-100
                    progress_message TEXT DEFAULT '',
                    queue_key TEXT,                 -- 串行键:同 key 的任务 FIFO 串行
                    payload TEXT,                   -- 输入参数 JSON
                    result TEXT,                    -- 成功产物 JSON
                    error TEXT,                     -- 失败原因
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue_key, created_at);

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增:断线补齐的游标(after=?)
                    task_id TEXT NOT NULL,
                    level TEXT DEFAULT 'info',             -- info/warn/error
                    message TEXT DEFAULT '',
                    data TEXT,                              -- 结构化附加数据 JSON
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
            """)

    # ── 任务主体 ────────────────────────────────────────────

    def create_task(
        self,
        task_type: str,
        pack_name: str = "",
        title: str = "",
        payload: Optional[Dict[str, Any]] = None,
        queue_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建 pending 任务并返回任务 dict(submit 落库 + 调度入队用)。"""
        task_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, task_type, pack_name, title, status, progress, progress_message,
                    queue_key, payload, created_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, '', ?, ?, ?)""",
                (
                    task_id, task_type, pack_name, title,
                    queue_key,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    now,
                ),
            )
        return {
            "id": task_id, "taskType": task_type, "packName": pack_name,
            "title": title, "status": "pending", "progress": 0,
            "progressMessage": "", "queueKey": queue_key or "",
            "payload": payload, "result": None, "error": "",
            "createdAt": now, "startedAt": None, "finishedAt": None,
        }

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def update_task(self, task_id: str, **fields) -> bool:
        """更新任务的可变列(status/progress/progress_message/result/error/
        started_at/finished_at)。列名白名单防注入。"""
        allowed = {
            "status", "progress", "progress_message", "result",
            "error", "started_at", "finished_at",
        }
        cols, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"task column not updatable: {k}")
            if k in ("result",) and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            cols.append(f"{k} = ?")
            params.append(v)
        if not cols:
            return False
        params.append(task_id)
        with self._get_conn() as conn:
            cur = conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id = ?", params)
            return cur.rowcount > 0

    def list_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        pack_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """分页查询(任务中心列表;过滤条件拼装参数化防注入)。"""
        clauses, params = [], []
        if status:
            clauses.append("status = ?"); params.append(status)
        if task_type:
            clauses.append("task_type = ?"); params.append(task_type)
        if pack_name:
            clauses.append("pack_name = ?"); params.append(pack_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tasks {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_row_to_task(r) for r in rows], int(total)

    # ── 任务日志 ────────────────────────────────────────────

    def append_log(
        self,
        task_id: str,
        level: str = "info",
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """追加一条日志,返回 (自增 id, created_at)。

        id 是前端 SSE 断线补齐的游标;created_at 随事件一起发布,前端
        增量日志行的时间列不再依赖轮询补齐。
        """
        now = _now()
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO task_logs (task_id, level, message, data, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    task_id, level, message,
                    json.dumps(data, ensure_ascii=False) if data else None,
                    now,
                ),
            )
            return int(cur.lastrowid), now

    def list_logs(self, task_id: str, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        """读日志(id > after_id,升序)——订阅时回放 + 断线重连补齐共用。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM task_logs WHERE task_id = ? AND id > ?
                   ORDER BY id ASC LIMIT ?""",
                (task_id, after_id, limit),
            ).fetchall()
        return [_row_to_log(r) for r in rows]

    # ── 重启恢复 ────────────────────────────────────────────

    def mark_interrupted_on_startup(self) -> List[str]:
        """把上次进程遗留的 pending/running 任务标记为 interrupted。

        第一期不自动续跑(handler 的进程内状态已丢失,续跑语义交由各 pack
        用"幂等重跑"实现——如知识图谱导入按 chunk checkpoint 跳过已完成块)。
        返回被打断的任务 id 列表(日志可见)。
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('pending', 'running')",
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    "UPDATE tasks SET status = 'interrupted',"
                    " error = COALESCE(NULLIF(error, ''), '服务重启,任务被中断'),"
                    " finished_at = ? WHERE status IN ('pending', 'running')",
                    (_now(),),
                )
        if ids:
            logger.warning(f"启动恢复: {len(ids)} 个遗留任务标记为 interrupted")
        return ids

    def delete_logs(self, task_id: str) -> None:
        """删除任务日志(测试清理用)。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
