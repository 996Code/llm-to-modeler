"""chatbi 查询质量统计测试 —— 写入/聚合/慢查询 + 阈值可配。

复核报告 P1 覆盖:
  - record_query 三路径字段落库(ok/error/ask 语义位);
  - datasource_metrics 聚合: 查询数/平均/最大耗时/错误率/慢查询数
    (阈值参与判定, 不落列——改阈值历史口径重算)/自愈/降级/澄清计数;
  - slow_queries: 超阈值按耗时倒序, limit 生效;
  - fail-open: 必填缺失/存储异常不抛(返回 None)。

跑法(专属库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_qstats \\
      ./venv/bin/python -m pytest tests/domains/test_chatbi_query_stats.py -q
"""
import pytest

from domains.chatbi.query_stats import (
    QUERY_STATS_DDL,
    datasource_metrics,
    record_query,
    slow_queries,
)


@pytest.fixture()
def db():
    from sdk.relational_store import PackRelationalDB
    d = PackRelationalDB("chatbi")
    d.init_schema(list(QUERY_STATS_DDL))
    yield d


def _seed(db):
    """ds1: 4 条(ok×2/慢/错误+自愈+降级) ds2: 1 条。"""
    record_query(db, data_source_id="ds1", status="ok", duration_ms=100,
                 row_count=5, question="快查询")
    record_query(db, data_source_id="ds1", status="ok", duration_ms=300,
                 row_count=8, question="普通查询", retrieval_degraded=1)
    record_query(db, data_source_id="ds1", status="ok", duration_ms=12000,
                 row_count=100, question="慢查询", heal_rounds=2,
                 chart_degraded=1)
    record_query(db, data_source_id="ds1", status="error", duration_ms=500,
                 error_message="boom", question="失败查询")
    record_query(db, data_source_id="ds1", status="ask", question="澄清",
                 asked_user=1)
    record_query(db, data_source_id="ds2", status="ok", duration_ms=50)


class TestRecord:
    def test_fields_persisted(self, db):
        rid = record_query(db, data_source_id="d", status="ok",
                           user_id="u1", conv_id="c1", question="q",
                           sql_text="SELECT 1", duration_ms=42,
                           row_count=3, heal_rounds=1,
                           retrieval_degraded=1, chart_degraded=1,
                           metric_hits=2)
        assert rid
        with db.connect() as conn:
            row = dict(conn.execute(
                "SELECT * FROM chatbi_query_stats WHERE id = ?", (rid,)).fetchone())
        assert row["status"] == "ok" and row["duration_ms"] == 42
        assert row["retrieval_degraded"] == 1 and row["metric_hits"] == 2

    def test_missing_required_returns_none(self, db):
        assert record_query(db, status="ok") is None          # 无 ds
        assert record_query(db, data_source_id="d") is None   # 无 status


class TestMetrics:
    def test_aggregation_per_datasource(self, db):
        _seed(db)
        rows = {r["data_source_id"]: r for r in datasource_metrics(db)}
        m1 = rows["ds1"]
        assert m1["query_count"] == 5
        assert m1["error_count"] == 1
        assert m1["error_rate"] == 20.0
        assert m1["avg_ms"] == round((100 + 300 + 12000 + 500) / 4)
        assert m1["max_ms"] == 12000
        assert m1["slow_count"] == 1                # 仅 12s > 10s 阈值
        assert m1["heal_count"] == 1
        assert m1["retrieval_degraded_count"] == 1
        assert m1["chart_degraded_count"] == 1
        assert m1["ask_user_count"] == 1
        assert rows["ds2"]["query_count"] == 1

    def test_slow_threshold_configurable(self, db):
        _seed(db)
        # 阈值收紧到 400ms → 12s 与 500ms 两条都算慢
        m = datasource_metrics(db, slow_ms=400)[0]
        assert m["slow_count"] == 2

    def test_ds_filter_with_days_window(self, db):
        """ds + days 真组合(五审十.3: 上一版两个断言分别只传了其中一个)。"""
        from domains.chatbi.query_stats import record_query
        # ds1 新旧各一条, ds2 一条新的
        rid_old = record_query(db, data_source_id="ds1", status="ok",
                               duration_ms=20000)
        with db.connect() as conn:
            conn.execute(
                "UPDATE chatbi_query_stats SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", rid_old))
        record_query(db, data_source_id="ds1", status="ok", duration_ms=9000)
        record_query(db, data_source_id="ds2", status="ok", duration_ms=100)
        # ★ 同一次调用同时传 ds + days(参数错位时此处恒空)
        only1_7d = datasource_metrics(db, data_source_id="ds1", days=7)
        assert len(only1_7d) == 1
        assert only1_7d[0]["data_source_id"] == "ds1"
        assert only1_7d[0]["query_count"] == 1   # 过期那条不进窗口
        # ds1 全历史 = 2
        only1_all = datasource_metrics(db, data_source_id="ds1")
        assert only1_all[0]["query_count"] == 2
        # 仅 days(全库)
        rows7 = {r["data_source_id"]: r for r in datasource_metrics(db, days=7)}
        assert rows7["ds2"]["query_count"] == 1

    def test_single_ds_filter(self, db):
        _seed(db)
        rows = datasource_metrics(db, data_source_id="ds2")
        assert len(rows) == 1 and rows[0]["query_count"] == 1

    def test_empty(self, db):
        assert datasource_metrics(db) == []


class TestSlowQueries:
    def test_order_and_threshold(self, db):
        _seed(db)
        items = slow_queries(db, "ds1", slow_ms=1000)
        assert [i["duration_ms"] for i in items] == [12000]
        # 阈值 100 → 12000/500/300 三条, 按耗时倒序
        items2 = slow_queries(db, "ds1", slow_ms=100)
        assert [i["duration_ms"] for i in items2] == [12000, 500, 300]

    def test_limit(self, db):
        for i in range(5):
            record_query(db, data_source_id="d", status="ok",
                         duration_ms=20000 + i, question=f"q{i}")
        assert len(slow_queries(db, "d", slow_ms=1000, limit=3)) == 3
