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

快速起测试库(本地):
  docker run -d --name pg-test -p 5433:5432 \\
      -e POSTGRES_PASSWORD=dev -e POSTGRES_USER=llm postgres:16-alpine
  export TEST_DATABASE_URL=postgresql://llm:dev@localhost:5433/llm_modeler_test
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
        "(会被清表,绝不指向业务库)。本地快速起库:\n"
        "  docker run -d --name pg-test -p 5433:5432 "
        "-e POSTGRES_PASSWORD=dev -e POSTGRES_USER=llm postgres:16-alpine\n"
        "  export TEST_DATABASE_URL=postgresql://llm:dev@localhost:5433/llm_modeler_test\n"
        "也可写入 backend/.env(.gitignore 已覆盖)。详见 .env.example 存储段。",
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

_ensure_database(TEST_DATABASE_URL)
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
_TRUNCATE_TABLES = (
    "public.session_pack_state", "public.call_logs", "public.events",
    "public.session_meta", "public.tasks", "public.task_logs",
    "public.pack_settings",
    "knowledge_graph.kg_chunks", "knowledge_graph.kg_documents",
    "knowledge_graph.kg_knowledge_bases",
    "public.checkpoints", "public.checkpoint_blobs", "public.checkpoint_writes",
)


@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    """会话级建表(四 Store DDL + checkpoint 三表,幂等执行一次,早于首个测试)。"""
    from services.conversation_store import ConversationStore
    from services.pack_settings import PackSettingsStore
    from services.task_store import TaskStore
    from domains.knowledge_graph.store import KGStore

    ConversationStore()
    PackSettingsStore()
    TaskStore()
    KGStore()
    # LangGraph checkpoint 三表(PostgresSaver.setup 幂等;清表依赖其存在)
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        PostgresSaver(conn).setup()
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_init_schema):
    """每测试清空全部业务表(函数级,autouse;schema 感知——只清实际存在的
    限定表,防御式探测)。"""
    import psycopg
    from psycopg import sql

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
        existing = {f"{r[0]}.{r[1]}" for r in rows} & set(_TRUNCATE_TABLES)
        if existing:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(
                        sql.SQL(".").join(map(sql.Identifier, t.split(".")))
                        for t in sorted(existing))
                )
            )
    yield
