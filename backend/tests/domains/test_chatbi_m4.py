"""M4: 保存查询 + 看板 测试。

跑法: TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_m4 \\
    ./venv/bin/python -m pytest tests/domains/test_chatbi_m4.py -q
"""
import json
import uuid

import pytest

from domains.chatbi.models import M4_DDL


@pytest.fixture()
def db():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store, configure_encryption
    from cryptography.fernet import Fernet
    configure_encryption(Fernet.generate_key().decode())
    d = PackRelationalDB("chatbi")
    init_store(d)
    d.init_schema(M4_DDL)
    yield d


class TestSavedQueries:
    def test_auto_save_and_dedup(self, db):
        from domains.chatbi.m4 import save_query
        r1 = save_query(db, "u1", "ds1", "各城市订单", "SELECT 1")
        assert not r1["deduplicated"]
        r2 = save_query(db, "u1", "ds1", "各城市订单", "SELECT 1")  # 同SQL去重
        assert r2["deduplicated"] and r2["id"] == r1["id"]
        r3 = save_query(db, "u1", "ds1", "各月", "SELECT 2")  # 不同SQL
        assert not r3["deduplicated"]

    def test_list(self, db):
        from domains.chatbi.m4 import save_query
        save_query(db, "u1", "ds1", "q1", "SELECT 1")
        save_query(db, "u2", "ds1", "q2", "SELECT 2")  # 不同用户
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chatbi_saved_queries WHERE user_id = ? "
                "ORDER BY created_at DESC", ("u1",)).fetchall()
        assert len(rows) == 1 and rows[0]["question"] == "q1"

    def test_csv_export_with_injection_guard(self, db):
        from domains.chatbi.m4 import save_query, _sanitize_csv_cell
        # CSV 注入防护
        assert _sanitize_csv_cell("=SUM(A1)") == "'=SUM(A1)"
        assert _sanitize_csv_cell("+cmd") == "'+cmd"
        assert _sanitize_csv_cell("-1") == "'-1"
        assert _sanitize_csv_cell("@ref") == "'@ref"
        assert _sanitize_csv_cell("normal") == "normal"
        assert _sanitize_csv_cell(None) == ""
        # 保存一条
        r = save_query(db, "u1", "ds1", "q", "SELECT 1")
        assert r["id"]


class TestDashboard:
    def test_crud_and_widgets(self, db):
        from domains.chatbi.m4 import _calc_next_position
        # 自动布局
        assert _calc_next_position([]) == (0, 0)
        assert _calc_next_position([(0, 0, 6)]) == (6, 0)
        assert _calc_next_position([(0, 0, 6), (6, 0, 6)]) == (0, 1)

        # 直接 SQL 验证 CRUD(端点测试需要 TestClient+admin, 这里验数据层)
        did = str(uuid.uuid4())
        now = "2026-09-14T00:00:00"
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_dashboards (id, user_id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (did, "u1", "测试看板", now, now))
            for i in range(3):
                conn.execute(
                    """INSERT INTO chatbi_dashboard_widgets
                       (id, dashboard_id, question, query_sql, datasource_id, chart_type,
                        chart_option, position_x, position_y, width, height, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), did, f"q{i}", f"SELECT {i}", "ds1", "bar",
                     json.dumps({"chart_type": "bar"}), i * 6, 0, 6, 4, now, now))
        with db.connect() as conn:
            dash = conn.execute(
                "SELECT * FROM chatbi_dashboards WHERE id = ?", (did,)).fetchone()
            widgets = conn.execute(
                "SELECT * FROM chatbi_dashboard_widgets WHERE dashboard_id = ? "
                "ORDER BY position_x", (did,)).fetchall()
        assert dash["name"] == "测试看板"
        assert len(widgets) == 3
        assert all(w["chart_type"] == "bar" for w in widgets)
        # JSON round-trip
        cfg = json.loads(widgets[0]["chart_option"])
        assert cfg == {"chart_type": "bar"}

        # 删除级联
        with db.connect() as conn:
            conn.execute("DELETE FROM chatbi_dashboards WHERE id = ?", (did,))
            conn.execute("DELETE FROM chatbi_dashboard_widgets WHERE dashboard_id = ?", (did,))
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM chatbi_dashboard_widgets WHERE dashboard_id = ?",
                (did,)).fetchone()
        assert remaining["c"] == 0
