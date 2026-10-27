"""公共 pytest fixtures.

测试组合根:平台服务的 SDK 门面在此装配(生产在 main.py 装配,
两处各自完成依赖注入——依赖倒置的标准做法)。最小 app 测试
(只挂 pack router、不经 main)也能拿到已装配的门面。

【PG-only 测试库引导】(必须在任何 Store/graph import 之前执行)
平台存储已收敛为 PostgreSQL 唯一后端,测试全量跑 PG:

  1. 解析 TEST_DATABASE_URL(env > backend/.env > 项目根 .env)
  2. 安全闸:库名必须含 "test"——防止把清表逻辑指到生产库
  3. 库不存在时自动建(连维护库 postgres 执行 CREATE DATABASE)
  4. os.environ["DATABASE_URL"] = 测试库 —— 运行时代码(Store/
     graph checkpointer/main lifespan)与测试同库
  5. 每个测试前 TRUNCATE 全部业务表(等价于旧 SQLite 时代的
     "每个 tmp 文件一个新库"隔离语义;pytest 串行执行下安全)

快速起测试库(本地,用常驻 chatbi-postgres 建独立测试库+vector 扩展;
不要另起纯 postgres 镜像——无 vector 扩展,向量用例会整片失败):
  docker exec chatbi-postgres psql -U root -d postgres \\
      -c "CREATE DATABASE llm_modeler_test_run"
  docker exec chatbi-postgres psql -U root -d llm_modeler_test_run \\
      -c "CREATE EXTENSION IF NOT EXISTS vector"
  export TEST_DATABASE_URL=postgresql://root:root@localhost:5432/llm_modeler_test_run
"""
import os
import sys
from pathlib import Path

# 让 backend/src 可被 import
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "src"))

import pytest


def _resolve_test_url() -> str:
    """TEST_DATABASE_URL 解析链:env > backend/.env > 项目根 .env。"""
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if url:
        return url
    try:
        from dotenv import load_dotenv
        for cand in (BACKEND_ROOT / ".env", BACKEND_ROOT.parent / ".env"):
            if cand.exists():
                load_dotenv(cand, override=False)
                url = os.getenv("TEST_DATABASE_URL", "").strip()
                if url:
                    return url
    except ImportError:
        pass
    pytest.exit(
        "未配置 TEST_DATABASE_URL:测试需要专用的 PostgreSQL 测试库"
        "(会被清表,绝不指向业务库)。本地约定:直接用常驻的 chatbi-postgres"
        "(pgvector 镜像,root:root@localhost:5432)建测试库,名字必须含 test:\n"
        "  docker exec chatbi-postgres psql -U root -d postgres -c "
        "'CREATE DATABASE llm_modeler_test_run'\n"
        "  docker exec chatbi-postgres psql -U root -d llm_modeler_test_run -c "
        "'CREATE EXTENSION IF NOT EXISTS vector'\n"
        "  export TEST_DATABASE_URL=postgresql://root:root@localhost:5432/llm_modeler_test_run\n"
        "也可写入 backend/.env 或项目根 .env(.gitignore 已覆盖)。详见 .env.example 存储段。",
        returncode=4,
    )


def _ensure_database(url: str) -> None:
    """目标库不存在时自动建(连维护库 postgres 执行 CREATE DATABASE)。"""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(url)
    dbname = params.get("dbname") or "postgres"
    admin = dict(params, dbname="postgres")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(**admin), autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
                print(f"\n[conftest] 已自动创建测试库: {dbname}")
    except psycopg.errors.DuplicateDatabase:
        pass  # 并发首跑竞态:别人刚建好,视为成功
    except psycopg.errors.InsufficientPrivilege:
        pytest.exit(
            f"当前用户无 CREATEDB 权限,无法自动创建测试库 '{dbname}'——"
            "请手工创建后重跑(或让管理员授权)。", returncode=4)
    except psycopg.OperationalError as e:
        pytest.exit(f"连接 PG 失败({e})——请检查 TEST_DATABASE_URL 可达性。", returncode=4)


# 并行执行守卫:单库 + 每测试清表与多 worker 不兼容(互相清对方的表)
if os.getenv("PYTEST_XDIST_WORKER"):
    pytest.exit("检测到 xdist 并行:测试隔离基于单库逐测试清表,不支持并行", returncode=4)

TEST_DATABASE_URL = _resolve_test_url()

# 安全闸:清表逻辑只允许指向名字含 "test" 的库
from psycopg.conninfo import conninfo_to_dict as _cid

_dbname = (_cid(TEST_DATABASE_URL).get("dbname") or "").lower()
if "test" not in _dbname:
    pytest.exit(
        f"TEST_DATABASE_URL 指向的库 '{_dbname}' 不含 'test'——"
        "测试会清空全部业务表,拒绝在疑似业务库上执行。请改指 *_test 库。",
        returncode=4,
    )

# 安全闸 2:测试库不得与运行库同库。每个测试都会 TRUNCATE 全部业务表,
# 若与运行库(backend/.env 的 DATABASE_URL / 运行中服务)同库,跑一次全量
# 回归就会清空演示数据源/语义层/看板——已两次事故,此闸杜绝第三次。
# 【第三次事故实证 2026-10-17】此闸此前只查 os.getenv("DATABASE_URL")——
# 独立 shell/worktree agent 的 env 里没有该变量(运行库 DSN 只活在 .env
# 文件里),闸形同虚设:worktree agent 把 TEST_DATABASE_URL 指向运行库
# llm_modeler_test(名字含 test 过了闸 1),一次全量清空了 KG 库/会话史/
# chatbi 演示数据。修复:运行库 DSN 也走 env > backend/.env > 根 .env
# 同一条解析链,与 main.py 的加载序一致。
def _resolve_runtime_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    try:
        from dotenv import dotenv_values
        for cand in (BACKEND_ROOT / ".env", BACKEND_ROOT.parent / ".env"):
            if cand.exists():
                url = (dotenv_values(cand).get("DATABASE_URL") or "").strip()
                if url:
                    return url
    except ImportError:
        pass
    return ""

_runtime_dsn = _resolve_runtime_url()
_runtime_db = (_cid(_runtime_dsn).get("dbname") or "").lower() if _runtime_dsn else ""
if _runtime_db and _runtime_db == _dbname:
    pytest.exit(
        f"TEST_DATABASE_URL 与运行库 DATABASE_URL 同库('{_dbname}')——"
        "测试会 TRUNCATE 运行数据。请为 pytest 建专用库,如:\n"
        "  docker exec chatbi-postgres psql -U root -d postgres "
        "-c 'CREATE DATABASE llm_modeler_test_run OWNER root;'\n"
        "并把 backend/.env 的 TEST_DATABASE_URL 指向它。",
        returncode=4,
    )

_ensure_database(TEST_DATABASE_URL)

# ── 并发互踩根治(脚本层机制,不靠纪律) ────────────────────────
# 单库+每测试 TRUNCATE 与并发 pytest 进程不兼容(互清对方数据→死锁/假
# 失败/僵尸连接)。历史上靠"归因前 pgrep 查并发"的纪律防——纪律会滑坡
# (2026-10-16 实证:23h 僵尸 mock 进程挂 7 连接,全量 46min)。改为库级
# advisory lock:第二个 pytest 启动即 fail-fast 并报持锁方,锁随会话
# 断开自动释放(僵尸进程被杀后无需人工清锁)。
_TEST_LOCK_KEY = 0x7E57  # "TEST" 谐音,任意固定值
import psycopg as _psycopg

_lock_conn = _psycopg.connect(TEST_DATABASE_URL, autocommit=True)
_granted = _lock_conn.execute(
    "SELECT pg_try_advisory_lock(%s)", (_TEST_LOCK_KEY,)).fetchone()[0]
if not _granted:
    # 精确诊断:只报真正持有本 advisory lock 的连接(pg_locks JOIN
    # pg_stat_activity)——旧查询按 datname 列同库全部连接,会把无辜
    # 连接(如泄漏的 mock 上游连接)误报成持锁者,误导排查方向。
    _holder = _lock_conn.execute(
        "SELECT a.pid, a.application_name, a.client_addr, "
        "now()-a.backend_start AS age, state "
        "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE l.locktype = 'advisory' AND l.objid = %s "
        "AND a.pid != pg_backend_pid()",
        (_TEST_LOCK_KEY,)).fetchall()
    _lock_conn.close()
    pytest.exit(
        "测试库已被另一个 pytest 会话持有(advisory lock):单库逐测试清表"
        "与并发不兼容,会互踩出死锁/假失败。\n当前持锁连接:\n  "
        + "\n  ".join(str(h) for h in _holder)
        + "\n处理:等对方跑完,或 kill 旧进程后重跑。", returncode=4)

# 运行时(Store/graph checkpointer/main lifespan)与测试同库
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# 装配 sdk.pack_api 的插件配置读取器工厂
from services.pack_settings import PackSettingsReader
from sdk.pack_api import register_settings_reader

register_settings_reader(lambda pack, store: PackSettingsReader(pack, store))

# 装配 sdk.pack_api 的管理端鉴权实现(pack api 测试的最小 app 依赖它)
from api.admin import require_admin
from sdk.pack_api import register_admin_auth

register_admin_auth(require_admin)

# ── 测试隔离:每测试清表 ──────────────────────────────────────
# 全部业务表(schema 限定:平台表在 public,pack 表在各自独立 schema——
# 见 sdk.relational_store 的命名空间隔离)。TRUNCATE RESTART IDENTITY 把
# 自增序列一并归零(task_logs 断线补齐游标、检查点行都回到"全新库"语义),
# 等价于旧 SQLite 时代每测试一个 tmp 文件。pytest 默认串行执行,无并发竞争。
_TRUNCATE_SCHEMAS = ("public", "knowledge_graph", "chatbi",
                      "metrology_assistant")
_TRUNCATE_EXCLUDE = {"checkpoint_migrations"}


@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    """会话级建表(四 Store DDL + checkpoint 三表,幂等执行一次,早于首个测试)。

    teardown 顺带清理向量残留表:测试按 scope UUID 建向量表(zz_test_* /
    kg_* / chatbi_*),每测试新 UUID 一张、TRUNCATE 清单只含注册过的表——
    历史累积 258 张(2026-10-16 实证,每测试 TRUNCATE 全库扫描越跑越慢)。
    会话结束 DROP 掉非业务表的向量残留,库水位回到建表基线。
    """
    from services.conversation_store import ConversationStore
    from services.pack_settings import PackSettingsStore
    from services.platform_settings import PlatformSettingsStore
    from services.task_store import TaskStore
    from domains.knowledge_graph.store import KGStore

    ConversationStore()
    PackSettingsStore()
    PlatformSettingsStore()
    TaskStore()
    KGStore()
    # metrology 场景配置/flow 表(P1;幂等建表,清表依赖其存在)
    from domains.metrology_assistant import runtime as metro_runtime
    metro_runtime.get_pack_db()
    # chatbi 语义层/事件表(main 478754d: 测试库 schema 初始化补齐)
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.runtime import _init_pack_schema
    _init_pack_schema(PackRelationalDB("chatbi"))
    # LangGraph checkpoint 三表(PostgresSaver.setup 幂等;清表依赖其存在)
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        PostgresSaver(conn).setup()
    yield
    # teardown:清向量残留表(业务表由 _TRUNCATE 白名单管,这里只删
    # "按 scope 动态建的向量表"——kg_*/chatbi_*/zz_test_* 前缀且不在
    # 业务表白名单内的)
    _business = {"bi_probe_t", "biz_orders", "biz_users"}
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        # 残留表可能被泄漏连接持锁(僵尸 mock 进程等)——DROP 默认无
        # 超时会无限挂起拖死整个 teardown。限 5s:拿不到锁的表跳过并
        # 告警,不阻断其余表的清理与后续的锁释放。
        conn.execute("SET lock_timeout = '5s'")
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND (tablename LIKE 'zz_test_%' OR tablename LIKE 'kg_%' "
            "     OR tablename LIKE 'chatbi_%')").fetchall()
        for (t,) in rows:
            if t not in _business:
                try:
                    conn.execute(
                        f'DROP TABLE IF EXISTS public."{t}" CASCADE')
                except psycopg.errors.LockNotAvailable:
                    print(f"\n[conftest] teardown: 表 {t} 被持锁连接占用"
                          f"(lock_timeout 5s 到期), 跳过 DROP")
    # 释放并发守卫锁(显式;进程退出断连也会自动释放,双保险)
    try:
        _lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_TEST_LOCK_KEY,))
        _lock_conn.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_tables(_init_schema):
    """每测试清空全部业务表(函数级,autouse;按 _TRUNCATE_SCHEMAS 动态探测
    现存表后 TRUNCATE,新 pack 表自动纳入隔离)。"""
    import psycopg
    from psycopg import sql

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
        existing = {
            f"{r[0]}.{r[1]}" for r in rows
            if r[0] in _TRUNCATE_SCHEMAS and r[1] not in _TRUNCATE_EXCLUDE
        }
        if existing:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(
                        sql.SQL(".").join(map(sql.Identifier, t.split(".")))
                        for t in sorted(existing))
                )
            )
    yield
