"""P1#5 评测集扩容 —— EX 执行口径 + 分层报告。

fixture_seed.sql（SQLite 六表种子）与 evalset.json（60 条）的自洽性、
EX 结果集比对（容错列序）、MRR/难度分层报告。
"""
import json
import sqlite3
from collections import Counter
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


def compare_result_sets(exp_rows, exp_cols, got_rows, got_cols) -> bool:
    """EX 比对（子集投影语义，T1 修复环口径）。

    期望列名在生成侧全列（替身 SELECT *）中按名取值投影后，做行多重集
    的**子多重集**比较：每个期望投影行的出现次数 ≤ 生成侧同投影行的
    出现次数即 True。
    - 期望 SQL 的 WHERE/LIMIT/DISTINCT 等过滤只会筛掉生成侧的行、
      不会凭空造行 → 替身无过滤的 SELECT * 全行集应包含期望行集；
    - 期望列在生成侧缺失 → False（列对不上 = 生成错了表或列）；
    - 整行类（expected_columns=[] 的全输出列比对）走同一函数：聚合
      列（如 COUNT(*)）名在 SELECT * 全列中对不上即 False——如实暴露
      "替身无聚合生成能力"的口径边界，非链路断裂。
    """
    try:
        idx = [got_cols.index(c) for c in exp_cols]
    except ValueError:
        return False
    exp_multiset = Counter(
        tuple(repr(r[i]) for i in range(len(exp_cols))) for r in exp_rows)
    got_multiset = Counter(
        tuple(repr(r[i]) for i in idx) for r in got_rows)
    return all(exp_multiset[k] <= got_multiset[k] for k in exp_multiset)


class TestExecutionAccuracy:

    def test_compare_result_sets_column_order_tolerant(self):
        """列名对齐（容错列序）+行多重集比较——DB-GPT EX 口径。

        三断言：列序不同但同名列值对齐→True；列同值不同→False；
        行序不同（多重集）→True；期望行是生成行子集（替身无 WHERE
        场景）→True；期望列生成侧缺失→False。
        """
        assert compare_result_sets(
            [("a", 1), ("b", 2)], ["name", "cnt"],
            [(1, "a"), (2, "b")], ["cnt", "name"]) is True
        assert compare_result_sets(
            [("a", 1)], ["name", "cnt"],
            [("a", 2)], ["name", "cnt"]) is False
        assert compare_result_sets(  # 行序不敏感（多重集）
            [("a", 1), ("b", 2)], ["name", "cnt"],
            [("b", 2), ("a", 1)], ["name", "cnt"]) is True
        assert compare_result_sets(  # 期望行为生成行子集（替身无过滤）
            [("a", 1)], ["name", "cnt"],
            [("a", 1), ("b", 2)], ["name", "cnt"]) is True
        assert compare_result_sets(  # 期望列生成侧缺失
            [("a", 1)], ["name", "cnt"],
            [("a", 1)], ["name", "other"]) is False

    def test_evalset_ex_baseline(self, capsys):
        """EX 基线（双口径，T1 修复环裁决）：

        a. EX-proj（expected_columns 非空的投影类）：替身生成 SELECT *
           JOIN 链 → 期望列按名投影比对——链路连通性口径，断言
           ex_hits >= checked - 3；
        b. 整行类（expected_columns=[] 的 31 条）：替身不生成聚合，
           聚合输出列名在 SELECT * 全列中对不上→整行比对必然 False——
           如实报告失配数，**只打印口径说明不设断言**（失配=替身无聚合
           生成能力，非链路断裂；真生成质量留真 LLM 评测）。
        """
        from domains.chatbi.retrieval import retrieve
        from tests.domains.test_chatbi_evalset import (
            make_eval_fixture, register_query_vector, SqlGeneratingFakeLLM)

        store, embedder, llm = make_eval_fixture()
        llm.__class__ = SqlGeneratingFakeLLM
        llm.last_generated_sql = ""
        conn = make_sqlite_fixture()
        proj_stats = {"hits": 0, "checked": 0}
        full_stats = {"hits": 0, "checked": 0}
        for case in EVALSET:
            register_query_vector(llm, case["query"], case["expected_tables"])
            result = retrieve(case["query"], store, embedder, llm=llm,
                              scope="44444444-4444-4444-4444-444444444444")
            if not result.models:
                continue
            stats = (proj_stats if case.get("expected_columns")
                     else full_stats)
            stats["checked"] += 1
            cur = conn.execute(llm.last_generated_sql)
            got_rows, got_cols = cur.fetchall(), [d[0] for d in cur.description]
            exp_cur = conn.execute(case["expected_sql"])
            exp_rows = exp_cur.fetchall()
            exp_cols = [d[0] for d in exp_cur.description]
            stats["hits"] += compare_result_sets(
                exp_rows, exp_cols, got_rows, got_cols)
        print(f"\n=== EX-proj（投影类） {proj_stats['hits']}/{proj_stats['checked']} ===")
        print(f"=== 整行类 {full_stats['hits']}/{full_stats['checked']} ===")
        print("整行类失配=替身无聚合生成能力（SELECT * 不产出聚合列），"
              "非链路问题；真生成质量留真 LLM 评测")
        assert proj_stats["checked"] + full_stats["checked"] >= 55, (
            f"未召回用例过多: checked={proj_stats['checked']}"
            f"+{full_stats['checked']}")
        assert proj_stats["hits"] >= proj_stats["checked"] - 3, (
            f"EX-proj {proj_stats['hits']}/{proj_stats['checked']} "
            f"显著低于链路连通预期")
