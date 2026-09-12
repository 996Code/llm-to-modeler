"""PG 存储层专项测试 —— conftest 已把全量测试引导到 PG 测试库,本文件聚焦方言分支与迁移读回。

覆盖点(与 SQLite 版行为对齐 + PG 方言分支):
  - ConversationStore: 事件流读写/消息重建/级联删除/pack_state upsert 上限/
    call_logs 过滤/管理端 ILIKE 搜索 + packs 过滤(jsonb pack 归属)/统计
  - 已迁数据读回: 迁移脚本搬进来的行必须能走 Store 正常读出
  - TaskStore: append_log 的 RETURNING id + after_id 游标
  - PackSettingsStore: 合并保存/显式清除
  - PostgresSaver: setup + checkpoint 读写往返(追问现场落 PG)

本地跑法(在 chatbi-postgres 容器里建独立测试库,不动业务库):
  docker exec chatbi-postgres psql -U root -d postgres -c \\
      "DROP DATABASE IF EXISTS llm_modeler_test; CREATE DATABASE llm_modeler_test;"
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/llm_modeler_test \\
      ./venv/bin/python -m pytest tests/test_pg_backend.py -v
"""
import json
import os
import uuid

import pytest

# conftest 已引导 TEST_DATABASE_URL 并设为 DATABASE_URL(必配,缺则收集期退出)
TEST_URL = os.getenv("DATABASE_URL", "")


@pytest.fixture()
def conv_store():
    from services.conversation_store import ConversationStore
    s = ConversationStore()
    yield s
    # 清理本测试写入的会话(按独占 user 前缀识别,不碰已迁数据)
    for c in s.list_conversations("pgtest-user", limit=1000):
        s.delete_conversation_any_user(c["id"])


def test_pg_event_flow_and_messages(conv_store):
    """会话创建→消息追加→事件重放→删除级联(PG 路径全链路)。"""
    conv = conv_store.create_conversation("pgtest-user", title="PG 冒烟")
    cid = conv["id"]
    conv_store.add_message(cid, "user", "参与拼团的用户月度复购率")
    conv_store.add_message(cid, "assistant", "已生成柱状图")
    conv_store.append_event(cid, "trace", {"stage": "chatbi.execute", "status": "ok"})

    msgs = conv_store.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]

    events = conv_store.load_events(cid, kinds=["trace"])
    assert len(events) == 1 and events[0]["payload"]["stage"] == "chatbi.execute"

    detail = conv_store.get_conversation(cid, "pgtest-user")
    assert detail["displayTitle"] == "PG 冒烟"
    assert detail["messages"][0]["content"].endswith("复购率")

    assert conv_store.conversation_exists(cid, "pgtest-user")
    assert conv_store.delete_conversation(cid, "pgtest-user") is True
    assert conv_store.get_conversation(cid, "pgtest-user") is None
    # 级联删除: events 不能残留
    assert conv_store.load_events(cid) == []


def test_pg_pack_state_upsert_and_limit(conv_store):
    cid = conv_store.create_conversation("pgtest-user")["id"]
    conv_store.set_pack_state(cid, "chatbi", {"prev_sql": "SELECT 1"})
    # upsert: 同 key 整包覆盖
    conv_store.set_pack_state(cid, "chatbi", {"prev_sql": "SELECT 2", "prev_tables": ["t"]})
    assert conv_store.get_pack_state(cid, "chatbi")["prev_sql"] == "SELECT 2"

    with pytest.raises(ValueError):  # 体积上限 fail-closed
        conv_store.set_pack_state(cid, "chatbi", {"blob": "x" * 70000})


def test_pg_call_logs_filters(conv_store):
    cid = conv_store.create_conversation("pgtest-user")["id"]
    conv_store.save_call_log("llm", "qwen", {"prompt": "a"}, {"text": "b"},
                             status_code=200, duration_ms=10, conv_id=cid)
    conv_store.save_call_log("upstream", "http://svc/api", None, None,
                             status_code=500, error_message="boom")

    assert len(conv_store.get_call_logs(conv_id=cid)) == 1
    assert conv_store.get_call_logs(conv_id=cid)[0]["request_data"] == {"prompt": "a"}
    page = conv_store.query_call_logs(call_type="upstream", limit=10)
    assert page["total"] >= 1 and page["items"][0]["error_message"] == "boom"


def test_pg_admin_search_and_pack_filter(conv_store):
    """管理端两个方言分支: ILIKE 模糊搜索 + packs 过滤的 jsonb pack 归属。"""
    marker = str(uuid.uuid4())[:8]
    cid = conv_store.create_conversation("pgtest-user", title=f"搜索针 {marker}")["id"]
    # 一条 intent_route trace → 会话归属 pack=pg_probe_pack
    conv_store.append_event(cid, "trace", {
        "stage": "intent_route",
        "detail": {"pack": "pg_probe_pack"},
    })

    rows = conv_store.list_all_conversations(q=marker)
    assert any(r["id"] == cid for r in rows)  # ILIKE 命中

    rows = conv_store.list_all_conversations(packs=["pg_probe_pack"])
    assert any(r["id"] == cid and r["pack"] == "pg_probe_pack" for r in rows)

    rows = conv_store.list_all_conversations(packs=[""])  # "其他" = 无路由记录
    assert all(r["pack"] == "" for r in rows)

    assert conv_store.count_all_conversations(q=marker) == 1
    stats = conv_store.get_admin_stats()
    assert stats["conversations"] >= 1 and "byType" in stats["calls"]


def test_external_rows_readable_via_store(conv_store):
    """迁移脚本搬进来的行,必须能经 Store 正常读出(不只裸 SQL 可查)。

    自备"迁移风格"数据:绕过 Store 用裸 SQL 写入(与迁移脚本的产物等价
    ——Store 之外的写入路径),再断言 Store 的列表/详情/消息重建可读回。
    (旧版依赖"目标库恰好有已迁数据",在每测试清表架构下永远 skip。)
    """
    import psycopg
    cid = str(__import__("uuid").uuid4())
    with psycopg.connect(TEST_URL) as conn:
        conn.execute(
            "INSERT INTO session_meta (conv_id, user_id, context_key, title, summary,"
            " created_at, updated_at) VALUES (%s, 'legacy-user', '', '迁移行', '',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00')", (cid,))
        for kind, payload in [
            ("user", '{"role": "user", "content": "老问题"}'),
            ("assistant", '{"role": "assistant", "content": "老回答"}'),
        ]:
            conn.execute(
                "INSERT INTO events (id, conv_id, kind, payload, created_at)"
                " VALUES (%s, %s, %s, %s::jsonb::text, '2026-01-01T00:00:02+00:00')",
                (str(__import__("uuid").uuid4()), cid, kind, payload))

    listed = conv_store.list_all_conversations(q="迁移行")
    assert any(r["id"] == cid and r["userId"] == "legacy-user" for r in listed)
    detail = conv_store.get_conversation_any_user(cid)
    assert [m["content"] for m in detail["messages"]] == ["老问题", "老回答"]


def test_pg_task_log_returning_id():
    from services.task_store import TaskStore
    ts = TaskStore()
    t = ts.create_task("pg.probe", pack_name="test", title="PG")
    id1, _ = ts.append_log(t["id"], "info", "第一条")
    id2, _ = ts.append_log(t["id"], "info", "第二条")
    assert id2 == id1 + 1  # RETURNING id 自增连续(断线补齐游标依赖)
    logs = ts.list_logs(t["id"], after_id=id1)
    assert [l["message"] for l in logs] == ["第二条"]
    ts.update_task(t["id"], status="succeeded", result={"ok": 1})
    assert ts.get_task(t["id"])["result"] == {"ok": 1}
    ts.delete_logs(t["id"])
    # 任务主体留着(管理端可见),不污染断言——用 update 归档即可


def test_pg_pack_settings_merge():
    from services.pack_settings import PackSettingsStore
    st = PackSettingsStore()
    pack = f"pg_probe_{uuid.uuid4().hex[:6]}"
    st.save_values(pack, {"neo4j_uri": "bolt://x", "top_k": 5})
    st.save_values(pack, {"top_k": 8})            # 未提供键保持
    assert st.get_values(pack) == {"neo4j_uri": "bolt://x", "top_k": 8}
    st.save_values(pack, {"neo4j_uri": None})     # None = 显式清除
    assert st.get_values(pack) == {"top_k": 8}
    st.delete(pack)


def test_postgres_saver_roundtrip():
    """LangGraph PostgresSaver: setup 建表 + checkpoint 写读往返(追问现场)。"""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    thread = f"pg-test-{uuid.uuid4().hex[:8]}"
    # autocommit 必须:setup() 的 CREATE INDEX CONCURRENTLY 不能在事务块内
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        saver = PostgresSaver(conn)
        saver.setup()  # 幂等
        ckpt = {
            "v": 4,
            "id": f"ckpt-{thread}",
            "ts": "2026-09-12T00:00:00+00:00",
            "channel_values": {"user_input": "年度呢?"},
            "channel_versions": {},
            "versions_seen": {},
        }
        saver.put({"configurable": {"thread_id": thread, "checkpoint_ns": ""}}, ckpt,
                  {"source": "test", "step": 1, "writes": None}, {})
        got = saver.get({"configurable": {"thread_id": thread, "checkpoint_ns": ""}})
        assert got is not None
        assert got["channel_values"]["user_input"] == "年度呢?"


def test_pg_kg_store_paths():
    """KGStore PG 路径 —— 评审抓出的两个必崩点回归锚:
    ① replace_chunks 走 conn.executemany(psycopg Connection 没有该方法)
    ② recover_importing_docs 的 LIKE 前缀模式(含字面 %,必须走参数绑定)
    """
    from domains.knowledge_graph.store import KGStore
    ks = KGStore()
    kb_name = f"pg_kb_{uuid.uuid4().hex[:6]}"
    kb = ks.create_kb(kb_name, description="PG 冒烟")
    try:
        doc = ks.create_document(kb["id"], "probe.txt", "text/plain", 12,
                                 f"/tmp/{kb_name}.txt", f"hash-{kb_name}")
        # ① executemany 批量切块(重导入主路径)
        n = ks.replace_chunks(doc["id"], kb["id"],
                              [{"seq": i, "text": f"第{i}块"} for i in range(5)])
        assert n == 5
        chunks = ks.list_chunks(doc["id"])
        assert [c["seq"] for c in chunks] == list(range(5))
        assert ks.mark_chunk(chunks[0]["id"], "done") is True

        # ② 启动恢复:先全部收敛再把活任务放回 importing(参数化 LIKE 分支)。
        #    返回值 = 收敛总数(含活任务);活任务的判定看状态被放回
        ks.update_document(doc["id"], import_status="importing")
        ks.recover_importing_docs(active_doc_ids=[doc["id"]])
        alive = ks.get_document(doc["id"])
        assert alive["importStatus"] == "importing" and alive["error"] == ""
        ks.recover_importing_docs(active_doc_ids=[])
        d = ks.get_document(doc["id"])
        assert d["importStatus"] == "failed" and "服务重启" in d["error"]
    finally:
        ks.delete_kb(kb["id"])


def test_truncate_list_covers_all_tables():
    """清表名单漂移守卫:库内任何 schema 下的业务表(除 checkpoint_migrations
    记账表)都必须以 schema 限定名出现在 conftest 清表名单里——否则新表/新
    pack schema 残留跨测试脏数据,故障隐蔽。"""
    import psycopg
    from tests.conftest import _TRUNCATE_TABLES

    with psycopg.connect(TEST_URL) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
    uncovered = ({f"{r[0]}.{r[1]}" for r in rows}
                 - set(_TRUNCATE_TABLES) - {"public.checkpoint_migrations"})
    assert not uncovered, (
        f"以下表不在 tests/conftest.py 的 _TRUNCATE_TABLES 清表名单,"
        f"会造成跨测试脏数据: {sorted(uncovered)}")
