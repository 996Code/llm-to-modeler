"""P1#5 评测集扩容 —— EX 执行口径 + 分层报告。

fixture_seed.sql（SQLite 六表种子）与 evalset.json（60 条）的自洽性、
EX 结果集比对（容错列序）、MRR/难度分层报告。
"""
import json
import sqlite3
from pathlib import Path

TESTDATA = Path(__file__).parent / "testdata" / "chatbi"
EVALSET = json.loads((TESTDATA / "evalset.json").read_text(encoding="utf-8"))


def make_sqlite_fixture() -> sqlite3.Connection:
    """内存 SQLite:执行 fixture_seed.sql 的 DDL+种子。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript((TESTDATA / "fixture_seed.sql").read_text(encoding="utf-8"))
    return conn


class TestFixtureSanity:

    def test_all_expected_sql_execute_and_nonempty(self):
        """每条 expected_sql 在 fixture 上可执行且结果集非空(防恒真比对)。"""
        conn = make_sqlite_fixture()
        for case in EVALSET:
            rows = conn.execute(case["expected_sql"]).fetchall()
            assert rows, f"{case['id']} expected_sql 结果集为空"

    def test_evalset_size_and_layers(self):
        from collections import Counter
        assert len(EVALSET) == 60
        c = Counter(x["difficulty"] for x in EVALSET)
        # h007 难度校正 hard->medium（id 稳定不改）：easy25/medium26/hard9
        assert c == {"easy": 25, "medium": 26, "hard": 9}

    def test_skeleton_equals_expected_sql(self):
        """60 条 expected_sql_skeleton 与 expected_sql 全等（T1 审查补）。

        两者口径本应一致（skeleton 供表名集断言、sql 供 EX 执行），
        任何一处单独漂移都会使骨架断言与结果集比对失配。
        """
        for case in EVALSET:
            assert case["expected_sql_skeleton"] == case["expected_sql"], case["id"]

    def test_expected_tables_subset_of_six(self):
        six = {"employee", "department", "salary", "orders", "customer", "product"}
        for case in EVALSET:
            assert set(case["expected_tables"]) <= six, case["id"]
