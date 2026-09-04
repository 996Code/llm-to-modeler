"""KGStore - 知识图谱插件的 SQLite 元数据层(kg_ 前缀三表)。

【表职责】
  kg_knowledge_bases  知识库本体:名称/描述/schema_json(类型体系)/
                      embedding 模型与维度/vector_enabled/状态
  kg_documents        文档:原始文件信息/内容指纹(content_hash)/
                      导入状态机/计数统计(chunk/实体/关系)
  kg_chunks           切块:文本与状态(status 兼作 chunk 级 checkpoint,
                      增量重跑跳过 done 块)

【import_status 状态机】
  uploaded → importing → succeeded | partial | failed
  (uploaded = 已上传未导入;partial = 部分块失败但整体可用;幂等重导
   从 content_hash 判定"内容没变且已成功 → 跳过")

【与平台的关系】同库不同表(ConversationStore 的 conversations.db),
沿用"每方法新连接 + WAL"模式,平台零感知。
"""
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KGStore:
    """kg_* 三表的 DAO。"""

    def __init__(self, db_path: str):
        from services.conversation_store import DEFAULT_DB_PATH
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"KGStore initialized: {self.db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kg_knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    schema_json TEXT,               -- 本体:entity_types/relation_types/schema_mode/pending_types
                    schema_template TEXT DEFAULT '',-- 建库所选模板 key(溯源)
                    embedding_model TEXT DEFAULT '',
                    vector_dim INTEGER,
                    vector_enabled INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kg_documents (
                    id TEXT PRIMARY KEY,
                    kb_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT DEFAULT '',
                    size_bytes INTEGER DEFAULT 0,
                    file_path TEXT DEFAULT '',
                    content_hash TEXT DEFAULT '',
                    import_status TEXT DEFAULT 'uploaded',
                    chunk_count INTEGER DEFAULT 0,
                    entity_count INTEGER DEFAULT 0,
                    relation_count INTEGER DEFAULT 0,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_docs_kb ON kg_documents(kb_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS kg_chunks (
                    id TEXT PRIMARY KEY,
                    kb_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',  -- pending/done/failed(chunk 级 checkpoint)
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_chunks_doc ON kg_chunks(doc_id, seq);
                CREATE INDEX IF NOT EXISTS idx_kg_chunks_status ON kg_chunks(doc_id, status);
            """)

    # ── 知识库 ─────────────────────────────────────────────

    def create_kb(
        self,
        name: str,
        description: str = "",
        schema_json: Optional[Dict] = None,
        schema_template: str = "",
    ) -> Dict[str, Any]:
        kb_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO kg_knowledge_bases
                   (id, name, description, schema_json, schema_template,
                    vector_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (kb_id, name, description,
                 json.dumps(schema_json, ensure_ascii=False) if schema_json else None,
                 schema_template, now, now),
            )
        return self.get_kb(kb_id)

    def get_kb(self, kb_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_knowledge_bases WHERE id = ?", (kb_id,)
            ).fetchone()
        return self._kb_row(row) if row else None

    def get_kb_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_knowledge_bases WHERE name = ?", (name,)
            ).fetchone()
        return self._kb_row(row) if row else None

    def list_kbs(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT b.*,
                          (SELECT COUNT(*) FROM kg_documents d WHERE d.kb_id = b.id) AS doc_count,
                          (SELECT COALESCE(SUM(d.entity_count), 0) FROM kg_documents d
                            WHERE d.kb_id = b.id) AS entity_total,
                          (SELECT COALESCE(SUM(d.relation_count), 0) FROM kg_documents d
                            WHERE d.kb_id = b.id) AS relation_total
                     FROM kg_knowledge_bases b
                    ORDER BY b.created_at DESC"""
            ).fetchall()
        result = []
        for r in rows:
            kb = self._kb_row(r)
            kb["docCount"] = int(r["doc_count"])
            kb["entityTotal"] = int(r["entity_total"])
            kb["relationTotal"] = int(r["relation_total"])
            result.append(kb)
        return result

    def update_kb(
        self, kb_id: str, name: Optional[str] = None, description: Optional[str] = None,
        schema_json: Optional[Dict] = None,
    ) -> bool:
        sets, params = ["updated_at = ?"], [_now()]
        if name is not None:
            sets.append("name = ?"); params.append(name)
        if description is not None:
            sets.append("description = ?"); params.append(description)
        if schema_json is not None:
            sets.append("schema_json = ?")
            params.append(json.dumps(schema_json, ensure_ascii=False))
        params.append(kb_id)
        with self._get_conn() as conn:
            cur = conn.execute(
                f"UPDATE kg_knowledge_bases SET {', '.join(sets)} WHERE id = ?", params)
            return cur.rowcount > 0

    def set_kb_vector_info(
        self, kb_id: str, embedding_model: str, vector_dim: int, vector_enabled: bool,
    ) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                """UPDATE kg_knowledge_bases
                   SET embedding_model = ?, vector_dim = ?, vector_enabled = ?, updated_at = ?
                   WHERE id = ?""",
                (embedding_model, vector_dim, 1 if vector_enabled else 0, _now(), kb_id),
            )
            return cur.rowcount > 0

    def delete_kb(self, kb_id: str) -> bool:
        """级联删元数据(图/向量由调用方清理)。"""
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM kg_knowledge_bases WHERE id = ?", (kb_id,))
            conn.execute("DELETE FROM kg_chunks WHERE kb_id = ?", (kb_id,))
            conn.execute("DELETE FROM kg_documents WHERE kb_id = ?", (kb_id,))
            return cur.rowcount > 0

    @staticmethod
    def _kb_row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        schema = None
        if d.get("schema_json"):
            try:
                schema = json.loads(d["schema_json"])
            except (ValueError, TypeError):
                schema = None
        return {
            "id": d["id"], "name": d["name"], "description": d.get("description") or "",
            "schema": schema, "schemaTemplate": d.get("schema_template") or "",
            "embeddingModel": d.get("embedding_model") or "",
            "vectorDim": d.get("vector_dim"),
            "vectorEnabled": bool(d.get("vector_enabled")),
            "status": d.get("status") or "active",
            "createdAt": d["created_at"], "updatedAt": d["updated_at"],
        }

    # ── 文档 ───────────────────────────────────────────────

    def create_document(
        self, kb_id: str, filename: str, mime_type: str, size_bytes: int,
        file_path: str, content_hash: str,
    ) -> Dict[str, Any]:
        doc_id = str(uuid.uuid4())
        now = _now()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO kg_documents
                   (id, kb_id, filename, mime_type, size_bytes, file_path,
                    content_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, kb_id, filename, mime_type, size_bytes, file_path,
                 content_hash, now, now),
            )
        return self.get_document(doc_id)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return self._doc_row(row) if row else None

    def get_document_by_hash(self, kb_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
        """同库同内容文件查重(增量判定辅助)。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kg_documents WHERE kb_id = ? AND content_hash = ?",
                (kb_id, content_hash),
            ).fetchone()
        return self._doc_row(row) if row else None

    def list_documents(self, kb_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM kg_documents WHERE kb_id = ? ORDER BY created_at DESC",
                (kb_id,),
            ).fetchall()
        return [self._doc_row(r) for r in rows]

    def update_document(self, doc_id: str, **fields) -> bool:
        allowed = {
            "import_status", "chunk_count", "entity_count", "relation_count",
            "error", "content_hash", "filename", "file_path",
        }
        sets, params = ["updated_at = ?"], [_now()]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"kg_documents column not updatable: {k}")
            sets.append(f"{k} = ?"); params.append(v)
        params.append(doc_id)
        with self._get_conn() as conn:
            cur = conn.execute(
                f"UPDATE kg_documents SET {', '.join(sets)} WHERE id = ?", params)
            return cur.rowcount > 0

    def delete_document(self, doc_id: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM kg_documents WHERE id = ?", (doc_id,))
            conn.execute("DELETE FROM kg_chunks WHERE doc_id = ?", (doc_id,))
            return cur.rowcount > 0

    def recover_importing_docs(self, active_doc_ids) -> int:
        """把没有存活任务支撑的 importing 文档收敛为 failed(启动恢复用)。

        active_doc_ids = 当前确实 pending/running 的导入任务对应的 doc_id
        (热切换重装配时活任务不能被误伤)。Returns: 收敛的行数。
        """
        with self._get_conn() as conn:
            cur = conn.execute(
                """UPDATE kg_documents
                   SET import_status = 'failed',
                       error = '服务重启导致导入中断(可重新发起,已完成块会续跑)',
                       updated_at = ?
                   WHERE import_status = 'importing'""",
                (_now(),),
            )
            # 排除活任务:先整体收敛再放回 importing(集合小,两步比拼 NOT IN 简单)
            for doc_id in active_doc_ids:
                conn.execute(
                    """UPDATE kg_documents SET import_status = 'importing', error = '',
                           updated_at = ? WHERE id = ? AND import_status = 'failed'
                           AND error LIKE '服务重启导致导入中断%'""",
                    (_now(), doc_id),
                )
            return cur.rowcount

    def document_file_path(self, doc_id: str) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT file_path FROM kg_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return row["file_path"] if row else None

    @staticmethod
    def _doc_row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        return {
            "id": d["id"], "kbId": d["kb_id"], "filename": d["filename"],
            "mimeType": d.get("mime_type") or "", "sizeBytes": d.get("size_bytes") or 0,
            "filePath": d.get("file_path") or "",
            "contentHash": d.get("content_hash") or "",
            "importStatus": d.get("import_status") or "uploaded",
            "chunkCount": d.get("chunk_count") or 0,
            "entityCount": d.get("entity_count") or 0,
            "relationCount": d.get("relation_count") or 0,
            "error": d.get("error") or "",
            "createdAt": d["created_at"], "updatedAt": d["updated_at"],
        }

    # ── 切块(兼 chunk 级 checkpoint) ───────────────────────

    def replace_chunks(
        self, doc_id: str, kb_id: str, chunks: List[Dict[str, Any]],
    ) -> int:
        """重建文档的切块(重导入路径:先删后插,seq 从 0 连续)。"""
        now = _now()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM kg_chunks WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                """INSERT INTO kg_chunks (id, kb_id, doc_id, seq, text, char_count, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                [(str(uuid.uuid4()), kb_id, doc_id, c["seq"], c["text"],
                  c.get("char_count", len(c["text"])), now) for c in chunks],
            )
            conn.execute(
                "UPDATE kg_documents SET chunk_count = ?, updated_at = ? WHERE id = ?",
                (len(chunks), now, doc_id),
            )
        return len(chunks)

    def list_chunks(self, doc_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM kg_chunks WHERE doc_id = ?"
        params: list = [doc_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY seq ASC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": r["id"], "docId": r["doc_id"], "kbId": r["kb_id"],
            "seq": int(r["seq"]), "text": r["text"],
            "charCount": int(r["char_count"] or 0),
            "status": r["status"],
        } for r in rows]

    def mark_chunk(self, chunk_id: str, status: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE kg_chunks SET status = ? WHERE id = ?", (status, chunk_id))
            return cur.rowcount > 0
