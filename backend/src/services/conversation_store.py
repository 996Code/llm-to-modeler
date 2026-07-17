"""
Conversation Store — SQLite persistence for chat history.

Tables:
  conversations (id, user_id, title, current_config, created_at, updated_at)
  messages (id, conversation_id, role, content, config_snapshot, created_at)

No login — user_id is passed from upstream system via headers.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationStore:
    """SQLite-backed conversation and message storage."""

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
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    current_config TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    config_snapshot TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);
            """)

    # ── Conversations ──────────────────────────────────────────

    def create_conversation(
        self,
        user_id: str,
        title: str = "",
    ) -> Dict[str, Any]:
        """Create a new conversation."""
        conv_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conv_id, user_id, title, now, now),
            )
        return {"id": conv_id, "userId": user_id, "title": title, "createdAt": now}

    def list_conversations(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """List conversations for a user, newest first."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            item["currentConfig"] = json.loads(item.pop("current_config")) if item.get("current_config") else None
            result.append({
                "id": item["id"],
                "title": item["title"] or "新对话",
                "currentConfig": item.get("currentConfig"),
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            })
        return result

    def get_conversation(self, conv_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a conversation with all messages. Validates user ownership."""
        with self._get_conn() as conn:
            conv = conn.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()

            if not conv:
                return None

            msg_rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()

        conv = dict(conv)
        messages = []
        for m in msg_rows:
            m = dict(m)
            messages.append({
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "configSnapshot": json.loads(m["config_snapshot"]) if m.get("config_snapshot") else None,
                "createdAt": m["created_at"],
            })

        return {
            "id": conv["id"],
            "title": conv["title"] or "新对话",
            "currentConfig": json.loads(conv["current_config"]) if conv.get("current_config") else None,
            "messages": messages,
            "createdAt": conv["created_at"],
            "updatedAt": conv["updated_at"],
        }

    def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        """Delete a conversation. Validates user ownership."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            )
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            return cursor.rowcount > 0

    def update_conversation_config(
        self,
        conv_id: str,
        config: Dict[str, Any],
        title: Optional[str] = None,
    ):
        """Update conversation's current config and optionally title."""
        now = datetime.utcnow().isoformat()
        config_json = json.dumps(config, ensure_ascii=False)
        with self._get_conn() as conn:
            if title:
                conn.execute(
                    "UPDATE conversations SET current_config = ?, title = ?, updated_at = ? WHERE id = ?",
                    (config_json, title, now, conv_id),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET current_config = ?, updated_at = ? WHERE id = ?",
                    (config_json, now, conv_id),
                )

    def touch_conversation(self, conv_id: str):
        """Update the conversation's updated_at timestamp."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )

    # ── Messages ───────────────────────────────────────────────

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add a message to a conversation."""
        msg_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        config_json = json.dumps(config_snapshot, ensure_ascii=False) if config_snapshot else None

        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO messages (id, conversation_id, role, content, config_snapshot, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (msg_id, conv_id, role, content, config_json, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
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
        """Get all messages for a conversation."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()

        result = []
        for r in rows:
            r = dict(r)
            result.append({
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "configSnapshot": json.loads(r["config_snapshot"]) if r.get("config_snapshot") else None,
                "createdAt": r["created_at"],
            })
        return result
