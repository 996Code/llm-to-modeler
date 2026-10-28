"""metrology_assistant 场景配置与对话 flow 存储(设计 §8.1/§8.3/§6)。

存储边界:
  assistant_config   场景配置的唯一事实源,整档直存,revision 用于缓存失效
  assistant_config_history  保留已有数据,不追加快照
  assistant_flows    跨轮对话权威状态(场景/条件/flow_revision/pending
                     追问;CAS 推进防迟到覆盖)
"""
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── DDL(全部幂等;schema 由 PackRelationalDB.migrate_locked 创建)──

METROLOGY_DDL = [
    # 场景配置:单行权威档(scope='global'),revision 单调递增。
    # content 存完整配置 JSON(scenarios/labels/welcome 等);
    """CREATE TABLE IF NOT EXISTS assistant_config (
        scope TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        content TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS assistant_config_history (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        revision INTEGER NOT NULL,
        content TEXT NOT NULL,
        updated_by TEXT,
        updated_at TEXT NOT NULL,
        comment TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_asst_cfg_hist "
    "ON assistant_config_history(scope, revision)",
    # 对话 flow:每会话至多一个进行中(设计 §6"每会话最多一个进行中任务");
    # flow_revision 是 CAS 令牌,任何推进(补答消费/条件变更/结果写回)
    # 都带 expected_revision,迟到写被拒。
    """CREATE TABLE IF NOT EXISTS assistant_flows (
        flow_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        user_id TEXT NOT NULL DEFAULT '',
        scenario TEXT NOT NULL DEFAULT '',
        slots TEXT NOT NULL DEFAULT '{}',
        flow_revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'running',
        pending_question TEXT,
        result_ref TEXT,
        config_revision INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_asst_flow_conv "
    "ON assistant_flows(conversation_id, status)",
]

CONFIG_SCOPE = "global"
_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
# 失效传播(设计 §6"单号变清进度费用"):条件变化把在途 flow 置
# cancelled——旧结果引用随行作废,恢复链路不再引用过期结论
_STATUS_CANCELLED = "cancelled"


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec='microseconds')


class FlowRevisionError(Exception):
    """flow CAS 冲突(迟到写/并发推进)。"""


# ── 场景配置 ─────────────────────────────────────────────────

def load_config(db) -> Optional[Dict[str, Any]]:
    """读当前生效配置;无行返回 None(调用方决定是否种子)。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT revision, content FROM assistant_config "
            "WHERE scope = ?", (CONFIG_SCOPE,)).fetchone()
    if not row:
        return None
    try:
        content = json.loads(row["content"])
    except (TypeError, ValueError):
        logger.exception("assistant_config content 损坏(scope=%s)", CONFIG_SCOPE)
        return None
    content["_revision"] = int(row["revision"])
    return content


def current_revision(db) -> int:
    """当前 revision(无行=0;配置缓存比对用)。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT revision FROM assistant_config WHERE scope = ?",
            (CONFIG_SCOPE,)).fetchone()
    return int(row["revision"]) if row else 0


def seed_config_if_absent(db, default: Dict[str, Any],
                          updated_by: str = "seed") -> int:
    """首启种子:无配置行时写入出厂默认(YAML 常量转档)。

    已有配置(含被清表后重建的测试库)不覆盖——DB 是唯一事实源,
    种子只在"从零开始"时发生。返回写入后的 revision。
    """
    payload = {k: v for k, v in default.items() if not k.startswith("_")}
    with db.connect() as conn:
        row = conn.execute(
            "SELECT revision FROM assistant_config WHERE scope = ?",
            (CONFIG_SCOPE,)).fetchone()
        if row:
            return int(row["revision"])
        conn.execute(
            "INSERT INTO assistant_config (scope, revision, content, "
            "updated_by, updated_at) VALUES (?, 1, ?, ?, ?)",
            (CONFIG_SCOPE, json.dumps(payload, ensure_ascii=False),
             updated_by, _now()))
        return 1


def save_config(db, content: Dict[str, Any], updated_by: str = "",
                comment: str = "") -> int:
    """整档直存并递增 revision,用于运行时缓存失效。"""
    payload = {k: v for k, v in content.items() if not k.startswith("_")}
    with db.connect() as conn:
        row = conn.execute(
            "SELECT revision FROM assistant_config WHERE scope = ?",
            (CONFIG_SCOPE,)).fetchone()
        new_rev = (int(row["revision"]) if row else 0) + 1
        if row:
            conn.execute(
                "UPDATE assistant_config SET revision = ?, content = ?, "
                "updated_by = ?, updated_at = ? WHERE scope = ?",
                (new_rev, json.dumps(payload, ensure_ascii=False),
                 updated_by or "", _now(), CONFIG_SCOPE))
        else:
            conn.execute(
                "INSERT INTO assistant_config (scope, revision, content, "
                "updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                (CONFIG_SCOPE, new_rev,
                 json.dumps(payload, ensure_ascii=False),
                 updated_by or "", _now()))
        return new_rev


# ── 对话 flow(设计 §6)──────────────────────────────────────

def create_flow(db, conversation_id: str, user_id: str,
                scenario: str, slots: Dict[str, Any],
                config_revision: Optional[int]) -> str:
    """新建 flow;同会话已有 running flow 直接复用(单任务语义)。

    返回 flow_id。
    """
    flow_id = str(uuid.uuid4())
    with db.connect() as conn:
        row = conn.execute(
            "SELECT flow_id FROM assistant_flows WHERE conversation_id = ? "
            "AND status = ?", (conversation_id, _STATUS_RUNNING)).fetchone()
        if row:
            return str(row["flow_id"])
        conn.execute(
            "INSERT INTO assistant_flows (flow_id, conversation_id, "
            "user_id, scenario, slots, config_revision, flow_revision, status, "
            "created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (flow_id, conversation_id, user_id or "", scenario or "",
             json.dumps(slots, ensure_ascii=False), config_revision, _STATUS_RUNNING,
             _now(), _now()))
        return flow_id


def get_flow(db, conversation_id: str,
             include_done: bool = False) -> Optional[Dict[str, Any]]:
    """读会话当前 flow(默认只看 running;完成后仍可加载改条件)。"""
    query = ("SELECT flow_id, conversation_id, user_id, scenario, slots, "
             "flow_revision, status, pending_question, result_ref, "
             "config_revision FROM assistant_flows WHERE conversation_id = ?")
    params: Tuple[Any, ...] = (conversation_id,)
    if not include_done:
        query += " AND status = ?"
        params = (conversation_id, _STATUS_RUNNING)
    query += " ORDER BY updated_at DESC LIMIT 1"
    with db.connect() as conn:
        row = conn.execute(query, params).fetchone()
    if not row:
        return None
    return _row_to_flow(row)


def get_flow_by_id(db, flow_id: str) -> Optional[Dict[str, Any]]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM assistant_flows WHERE flow_id = ?", (flow_id,)).fetchone()
    return _row_to_flow(row) if row else None


def advance_flow(db, flow_id: str, expected_revision: int,
                 slots: Optional[Dict[str, Any]] = None,
                 scenario: Optional[str] = None,
                 pending_question: Optional[Dict[str, Any]] = None,
                 clear_pending: bool = False,
                 status: Optional[str] = None,
                 result_ref: Optional[dict] = None,
                 config_revision: Optional[int] = None) -> int:
    """CAS 推进 flow:expected_revision 不匹配抛 FlowRevisionError。

    只更新显式传入的字段(None=不动);pending_question 与版本推进
    同事务(设计 §6.1"问题消费与版本推进原子执行")。
    """
    sets = ["flow_revision = flow_revision + 1", "updated_at = ?"]
    params: List[Any] = [_now()]
    if slots is not None:
        sets.append("slots = ?")
        params.append(json.dumps(slots, ensure_ascii=False))
    if scenario is not None:
        sets.append("scenario = ?")
        params.append(scenario)
    if clear_pending:
        sets.append("pending_question = NULL")
    elif pending_question is not None:
        sets.append("pending_question = ?")
        params.append(json.dumps(pending_question, ensure_ascii=False))
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if result_ref is not None:
        sets.append("result_ref = ?")
        params.append(json.dumps(result_ref, ensure_ascii=False))
    if config_revision is not None:
        sets.append("config_revision = ?")
        params.append(int(config_revision))
    params.extend([flow_id, int(expected_revision)])
    with db.connect() as conn:
        cur = conn.execute(
            f"UPDATE assistant_flows SET {', '.join(sets)} "
            "WHERE flow_id = ? AND flow_revision = ? AND status = 'running'", params)
        if cur.rowcount == 0:
            raise FlowRevisionError(
                f"flow 版本冲突(flow_id={flow_id}, "
                f"expected_revision={expected_revision})——迟到或并发写被拒")
        return int(expected_revision) + 1


def close_running_flows(db, conversation_id: str,
                        reason: str = "条件变更") -> int:
    """把会话全部 running flow 置 cancelled(失效传播)。

    返回关闭条数。cancelled flow 保留行(审计),但 get_flow 默认
    只看 running——旧 pending/结果引用不再被消费。
    """
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE assistant_flows SET status = ?, updated_at = ?, "
            "flow_revision = flow_revision + 1, pending_question = NULL "
            "WHERE conversation_id = ? AND status = ?",
            (_STATUS_CANCELLED, _now(), conversation_id, _STATUS_RUNNING))
        return cur.rowcount


def _row_to_flow(row) -> Dict[str, Any]:
    r = dict(row)
    pending = None
    if r.get("pending_question"):
        try:
            pending = json.loads(r["pending_question"])
        except (TypeError, ValueError):
            pending = None
    result_ref = None
    if r.get("result_ref"):
        try:
            result_ref = json.loads(r["result_ref"])
        except (TypeError, ValueError):
            result_ref = None
    return {
        "flowId": r["flow_id"], "conversationId": r["conversation_id"],
        "userId": r.get("user_id") or "", "scenario": r.get("scenario") or "",
        "slots": json.loads(r["slots"]) if r.get("slots") else {},
        "flowRevision": int(r["flow_revision"]), "status": r["status"],
        "pendingQuestion": pending, "resultRef": result_ref,
        "configRevision": (int(r["config_revision"])
                           if r.get("config_revision") is not None else None),
    }
