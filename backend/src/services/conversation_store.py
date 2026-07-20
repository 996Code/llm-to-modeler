"""ConversationStore - SQLite 持久化(append-only 事件流)。

阶段 4 重建:
- 旧 conversations/messages 表 RENAME 为 _legacy_ 留档(不迁移数据)
- 新建 events 表(append-only,含 kind 列)
- 新建 session_meta 表(列表查询,不 JOIN events)

events.kind 取值:
- user: 用户输入
- assistant: 助手回复
- tool_result: 工具产出(summary 标准化后)
- compacted: 压缩点标记
- compact_trace: 压缩轨迹(审计用)
- checkpoint: artifact 快照
- ask: pending_ask 现场

保留旧 API(create_conversation/list_conversations/get_conversation/add_message 等)
以兼容现有 api/conversations.py 和 api/config.py,但内部改用 events 表。
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    """获取当前 UTC 时间 ISO 字符串(使用 timezone-aware)。"""
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    """SQLite-backed conversation storage (append-only event stream)."""

    def __init__(self, db_path: str = "data/conversations.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"ConversationStore initialized: {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """初始化数据库:迁移旧表 + 创建新表。"""
        with self._get_conn() as conn:
            # 1. 检测旧表,RENAME 为 _legacy_ 留档(不导入数据)
            self._migrate_legacy_tables(conn)

            # 2. 创建新表(append-only 事件流)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conv_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_kind ON events(conv_id, kind);

                CREATE TABLE IF NOT EXISTS session_meta (
                    conv_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    current_config TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_meta_user ON session_meta(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS call_logs (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT,
                    call_type TEXT NOT NULL,  -- 'llm' or 'upstream'
                    endpoint TEXT NOT NULL,
                    request_data TEXT,
                    response_data TEXT,
                    status_code INTEGER,
                    duration_ms INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_call_logs_conv ON call_logs(conv_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_call_logs_type ON call_logs(call_type, created_at);
            """)

    def _migrate_legacy_tables(self, conn: sqlite3.Connection):
        """旧表 RENAME 为 _legacy_ 留档,不导入数据。"""
        # 检查旧 conversations 表是否存在
        try:
            conn.execute("SELECT 1 FROM conversations LIMIT 1")
            has_legacy_conv = True
        except sqlite3.OperationalError:
            has_legacy_conv = False

        if has_legacy_conv:
            logger.info("Migrating legacy tables: RENAME to _legacy_* (no data import)")
            try:
                conn.execute("ALTER TABLE messages RENAME TO _legacy_messages")
            except sqlite3.OperationalError:
                pass  # 已迁移
            try:
                conn.execute("ALTER TABLE conversations RENAME TO _legacy_conversations")
            except sqlite3.OperationalError:
                pass
            logger.info("Legacy tables renamed: _legacy_conversations, _legacy_messages")

    # ── Conversations(session_meta 表) ─────────────────────────

    def create_conversation(
        self,
        user_id: str,
        title: str = "",
    ) -> Dict[str, Any]:
        """Create a new conversation."""
        conv_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO session_meta (conv_id, user_id, title, summary, created_at, updated_at) VALUES (?, ?, ?, '', ?, ?)",
                (conv_id, user_id, title, now, now),
            )
            # 同时写一条 events(kind=checkpoint)记录会话创建
            self._append_event(conn, conv_id, "checkpoint", {"action": "created"})
        return {"id": conv_id, "userId": user_id, "title": title, "createdAt": now}

    def list_conversations(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """List conversations for a user, newest first. 只查 session_meta。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_meta WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            current_config = json.loads(item["current_config"]) if item.get("current_config") else None
            result.append({
                "id": item["conv_id"],
                "title": item["title"] or "新对话",
                "currentConfig": current_config,
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            })
        return result

    def list_all_conversations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all conversations for admin, newest first. 包含 user_id 字段。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM session_meta ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            current_config = json.loads(item["current_config"]) if item.get("current_config") else None
            result.append({
                "id": item["conv_id"],
                "userId": item["user_id"],
                "title": item["title"] or "新对话",
                "currentConfig": current_config,
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            })
        return result

    def get_conversation(self, conv_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a conversation with all messages. Validates user ownership."""
        return self._get_conversation(conv_id, user_id)

    def get_conversation_any_user(self, conv_id: str) -> Optional[Dict[str, Any]]:
        """Get a conversation with all messages. No user check (for admin)."""
        return self._get_conversation(conv_id, None)

    def _get_conversation(self, conv_id: str, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Internal: get conversation with optional user check."""
        with self._get_conn() as conn:
            if user_id:
                meta = conn.execute(
                    "SELECT * FROM session_meta WHERE conv_id = ? AND user_id = ?",
                    (conv_id, user_id),
                ).fetchone()
            else:
                meta = conn.execute(
                    "SELECT * FROM session_meta WHERE conv_id = ?",
                    (conv_id,),
                ).fetchone()
            if not meta:
                return None

            # 从 events 表重建 messages(user/assistant/tool_result)
            event_rows = conn.execute(
                """SELECT * FROM events WHERE conv_id = ? AND kind IN ('user', 'assistant', 'tool_result')
                   ORDER BY created_at ASC""",
                (conv_id,),
            ).fetchall()

        meta = dict(meta)
        current_config = json.loads(meta["current_config"]) if meta.get("current_config") else None
        messages = []
        for r in event_rows:
            r = dict(r)
            payload = json.loads(r["payload"])
            messages.append({
                "id": r["id"],
                "role": payload.get("role", r["kind"]),
                "content": payload.get("content", ""),
                "configSnapshot": payload.get("config_snapshot"),
                "createdAt": r["created_at"],
            })

        return {
            "id": meta["conv_id"],
            "title": meta["title"] or "新对话",
            "currentConfig": current_config,
            "messages": messages,
            "createdAt": meta["created_at"],
            "updatedAt": meta["updated_at"],
        }

    def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        """Delete a conversation. Validates user ownership."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM session_meta WHERE conv_id = ? AND user_id = ?",
                (conv_id, user_id),
            )
            # events 也删除(级联)
            conn.execute("DELETE FROM events WHERE conv_id = ?", (conv_id,))
            return cursor.rowcount > 0

    def update_conversation_config(
        self,
        conv_id: str,
        config: Dict[str, Any],
        title: Optional[str] = None,
    ):
        """Update conversation's current config and optionally title."""
        now = _now()
        config_json = json.dumps(config, ensure_ascii=False)
        with self._get_conn() as conn:
            if title:
                conn.execute(
                    "UPDATE session_meta SET current_config = ?, title = ?, updated_at = ? WHERE conv_id = ?",
                    (config_json, title, now, conv_id),
                )
            else:
                conn.execute(
                    "UPDATE session_meta SET current_config = ?, updated_at = ? WHERE conv_id = ?",
                    (config_json, now, conv_id),
                )

    def touch_conversation(self, conv_id: str):
        """Update the conversation's updated_at timestamp."""
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (now, conv_id),
            )

    # ── Messages(events 表,append-only) ───────────────────────

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a message to a conversation. 只 INSERT,不 UPDATE(append-only)。"""
        msg_id = str(uuid.uuid4())
        now = _now()

        # kind 映射:role -> event kind
        kind = "user" if role == "user" else "assistant"
        payload = {
            "role": role,
            "content": content,
            "config_snapshot": config_snapshot,
        }

        with self._get_conn() as conn:
            self._append_event(conn, conv_id, kind, payload, msg_id, now)
            # 更新 session_meta 的 updated_at
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (now, conv_id),
            )

        return {
            "id": msg_id,
            "role": role,
            "content": content,
            "configSnapshot": config_snapshot,
            "createdAt": now,
        }

    def get_messages(self, conv_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a conversation. 从 events 表重建。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM events WHERE conv_id = ? AND kind IN ('user', 'assistant')
                   ORDER BY created_at ASC""",
                (conv_id,),
            ).fetchall()

        result = []
        for r in rows:
            r = dict(r)
            payload = json.loads(r["payload"])
            result.append({
                "id": r["id"],
                "role": payload.get("role", r["kind"]),
                "content": payload.get("content", ""),
                "configSnapshot": payload.get("config_snapshot"),
                "createdAt": r["created_at"],
            })
        return result

    # ── Call Logs (LLM/Upstream 调用日志) ─────────────────────

    def save_call_log(
        self,
        call_type: str,  # 'llm' or 'upstream'
        endpoint: str,
        request_data: Optional[Dict] = None,
        response_data: Optional[Dict] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        conv_id: Optional[str] = None,
    ) -> str:
        """保存一次 LLM 或上游服务调用日志。"""
        log_id = str(uuid.uuid4())
        now = _now()

        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO call_logs 
                   (id, conv_id, call_type, endpoint, request_data, response_data, 
                    status_code, duration_ms, error_message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_id,
                    conv_id,
                    call_type,
                    endpoint,
                    json.dumps(request_data, ensure_ascii=False) if request_data else None,
                    json.dumps(response_data, ensure_ascii=False) if response_data else None,
                    status_code,
                    duration_ms,
                    error_message,
                    now,
                ),
            )

        return log_id

    def get_call_logs(
        self, 
        conv_id: Optional[str] = None,
        call_type: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """查询调用日志。"""
        with self._get_conn() as conn:
            if conv_id and call_type:
                rows = conn.execute(
                    "SELECT * FROM call_logs WHERE conv_id = ? AND call_type = ? ORDER BY created_at DESC LIMIT ?",
                    (conv_id, call_type, limit),
                ).fetchall()
            elif conv_id:
                rows = conn.execute(
                    "SELECT * FROM call_logs WHERE conv_id = ? ORDER BY created_at DESC LIMIT ?",
                    (conv_id, limit),
                ).fetchall()
            elif call_type:
                rows = conn.execute(
                    "SELECT * FROM call_logs WHERE call_type = ? ORDER BY created_at DESC LIMIT ?",
                    (call_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM call_logs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            if item.get("request_data"):
                item["request_data"] = json.loads(item["request_data"])
            if item.get("response_data"):
                item["response_data"] = json.loads(item["response_data"])
            result.append(item)
        return result

    # ── Append-only 事件流 API(新) ─────────────────────────────

    def _append_event(
        self,
        conn: sqlite3.Connection,
        conv_id: str,
        kind: str,
        payload: Dict[str, Any],
        event_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> str:
        """内部方法:追加一条事件(只 INSERT)。"""
        event_id = event_id or str(uuid.uuid4())
        created_at = created_at or _now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        conn.execute(
            "INSERT INTO events (id, conv_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, conv_id, kind, payload_json, created_at),
        )
        return event_id

    def append_event(
        self,
        conv_id: str,
        kind: str,
        payload: Dict[str, Any],
    ) -> str:
        """公开方法:追加一条事件(append-only)。

        kind ∈ {user, assistant, tool_result, compacted, compact_trace, checkpoint, ask}
        """
        with self._get_conn() as conn:
            event_id = self._append_event(conn, conv_id, kind, payload)
            conn.execute(
                "UPDATE session_meta SET updated_at = ? WHERE conv_id = ?",
                (_now(), conv_id),
            )
        return event_id

    def load_events(
        self,
        conv_id: str,
        kinds: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """加载事件(按 kind 过滤,按时间排序)。"""
        with self._get_conn() as conn:
            if kinds:
                placeholders = ",".join("?" * len(kinds))
                rows = conn.execute(
                    f"SELECT * FROM events WHERE conv_id = ? AND kind IN ({placeholders}) ORDER BY created_at ASC",
                    (conv_id, *kinds),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE conv_id = ? ORDER BY created_at ASC",
                    (conv_id,),
                ).fetchall()

        result = []
        for r in rows:
            r = dict(r)
            result.append({
                "id": r["id"],
                "conv_id": r["conv_id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            })
        return result

    def load_pending_ask(self, conv_id: str) -> Optional[Dict[str, Any]]:
        """加载 pending_ask(最近一条 kind=ask 事件)。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE conv_id = ? AND kind = 'ask' ORDER BY created_at DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
        if not row:
            return None
        r = dict(row)
        return {
            "id": r["id"],
            "payload": json.loads(r["payload"]),
            "created_at": r["created_at"],
        }

    def clear_pending_ask(self, conv_id: str) -> None:
        """清除 pending_ask(删除 kind=ask 事件)。

        注:append-only 原则上不删除,但 pending_ask 是临时状态,
        清除后写一条 kind=checkpoint 标记"追问已解决"。
        """
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM events WHERE conv_id = ? AND kind = 'ask'",
                (conv_id,),
            )
            self._append_event(conn, conv_id, "checkpoint", {"action": "ask_resolved"})
