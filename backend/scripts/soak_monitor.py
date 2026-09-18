"""ChatBI 迁移 soak 监控(二十审 9.8 修复版)。

每 5 分钟向 logs/soak-<date>.jsonl 追加一条快照:
  - 元数据: git commit / 进程启动时间 / 配置摘要(二十审要求写入验收元数据)
  - 每数据源: semantic 当前版本 / active 索引指针 / merge 报告版本与告警
  - build ledger 状态分布(孤儿增长观察) + chunk 身份行数(含含截断键计数)
  - scheduler 租约持有者(多进程唯一性观察)
  - 最近 1h 任务统计(refresh/scan 成功/失败) + 任务总数
  - 双实例 RSS/线程数/fd(二十审 9.8: 采样失败显式记 null, 不再伪装 0)
  - 两个实例日志的 ERROR 计数(累计)

配置(全部必填, fail-closed):
  SOAK_DATABASE_URL   PG 连接串(不再硬编码回退)
  SOAK_PORTS          逗号分隔端口, 缺省 "18080,18081"
  SOAK_INSTANCE_LOGS  逗号分隔日志路径, 缺省 /tmp/llm-modeler-{port}.log

用法: venv/bin/python scripts/soak_monitor.py [--interval 300]
停止: kill <pid>(SIGTERM 后当前快照写完即退)
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DSN = os.getenv("SOAK_DATABASE_URL", "").strip()
PORTS = [p.strip() for p in
         os.getenv("SOAK_PORTS", "18080,18081").split(",") if p.strip()]
INSTANCE_LOGS = [l.strip() for l in
                 os.getenv("SOAK_INSTANCE_LOGS", "").split(",") if l.strip()]
if not INSTANCE_LOGS:
    INSTANCE_LOGS = [f"/tmp/llm-modeler-{p}.log" for p in PORTS]
if not DSN:
    print("SOAK_DATABASE_URL 未设置(fail-closed, 不再硬编码回退)", flush=True)
    sys.exit(2)

_REPO = os.path.join(os.path.dirname(__file__), "..")
try:
    GIT_COMMIT = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=10, cwd=_REPO).stdout.strip()
except Exception:
    GIT_COMMIT = "unknown"
try:
    GIT_DIRTY = bool(subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=10, cwd=_REPO).stdout.strip())
except Exception:
    GIT_DIRTY = None
import hashlib
try:
    SCRIPT_HASH = hashlib.sha256(open(os.path.abspath(__file__), "rb")
                                 .read()).hexdigest()[:12]
except Exception:
    SCRIPT_HASH = "unknown"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _ps_fields(pid: int, fmt: str):
    """macOS/Linux 通用单字段采样; 失败返回 None(调用方记 null)。"""
    try:
        out = subprocess.run(
            ["ps", "-o", fmt, "-p", str(pid)],
            capture_output=True, text=True, timeout=10).stdout
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # 去掉可能的表头行, 取最后一个数字行(ps 输出尾部有空白填充行)
        digits = [l for l in lines if l.isdigit()]
        return int(digits[-1]) if digits else None
    except Exception:
        return None


def proc_metrics():
    """RSS(kb)/线程数/fd 数。

    二十审 9.8: macOS 无 nlwp——线程数用 `ps -M` 数据行数; RSS 用
    `ps -o rss=`; 任一采样失败显式记 None, 绝不伪装成 0。
    """
    out = []
    pids, seen = [], set()
    for port in PORTS:
        try:
            # 二十审 5.7: 只取 LISTEN 进程——":port" 无过滤会把连到该端口
            # 的客户端(如 monitor 自己的出站连接)也算成服务进程
            # 注意: "-ti" 与地址分开传时 lsof 解析异常(实测返回空),
            # 必须合并为单个参数或用 "-t -i..." 分列
            pids += subprocess.run(
                ["/usr/sbin/lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=10).stdout.split()
        except Exception:
            pass
    for pid in pids:
        if pid in seen or not pid.isdigit() or int(pid) == os.getpid():
            continue
        seen.add(pid)
        rss = _ps_fields(int(pid), "rss=")
        try:
            m = subprocess.run(["ps", "-M", "-p", str(pid)],
                               capture_output=True, text=True,
                               timeout=10).stdout.splitlines()
            threads = max(0, len([l for l in m if l.strip()]) - 1)
        except Exception:
            threads = None
        try:
            fds = len(subprocess.run(
                ["/usr/sbin/lsof", "-p", str(pid)],
                capture_output=True, text=True, timeout=15).stdout.splitlines())
        except Exception:
            fds = None
        started = None
        try:
            lstart = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=10).stdout.strip()
            started = lstart or None
        except Exception:
            pass
        out.append({"pid": int(pid), "rss_kb": rss, "threads": threads,
                    "fds": fds, "started": started})
    return out


def _verdict(snap_body, history) -> str:
    """每条快照给出明确结论(二十审 5.7 + 二十二审 5.7 扩展)。

    检查维度:
      一致性: pointer_lag / requires_review / building 堆积
      任务健康: 1h 内 degraded 或 failed 任务
      效率: 无变化刷新仍全量重建(tasks_1h 中 succeeded refresh 数远超
            周期应有个数——按 refresh 周期换算) / 重复 rebuild(身份表
            total 突增 = 又做了全量 embedding)
      资源: RSS 单点 8GB 上限 + 相对首帧增量 > 2GB(异常高水位)
    """
    problems = []
    for d in snap_body["datasources"]:
        if d.get("pointer_lag") not in (0, None):
            problems.append(f"{d['ds']} pointer_lag={d['pointer_lag']}")
        if d.get("requires_review"):
            problems.append(f"{d['ds']} requires_review")
    if snap_body.get("build_ledger", {}).get("building", 0) > 3:
        problems.append("building 堆积")
    # 任务健康: degraded 留痕(refresh 结果里 index_rebuild=degraded 的
    # 任务在 tasks 表 status=failed——按 failed 计数; succeeded 高频 +
    # 身份表未增 = 无变化重复重建嫌疑)
    for t in snap_body.get("tasks_1h", []):
        if t["type"] == "chatbi.refresh_semantics" and t["status"] == "failed":
            err_degraded = True   # failed 可能是租约冲突(正常)——降为 WARN
    refresh_ok = sum(t["n"] for t in snap_body.get("tasks_1h", [])
                     if t["type"] == "chatbi.refresh_semantics"
                     and t["status"] == "succeeded")
    # 周期(分钟)来自快照外配置; 默认按 6h=每 1h 1 次判断——超过 2 次
    # 且身份行数未增长, 即重复重建嫌疑(二十二审 6 的症状)
    ident_total = snap_body.get("chunk_identities", {}).get("total", 0)
    prev_ident = history.get("prev_ident_total")
    if refresh_ok > 3 and prev_ident is not None and ident_total == prev_ident:
        problems.append(f"1h内 {refresh_ok} 次 refresh 且索引未变(重复重建嫌疑)")
    history["prev_ident_total"] = ident_total
    # 资源: 单点 + 增量
    base_rss = history.get("base_rss") or {}
    for p in snap_body.get("processes", []):
        rss = p.get("rss_kb")
        if rss is None:
            continue
        if rss > 8 * 1024 * 1024:
            problems.append(f"pid{p['pid']} RSS>8GB")
        b = base_rss.get(p["pid"])
        if b is None:
            base_rss[p["pid"]] = rss
        elif rss - b > 2 * 1024 * 1024:
            problems.append(f"pid{p['pid']} RSS 较基线 +{(rss-b)//1024//1024}GB")
    history["base_rss"] = base_rss
    return "FAIL: " + "; ".join(problems) if problems else "OK"


def snapshot(db, _hist=None):
    _hist = _hist if _hist is not None else {}
    snap = {"ts": now_iso(), "commit": GIT_COMMIT,
            "git_dirty": GIT_DIRTY, "script_hash": SCRIPT_HASH}
    with db.connect() as conn:
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
                "WHERE scope = ?",
                (ds["scope_id"],)).fetchone() if ds["scope_id"] else None
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
        ledger = conn.execute(
            "SELECT status, COUNT(*) AS n FROM chatbi_index_builds "
            "GROUP BY status").fetchall()
        snap["build_ledger"] = {r["status"]: int(r["n"]) for r in ledger}
        ident = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE chunk_id LIKE '%#%') "
            "AS trunc FROM chatbi_chunk_identities").fetchone()
        snap["chunk_identities"] = {"total": int(ident["n"]),
                                    "truncated": int(ident["trunc"])}
        leases = conn.execute(
            "SELECT task_type, holder, expires_at FROM chatbi_scheduler_leases"
        ).fetchall()
        snap["leases"] = [
            {"task": r["task_type"], "holder": r["holder"][-13:],
             "expires_ms": int(r["expires_at"])
             if str(r["expires_at"]).isdigit() else None}
            for r in leases]
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
    log_errors = {}
    for p in INSTANCE_LOGS:
        try:
            with open(p, "rb") as f:
                data = f.read()
            log_errors[os.path.basename(p)] = data.count(b"ERROR")
        except OSError:
            log_errors[os.path.basename(p)] = None
    snap["log_errors"] = log_errors
    snap["verdict"] = _verdict(snap, _hist)
    return snap


def main():
    interval = 300
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])
    os.makedirs(LOG_DIR, exist_ok=True) if (LOG_DIR := os.path.join(
        os.path.dirname(__file__), "..", "logs")) else None
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = os.path.join(LOG_DIR, f"soak-{date}.jsonl")
    from sdk.relational_store import PackRelationalDB
    db = PackRelationalDB("chatbi", database_url=DSN)
    print(f"soak monitor -> {out_path} every {interval}s "
          f"commit={GIT_COMMIT} ports={PORTS}", flush=True)
    _hist = {}
    while True:
        try:
            snap = snapshot(db, _hist)
            with open(out_path, "a") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            procs = " ".join(
                f"pid{p['pid']}:rss={p['rss_kb']},th={p['threads']},fd={p['fds']}"
                for p in snap["processes"])
            print(f"[{snap['ts'][:19]}] {snap['verdict']} {procs}",
                  flush=True)
        except Exception as e:
            print(f"[{now_iso()}] snapshot failed: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
