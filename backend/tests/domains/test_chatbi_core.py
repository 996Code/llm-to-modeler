"""chatbi pack 核心模块测试 —— 校验器/自检/自愈/图表引擎/数据源/Skills。

移植对齐锚: 与 chat-bi 的 T030/T032/T033/T034 行为逐点对齐。
跑法(专属库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_core \\
      ./venv/bin/python -m pytest tests/domains/test_chatbi_core.py -q
"""
import json
import os
import uuid

import pytest


# ── SQL 三层校验 (T030) ──────────────────────────────────────

class TestSqlValidator:
    from domains.chatbi.security.sql_validator import validate_sql as _raw_v

    @staticmethod
    def v(sql, allowed=None):
        from domains.chatbi.security.sql_validator import validate_sql
        return validate_sql(sql, allowed)

    def test_select_pass(self):
        r = TestSqlValidator.v("SELECT id, name FROM users WHERE id = 1", {"id", "name"})
        assert r.ok

    def test_write_rejected(self):
        for sql in ("DROP TABLE users", "DELETE FROM users", "UPDATE users SET id = 1",
                    "INSERT INTO users VALUES (1)"):
            r = TestSqlValidator.v(sql)
            assert not r.ok and r.violated_layer == "AST", sql

    def test_multi_statement_rejected(self):
        r = TestSqlValidator.v("SELECT 1; DROP TABLE users")
        assert not r.ok and "多语句" in r.reason

    def test_cte_write_bypass_rejected(self):
        # v1 教训 #46: CTE 内部写操作绕过
        r = TestSqlValidator.v("WITH upd AS (UPDATE users SET id = 1 RETURNING *) SELECT * FROM upd")
        assert not r.ok and "写操作" in r.reason

    def test_dangerous_function_rejected(self):
        for sql in ("SELECT PG_SLEEP(10)", "SELECT DBLINK('host=...')",
                    "SELECT PG_READ_FILE('/etc/passwd')"):
            r = TestSqlValidator.v(sql)
            assert not r.ok and r.violated_layer == "dangerous_function", sql

    def test_into_rejected(self):
        r = TestSqlValidator.v("SELECT * INTO new_users FROM users")
        assert not r.ok and "INTO" in r.reason

    def test_hallucinated_column_rejected(self):
        r = TestSqlValidator.v("SELECT fake_col FROM users", {"id", "name"})
        assert not r.ok and r.violated_layer == "whitelist_column"

    def test_alias_and_cte_derivable_allowed(self):
        sql = ("WITH t AS (SELECT department_id AS dep, COUNT(*) AS cnt FROM emp GROUP BY dep) "
               "SELECT dep, cnt FROM t ORDER BY cnt")
        r = TestSqlValidator.v(sql, {"department_id"})
        assert r.ok  # 别名/CTE 派生名合法

    def test_syntax_error_rejected(self):
        r = TestSqlValidator.v("SELEC * FORM users")
        assert not r.ok and r.violated_layer == "AST"


# ── 结果自检 (T033) ──────────────────────────────────────────

class TestChecker:
    @staticmethod
    def c(rows, columns, sql, max_rows=10000):
        from domains.chatbi.checker import check_result
        return check_result(rows, columns, sql, max_rows=max_rows)

    def test_zero_rows_flagged_not_blocked(self):
        r = TestChecker.c([], ["a"], "SELECT a FROM t")
        assert r.ok and r.issue.value == "ZERO_ROWS"

    def test_all_null_rejected(self):
        rows = [("x", None, None), ("y", None, None)]
        r = TestChecker.c(rows, ["name", "a", "b"], "SELECT ...")
        assert not r.ok and r.issue.value == "ALL_NULL"

    def test_cartesian_rejected(self):
        rows = [(i, i * 2) for i in range(6000)]
        r = TestChecker.c(rows, ["a", "b"], "SELECT ...", max_rows=10000)
        assert not r.ok and r.issue.value == "CARTESIAN_PRODUCT"

    def test_suspicious_zero(self):
        r = TestChecker.c([(0,)], ["cnt"], "SELECT COUNT(*) FROM t")
        assert r.ok and r.issue.value == "SUSPICIOUS_ZERO"

    def test_normal_pass(self):
        assert TestChecker.c([(1, 100)], ["id", "amt"], "SELECT ...").ok


# ── 自愈 (T032) ──────────────────────────────────────────────

class FakeHealLLM:
    def __init__(self, sql_to_return):
        self._sql = sql_to_return
        self.calls = []

    def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
        self.calls.append({"stage": stage, "prompt": messages[-1]["content"]})
        return f"```sql\n{self._sql}\n```", {}


class TestHealer:
    def test_heal_success_passes_validation(self):
        from domains.chatbi.healer import heal_sql, reset_circuit_breaker
        reset_circuit_breaker()
        llm = FakeHealLLM("SELECT id FROM users")
        r = heal_sql(llm, "SELECT idd FROM users",
                     'column "idd" does not exist', {"id"},
                     "users(id BIGINT, name VARCHAR)")
        assert r.success and r.sql == "SELECT id FROM users" and r.rounds == 1
        # SEC S6: 原始 DB 错误不进 prompt(只传类别)
        assert "does not exist" not in llm.calls[0]["prompt"]
        assert "COLUMN_NOT_EXIST" in llm.calls[0]["prompt"]

    def test_heal_output_still_validated(self):
        # v1 教训 #32: 自愈结果走同样三层校验
        from domains.chatbi.healer import heal_sql, reset_circuit_breaker
        reset_circuit_breaker()
        llm = FakeHealLLM("DROP TABLE users")
        r = heal_sql(llm, "SELECT 1", "syntax error", set(), "t(a int)")
        assert not r.success and "校验失败" in (r.error or "")

    def test_circuit_breaker_trips(self):
        from domains.chatbi.healer import SelfHealCircuitBreaker
        cb = SelfHealCircuitBreaker(threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_tripped()
        cb.record_success()
        assert not cb.is_tripped()


# ── 图表引擎 (T034/T035) ─────────────────────────────────────

class TestChartEngine:
    def test_kpi_single_value(self):
        from domains.chatbi.chart_engine import infer_chart_by_rule
        opt = infer_chart_by_rule(["total"], [(1234,)])
        assert opt["chart_type"] == "kpi"

    def test_time_series_line(self):
        from domains.chatbi.chart_engine import infer_chart_by_rule
        rows = [(f"2026-0{i}", 100 + i) for i in range(1, 7)]
        opt = infer_chart_by_rule(["month", "amount"], rows)
        assert opt["series"][0]["type"] == "line"

    def test_pie_low_cardinality(self):
        from domains.chatbi.chart_engine import infer_chart_by_rule
        rows = [(f"城市{i}", 100 - i) for i in range(5)]
        opt = infer_chart_by_rule(["city", "orders"], rows)
        assert opt["series"][0]["type"] == "pie"

    def test_table_recommended(self):
        from domains.chatbi.chart_engine import infer_chart_by_rule
        opt = infer_chart_by_rule(["col"], [(f"r{i}",) for i in range(30)])
        assert opt["chart_type"] == "table"

    def test_heal_json_truncated(self):
        # 原逻辑设计场景: 括号/列表截断(max_tokens 截断的典型形态)
        from domains.chatbi.chart_engine import heal_json
        healed, ok = heal_json('{"chart_type": "bar", "dim_col": "city", '
                              '"measure_cols": ["orders"')
        assert ok
        assert json.loads(healed)["measure_cols"] == ["orders"]

    def test_generate_chart_llm_config_path(self):
        from domains.chatbi.chart_engine import generate_chart

        class LLM:
            def chat(self, messages=None, **kw):
                return ('{"chart_type": "pie", "dim_col": "city", '
                        '"measure_cols": ["orders"]}', {})

        rows = [("北京", 100), ("上海", 80)]
        result = generate_chart(LLM(), "各城市订单", ["city", "orders"], rows)
        assert result.ok and not result.degraded
        assert result.option["series"][0]["type"] == "pie"
        assert result.config["chart_type"] == "pie"  # config 随结果返回(缓存复用)

    def test_generate_chart_fallback_on_garbage(self):
        from domains.chatbi.chart_engine import generate_chart

        class LLM:
            def chat(self, messages=None, **kw):
                return "这不是 JSON", {}

        rows = [("a", 1), ("b", 2)]
        result = generate_chart(LLM(), "q", ["c", "v"], rows)
        assert result.ok and result.degraded  # 规则推断降级, 不返回空

    def test_zero_rows_no_chart(self):
        from domains.chatbi.chart_engine import generate_chart

        class LLM:
            def chat(self, messages=None, **kw):  # pragma: no cover
                raise AssertionError("0 行不应调 LLM")

        result = generate_chart(LLM(), "q", ["c"], [])
        assert not result.ok and result.option is None


# ── 数据源管理(注册表/加密/执行/健康) ─────────────────────────

@pytest.fixture()
def chatbi_db():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import configure_encryption
    from cryptography.fernet import Fernet as _F
    configure_encryption(_F.generate_key().decode())
    db = PackRelationalDB("chatbi")
    from domains.chatbi.datasources import init_store
    init_store(db)
    yield db


class TestDatasources:
    def test_crud_and_password_encryption(self, chatbi_db):
        from domains.chatbi import datasources
        info = datasources.create_datasource(
            chatbi_db, "电商库", "postgresql", "127.0.0.1", 5432,
            "bizdb", "reader", "secret-pass")
        assert info.id and info.is_active
        # 密文落库(非明文)
        with chatbi_db.connect() as conn:
            row = conn.execute("SELECT encrypted_password FROM chatbi_data_sources "
                               "WHERE id = ?", (info.id,)).fetchone()
        assert "secret-pass" not in row["encrypted_password"]
        # 解密往返
        got = datasources.get_datasource(chatbi_db, info.id, decrypt=True)
        assert got.password_plain == "secret-pass"
        # 更新(重加密)与删除
        assert datasources.update_datasource(chatbi_db, info.id, name="电商库2",
                                             password="new-pass")
        got = datasources.get_datasource(chatbi_db, info.id, decrypt=True)
        assert got.name == "电商库2" and got.password_plain == "new-pass"
        assert datasources.delete_datasource(chatbi_db, info.id)

    def test_resolve_fail_closed(self, chatbi_db):
        from domains.chatbi import datasources
        with pytest.raises(ValueError, match="尚无可用数据源"):
            datasources.resolve_datasource(chatbi_db, None)

    def test_execute_readonly_rejects_write(self, chatbi_db):
        # READ ONLY 事务防线:即使校验被绕过也不能写(对 ChatBI sql_executor SEC 对齐)
        import os
        from domains.chatbi import datasources
        dbname = os.environ["DATABASE_URL"].rsplit("/", 1)[-1]
        info = datasources.create_datasource(
            chatbi_db, "本机PG", "postgresql", "127.0.0.1",
            5432, dbname, "root", "root")
        r = datasources.execute_readonly(info, "CREATE TABLE hack_x(id int)")
        assert not r.ok

    def test_execute_select(self, chatbi_db):
        from domains.chatbi import datasources
        # 在测试库建一张表再查(数据源即测试库自身)
        from sdk.relational_store import PackRelationalDB
        with PackRelationalDB("chatbi").connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS public.bi_probe_t(id int, v numeric)")
            conn.execute("TRUNCATE public.bi_probe_t")
            conn.execute("INSERT INTO public.bi_probe_t VALUES (1, 10.5), (2, 20.5)")
        info = datasources.create_datasource(
            chatbi_db, "本机PG", "postgresql", "127.0.0.1",
            5432, os.environ["DATABASE_URL"].rsplit("/", 1)[-1], "root", "root")
        r = datasources.execute_readonly(info, "SELECT SUM(v) AS total FROM public.bi_probe_t")
        assert r.ok and r.rowcount == 1
        assert float(r.rows[0][0]) == 31.0

    def test_health(self, chatbi_db):
        from domains.chatbi import datasources
        info = datasources.create_datasource(
            chatbi_db, "本机PG", "postgresql", "127.0.0.1",
            5432, os.environ["DATABASE_URL"].rsplit("/", 1)[-1], "root", "root")
        h = datasources.check_health(info)
        assert h["healthy"] and "PostgreSQL" in h["server_version"]
        bad = datasources.create_datasource(
            chatbi_db, "坏库", "postgresql", "127.0.0.1", 59999,
            "nope", "x", "y")
        h2 = datasources.check_health(bad)
        assert not h2["healthy"] and h2["error"]


# ── Skills 加载 (SKL-001) ────────────────────────────────────

class TestSkills:
    def test_load_and_dialect_reference(self):
        from domains.chatbi.skills_loader import load_skills_text
        text = load_skills_text("postgresql")
        assert "GMV" in text and "DATE_TRUNC" in text  # 方言 reference 附上
        mysql_text = load_skills_text("mysql")
        assert "DATE_FORMAT" in mysql_text
        plain = load_skills_text(None)
        assert "GMV" in plain and "DATE_TRUNC" not in plain  # 无方言不带 reference
