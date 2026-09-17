"""ChatBI 迁移 soak 监控(十九审验收: 6~24h 真实定时刷新观察)。

每 5 分钟向 logs/soak-<date>.jsonl 追加一条快照:
  - 每数据源: semantic 当前版本 / active 索引指针 / merge 报告版本与告警
  - build ledger 状态分布(孤儿增长观察) + chunk 身份行数
  - scheduler 租约持有者(多进程唯一性观察)
  - 最近 1h 任务统计(refresh/scan 成功/失败) + 任务总数
  - 两个 uvicorn 进程 RSS/线程数/fd(资源泄漏观察)
  - 两个实例日志的 ERROR 计数(累计)

用法: venv/bin/python scripts/soak_monitor.py [--interval 300]
停止: kill <pid>(写完当前快照即退出)
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault(
    "DATABASE_URL", "postgresql://root:root@localhost:5432/llm_modeler_test")

from sdk.relational_store import PackRelationalDB  # noqa: E402

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
INSTANCE_LOGS = ["/tmp/llm-modeler-18080.log", "/tmp/llm-modeler-18081.log"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def count_log_errors(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.count(b"ERROR") , len(data.splitlines())
    except OSError:
        return 0, 0


def proc_metrics():
    out = []
    pids, seen = [], set()
    for port in ("18080", "18081"):
        try:
            pids += subprocess.run(
                ["/usr/sbin/lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10).stdout.split()
        except Exception:
            pass
    for pid in pids:
        if pid in seen or not pid.isdigit():
            continue
        seen.add(pid)
        # macOS: ps 取 rss/线程
        try:
            r = subprocess.run(
                ["ps", "-o", "rss=,nlwp=", "-p", pid],
                capture_output=True, text=True, timeout=10).stdout.split()
            rss_kb, threads = (int(r[0]), int(r[1])) if len(r) >= 2 else (0, 0)
        except Exception:
            rss_kb, threads = 0, 0
        try:
            fds = len(subprocess.run(
                ["/usr/sbin/lsof", "-p", pid],
                capture_output=True, text=True, timeout=15).stdout.splitlines())
        except Exception:
            fds = 0
        out.append({"pid": int(pid), "rss_mb": round(rss_kb / 1024, 1),
                    "threads": threads, "fds": fds})
    return out


def snapshot(db: PackRelationalDB):
    snap = {"ts": now_iso()}
    with db.connect() as conn:
        # 每数据源: 语义版本 / active 指针 / 报告
        dss = conn.execute(
            "SELECT id, name, scope_id FROM chatbi_data_sources "
            "WHERE is_active = 1").fetchall()
        per = []
        for ds in dss:
            sem = conn.execute(
                "SELECT version FROM chatbi_semantic_models "
                "WHERE data_source_id = ? AND is_current = 1",
                (ds["id"],)).fetchone()
            rev = conn.execute(
                "SELECT active_doc_id, version FROM chatbi_index_revisions "
                "WHERE scope = ?", (ds["scope_id"],)).fetchone() if ds["scope_id"] else None
            rep = conn.execute(
                "SELECT version, report FROM chatbi_merge_reports "
                "WHERE data_source_id = ?", (ds["id"],)).fetchone()
            review = None
            if rep:
                try:
                    review = json.loads(rep["report"]).get("requires_review")
                except Exception:
                    review = "unparsable"
            per.append({
                "ds": ds["name"],
                "semantic_v": int(sem["version"]) if sem else None,
                "active": (rev["active_doc_id"], int(rev["version"])) if rev else None,
                "report_v": int(rep["version"]) if rep else None,
                "requires_review": review,
                "pointer_lag": (int(sem["version"]) - int(rev["version"]))
                if sem and rev else None,
            })
        snap["datasources"] = per
        # ledger 状态分布 + 身份行数
        ledger = conn.execute(
            "SELECT status, COUNT(*) AS n FROM chatbi_index_builds "
            "GROUP BY status").fetchall()
        snap["build_ledger"] = {r["status"]: int(r["n"]) for r in ledger}
        ident = conn.execute(
            "SELECT scope, COUNT(*) AS n FROM chatbi_chunk_identities "
            "GROUP BY scope").fetchall()
        snap["chunk_identities"] = {r["scope"][:8]: int(r["n"]) for r in ident}
        # 租约
        leases = conn.execute(
            "SELECT task_type, holder, expires_at FROM chatbi_scheduler_leases"
        ).fetchall()
        snap["leases"] = [
            {"task": r["task_type"], "holder": r["holder"][-13:],
             "expires_ms": int(r["expires_at"]) if str(r["expires_at"]).isdigit() else None}
            for r in leases]
        # 任务统计: 最近 1 小时 + 总量
        total = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        snap["tasks_total"] = int(total["n"])
        recent = conn.execute(
            "SELECT task_type, status, COUNT(*) AS n FROM tasks "
            "WHERE created_at >= to_char(now() - interval '1 hour', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.USOF') "
            "GROUP BY task_type, status").fetchall()
        snap["tasks_1h"] = [
            {"type": r["task_type"], "status": r["status"], "n": int(r["n"])}
            for r in recent]
    snap["processes"] = proc_metrics()
    snap["log_errors"] = {
        os.path.basename(p): count_log_errors(p) for p in INSTANCE_LOGS}
    return snap


def main():
    interval = 300
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])
    os.makedirs(LOG_DIR, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join(LOG_DIR, f"soak-{date}.jsonl")
    db = PackRelationalDB("chatbi")
    print(f"soak monitor -> {out_path} every {interval}s", flush=True)
    while True:
        try:
            snap = snapshot(db)
            with open(out_path, "a") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            print(f"[{snap['ts']}] ok", flush=True)
        except Exception as e:
            print(f"[{now_iso()}] snapshot failed: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
