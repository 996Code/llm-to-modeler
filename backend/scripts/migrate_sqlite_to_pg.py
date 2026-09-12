#!/usr/bin/env python3
"""SQLite → PostgreSQL 一次性迁移脚本(幂等,可重复执行)。

【用途】
引擎存储层换 PG(services/db.py 双后端)后,把老 SQLite 库
(默认 data/conversations.db)里的存量数据搬进 PG。连上服务器后直接跑
(**建议停服后迁移**:源库以只读方式打开,对带活跃 WAL 的在线库读到的是
移动快照;只读挂载/远端副本场景 ro 打开可能直接失败):

    # 1. 停 app 容器(防在线写)  2. 迁移  3. 起服务
    cd backend
    DATABASE_URL=postgresql://llm:密码@localhost:15432/llm_modeler \
        python scripts/migrate_sqlite_to_pg.py

【幂等性】
全部走 INSERT ... ON CONFLICT (主键) DO NOTHING——已迁过的行自动跳过,
中断重跑安全;task_logs 自增序列在搬运后 setval 校准。

【迁移范围】(与四个 Store 的表一一对应)
  session_meta / events / call_logs / session_pack_state   会话/链路/记忆
  tasks / task_logs                                          任务中心
  pack_settings                                              插件设置页
  kg_knowledge_bases / kg_documents / kg_chunks              知识图谱元数据

【源文件落点】存量 SQLite 历史上有三个位置,迁移前先确认哪份(或哪几份)
有数据,漏迁即静默丢失:
  1. 容器/compose 形态: ./data/app/conversations.db (容器内 /app/data)
  2. 开发形态(cwd=backend): backend/data/conversations.db
  3. 开发形态(cwd=src 或仓库根): backend/src/data/conversations.db
两份开发库可能并存且数据不相交——都要迁就分别跑两次(本脚本幂等,同库
可叠加)。检查行数: sqlite3 <路径> "SELECT COUNT(*) FROM session_meta;"

【执行方式】迁移脚本未打进镜像(Dockerfile 不含 backend/scripts),在宿主
跑;宿主 python 需有 psycopg,可复用镜像预构建的依赖:
  cd backend
  DATABASE_URL=postgresql://llm:密码@localhost:15432/llm_modeler \
      PYTHONPATH=../backend/.deps python3 scripts/migrate_sqlite_to_pg.py \
      --sqlite /path/to/conversations.db
(或任何装有 psycopg>=3.2 的 python3。)迁移前可选量化将作废的追问现场:
  sqlite3 <源库> "SELECT COUNT(*) FROM (SELECT thread_id FROM checkpoints,
  json_each(checkpoint) WHERE key LIKE '%interrupt%');"  -- 粗略计数

【明确不迁】LangGraph checkpoint 表(sqlite 侧实际为 checkpoints 与
checkpoint_writes 两张;PG 侧 PostgresSaver 另建三张):两版 Saver 的
blob 序列化格式不同,跨库搬运有损坏风险。影响仅限"迁移时刻正在追问
挂起的会话"——其追问现场作废,用户在该会话里发新消息即可(events 里的
历史与 ask 记录完好)。

【验收】
脚本末尾逐表对比 源行数 vs PG 行数(含迁移前已有),全部 ≥ 源行数即成功;
确认无误后即可停用 SQLite(备份归档 data/conversations.db* 后可删)。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# sys.path 引导:脚本位于 backend/scripts/,源码在 backend/src/
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# (表名, 主键列, 说明, schema) —— 搬运顺序:被引用的表先走(无外键约束,纯
# 稳妥起见)。schema=None → public(平台表);kg 三表随 KGStore 走 SDK 关系型
# 通道(sdk.relational_store),落 pack 独立 schema "knowledge_graph"
# (脚本实例化 KGStore 时自动建 schema + 表)。
TABLES = [
    ("pack_settings", ["pack_name"], "插件设置页保存值", None),
    ("session_meta", ["conv_id"], "会话元数据", None),
    ("events", ["id"], "对话事件流", None),
    ("call_logs", ["id"], "LLM/上游调用日志", None),
    ("session_pack_state", ["conv_id", "scope"], "会话级插件记忆", None),
    ("tasks", ["id"], "后台任务主体", None),
    ("task_logs", ["id"], "任务日志(自增 id)", None),
    ("kg_knowledge_bases", ["id"], "KG 知识库", "knowledge_graph"),
    ("kg_documents", ["id"], "KG 文档", "knowledge_graph"),
    ("kg_chunks", ["id"], "KG 切块", "knowledge_graph"),
]


def _qualified(table: str, schema) -> str:
    """PG 侧限定名:kg 三表在 pack schema,平台表在 public(可省)。"""
    return f'"{schema}".{table}' if schema else table

BATCH = 1000


def sqlite_source_counts(sq: sqlite3.Connection) -> dict:
    counts = {}
    for table, _, _, _ in TABLES:
        try:
            counts[table] = sq.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = None  # 源库里没这张表(如 KG 从未用过)
    return counts


def pg_table_exists(pg, table: str, schema=None) -> bool:
    row = pg.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        (schema or "public", table),
    ).fetchone()
    return row is not None


def pg_count(pg, qtable: str) -> int:
    return pg.execute(f"SELECT COUNT(*) FROM {qtable}").fetchone()[0]


def copy_table(sq: sqlite3.Connection, pg, table: str, pk: list[str],
                  schema=None) -> dict:
    """流式搬运一张表(fetchmany 分批,主键冲突跳过),返回统计。"""
    qtable = _qualified(table, schema)
    before = pg_count(pg, qtable)
    cols = [r[1] for r in sq.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return {"source": 0, "pg_before": before, "pg_after": before, "copied": 0,
                "inserted": 0, "note": "源表不存在,跳过"}
    col_list = ", ".join(cols)
    conflict = ", ".join(pk)
    placeholders = ", ".join(["%s"] * len(cols))
    insert = (f"INSERT INTO {qtable} ({col_list}) VALUES ({placeholders}) "
              f"ON CONFLICT ({conflict}) DO NOTHING")

    cur = sq.execute(f"SELECT {col_list} FROM {table}")
    copied = 0
    with pg.cursor() as pgcur:
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            pgcur.executemany(insert, rows)
            copied += len(rows)
    after = pg_count(pg, qtable)
    return {
        "source": copied,
        "pg_before": before,
        "pg_after": after,
        "copied": copied,
        "inserted": after - before,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite → PG 存量迁移(幂等)")
    parser.add_argument("--sqlite", default=os.getenv("DATABASE_PATH", "data/conversations.db"),
                        help="源 SQLite 文件(默认 data/conversations.db)")
    parser.add_argument("--url", default=os.getenv("DATABASE_URL", ""),
                        help="目标 PG 连接串(默认 env DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计源数据与目标现状,不写任何行")
    args = parser.parse_args()

    url = (args.url or "").strip()
    if not url:
        print("✗ 缺少目标库:请用 --url 或配置 env DATABASE_URL")
        return 2
    src = Path(args.sqlite)
    if not src.exists():
        print(f"✗ 源库不存在: {src}")
        return 2

    sq = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row
    src_counts = sqlite_source_counts(sq)
    total_src = sum(c or 0 for c in src_counts.values())

    print(f"源库: {src}")
    for table, _, desc, _schema in TABLES:
        c = src_counts[table]
        print(f"  - {table:<24} {str(c) if c is not None else '(表不存在)'}"
              f"{'  # ' + desc if c else ''}")
    print(f"源合计: {total_src} 行\n")

    import psycopg

    if args.dry_run:
        with psycopg.connect(url) as pg:
            print("[dry-run] 目标 PG 现状:")
            for table, _, _, schema in TABLES:
                if pg_table_exists(pg, table, schema):
                    print(f"  - {table:<24} 已有 {pg_count(pg, _qualified(table, schema))} 行")
                else:
                    print(f"  - {table:<24} 表未建(正式迁移时会自动建)")
        print("[dry-run] 未写入任何数据。")
        return 0

    # 正式迁移:先实例化四个 Store 触发目标库 DDL(幂等),再搬运
    from services.conversation_store import ConversationStore
    from services.pack_settings import PackSettingsStore
    from services.task_store import TaskStore
    from domains.knowledge_graph.store import KGStore

    ConversationStore(database_url=url)
    PackSettingsStore(db_path="", database_url=url)
    TaskStore(db_path="", database_url=url)
    KGStore(database_url=url)
    print("目标库 schema 已就绪(四 Store DDL 幂等执行)\n")

    results = {}
    with psycopg.connect(url) as pg:
        for table, pk, desc, schema in TABLES:
            r = copy_table(sq, pg, table, pk, schema)
            results[table] = r
            print(f"  {table:<24} 源 {r['source']:>7} → 新增 {r['inserted']:>7}"
                  f" (PG 现有 {r['pg_after']:>7})")
            pg.commit()

        # task_logs:显式搬了历史自增 id,校准 identity 序列,防后续插入撞号
        seq = pg.execute(
            "SELECT setval(pg_get_serial_sequence('task_logs', 'id'), "
            "COALESCE((SELECT MAX(id) FROM task_logs), 0) + 1, false)"
        ).fetchone()
        print(f"\n  task_logs identity 序列已校准 → {seq[0]}")

        # 完整性提示(不阻断):events 里引用了 session_meta 不存在的会话?
        orphans = pg.execute(
            "SELECT COUNT(*) FROM events e "
            "WHERE NOT EXISTS (SELECT 1 FROM session_meta m WHERE m.conv_id = e.conv_id)"
        ).fetchone()[0]
        if orphans:
            print(f"  ⚠ 注意: {orphans} 条 events 引用的会话不在 session_meta"
                  f"(源库即如此,已原样保留)")

    # 验收:PG 行数 ≥ 源行数
    print("\n==== 验收 ====")
    ok = True
    with psycopg.connect(url) as pg:
        for table, _, _, schema in TABLES:
            s = src_counts[table]
            if s is None:
                continue
            p = pg_count(pg, _qualified(table, schema))
            status = "✓" if p >= s else "✗ 缺行!"
            if p < s:
                ok = False
            print(f"  {status} {table:<24} 源 {s:>7} / PG {p:>7}")

    print("\n说明: LangGraph checkpoint 三表未迁移(格式不兼容,风险可控)——"
          "迁移时刻处于追问挂起状态的会话,其现场作废,发新消息即可恢复使用;"
          "全部历史消息/链路/任务记录不受影响。")
    print("迁移完成" + (",验收通过。SQLite 文件可备份归档后删除。" if ok
                       else ",但存在缺行,请检查上方 ✗ 项后重跑(幂等)。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
