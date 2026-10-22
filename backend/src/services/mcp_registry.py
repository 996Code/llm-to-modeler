"""mcp_servers 注册表——外部 MCP server 的平台级登记处。

表结构=设计文档 §3.3 最小集;明文存储(内网+admin 门控,不加密)。
探测(网络 IO)在 DB 连接之外:save 先落库(pending)→ 事务外 list_remote_tools
→ 短事务回写快照+status(Dify 三段式,防慢连接占池)。
"""
import json
import logging
import re
import threading
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
_DDL = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    code          TEXT NOT NULL UNIQUE,
    url           TEXT NOT NULL,
    headers_json  TEXT NOT NULL DEFAULT '{}',
    timeout       REAL NOT NULL DEFAULT 30,
    read_timeout  REAL NOT NULL DEFAULT 300,
    status        TEXT NOT NULL DEFAULT 'pending',
    tools_json    TEXT NOT NULL DEFAULT '[]',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
"""
# 既有表补列(幂等;CREATE TABLE IF NOT EXISTS 不改已存在表)
_MIGRATE = "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS enabled INTEGER NOT NULL DEFAULT 1"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class McpServerStore:
    """mcp_servers 表 DAO(照抄 PlatformSettingsStore:每方法新连接+写锁)。"""

    def __init__(self, database_url: Optional[str] = None):
        from services.db import get_pg_engine
        self.db = get_pg_engine(database_url)
        self._lock = threading.Lock()
        with self.db.connect() as conn:
            conn.execute(_DDL)
            conn.execute(_MIGRATE)

    def _row(self, r) -> Dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "code": r["code"], "url": r["url"],
                "headers": json.loads(r["headers_json"] or "{}"),
                "timeout": r["timeout"], "read_timeout": r["read_timeout"],
                "status": r["status"],
                "enabled": bool(r["enabled"]),
                "tools_snapshot": json.loads(r["tools_json"] or "[]"),
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def list_servers(self) -> List[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_servers ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def get_by_code(self, code: str) -> Optional[dict]:
        with self.db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM mcp_servers WHERE code = ?", (code,)).fetchone()
        return self._row(r) if r else None

    def create_server(self, name: str, code: str, url: str, headers: dict,
                      timeout: float = 30.0, read_timeout: float = 300.0) -> dict:
        if not _CODE_RE.fullmatch(code or ""):
            raise ValueError("code 只允许小写字母/数字/-/_，1-24 位")
        sid, now = uuid.uuid4().hex, _now()
        try:
            with self._lock, self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO mcp_servers (id,name,code,url,headers_json,"
                    "timeout,read_timeout,status,tools_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,'pending','[]',?,?)",
                    (sid, name, code, url, json.dumps(headers or {}), timeout,
                     read_timeout, now, now))
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(f"code 已存在: {code}")
            raise
        return self.apply_probe(sid)

    def update_server(self, sid: str, **fields: Any) -> dict:
        with self._lock, self.db.connect() as conn:
            cur = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
            if not cur:
                raise ValueError("server 不存在")
            f = {"name": fields.get("name", cur["name"]),
                 "url": fields.get("url", cur["url"]),
                 "headers_json": json.dumps(
                     fields.get("headers", json.loads(cur["headers_json"] or "{}"))),
                 "timeout": fields.get("timeout", cur["timeout"]),
                 "read_timeout": fields.get("read_timeout", cur["read_timeout"])}
            conn.execute(
                "UPDATE mcp_servers SET name=?,url=?,headers_json=?,timeout=?,"
                "read_timeout=?,updated_at=? WHERE id=?",
                (f["name"], f["url"], f["headers_json"], f["timeout"],
                 f["read_timeout"], _now(), sid))
        return self.apply_probe(sid)

    def delete_server(self, sid: str) -> None:
        with self._lock, self.db.connect() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = ?", (sid,))

    def set_enabled(self, sid: str, enabled: bool) -> dict:
        """启用/停用(用户意图,与探测态 status 正交;停用后调用层 fail-fast)。"""
        with self._lock, self.db.connect() as conn:
            cur = conn.execute(
                "SELECT id FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
            if not cur:
                raise ValueError("server 不存在")
            conn.execute(
                "UPDATE mcp_servers SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, _now(), sid))
            cur = conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        return self._row(cur)

    def apply_probe(self, sid: str) -> dict:
        """事务外探测回写:成功 connected+快照;失败保持 pending(不抛,如实返回)。"""
        with self.db.connect() as conn:
            cur = conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        if not cur:
            raise ValueError("server 不存在")
        cfg = {"url": cur["url"], "headers": json.loads(cur["headers_json"] or "{}"),
               "timeout": cur["timeout"], "read_timeout": cur["read_timeout"]}
        status, snapshot = "pending", []
        try:
            from sdk.mcp_client import list_remote_tools
            snapshot = list_remote_tools(cfg)
            status = "connected"
        except Exception as e:
            logger.warning(f"mcp probe failed({cur['code']}): {e}")
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE mcp_servers SET status=?,tools_json=?,updated_at=? WHERE id=?",
                (status, json.dumps(snapshot, ensure_ascii=False), _now(), sid))
            cur = conn.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        return self._row(cur)
