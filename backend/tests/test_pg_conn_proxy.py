"""_PgConnProxy 参数清洗 —— PG text 不接受 NUL(0x00) 的边界兜底。

单元层用假连接验证代理行为(占位符翻译不回归 + NUL 剥离递归覆盖);
集成层走真实 psycopg 连接,证明带 NUL 的参数不再触发库级错误
(JJF 京 3024-2023 导入失败实测事故:坏字体映射产出 X\x00 进 kg_chunks)。
"""
import pytest

from services.db import _PgConnProxy, _pg_clean_params


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def executemany(self, sql, params_seq, **kwargs):
        self._conn.calls.append((sql, list(params_seq)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return None

    def cursor(self):
        return _FakeCursor(self)


class TestPlaceholderTranslation:
    """占位符翻译边界纪律不回归(误替换曾造成数据损坏,评审修复)。"""

    def test_question_mark_translated_with_params(self):
        conn = _FakeConn()
        _PgConnProxy(conn).execute("INSERT INTO t VALUES (?, ?)", ("a", 1))
        assert conn.calls == [("INSERT INTO t VALUES (%s, %s)", ("a", 1))]

    def test_no_params_leaves_sql_untouched(self):
        conn = _FakeConn()
        _PgConnProxy(conn).execute("SELECT 1 WHERE c = 'a?b'")
        assert conn.calls == [("SELECT 1 WHERE c = 'a?b'", None)]


class TestNulStripping:
    def test_execute_strips_nul_from_str(self):
        conn = _FakeConn()
        _PgConnProxy(conn).execute(
            "INSERT INTO kg_chunks (text) VALUES (?)", ("X\x00——均值",))
        sql, params = conn.calls[0]
        assert params == ("X——均值",)

    def test_clean_param_keeps_bytes_and_scalars(self):
        assert _pg_clean_params(b"a\x00b") == b"a\x00b"   # bytes 原样(bytea 合法)
        assert _pg_clean_params(42) == 42
        assert _pg_clean_params(None) is None

    def test_clean_params_recursive_containers(self):
        src = {"name": "a\x00b", "tags": ["x\x00y", ("z\x00w", 1)], "n": 3}
        out = _pg_clean_params(src)
        assert out == {"name": "ab", "tags": ["xy", ("zw", 1)], "n": 3}
        # list 元素仍是 list,tuple 仍是 tuple(类型不被摊平)
        assert isinstance(out["tags"], list) and isinstance(out["tags"][1], tuple)

    def test_clean_params_zero_copy_when_clean(self):
        s = "干净文本"
        assert _pg_clean_params(s) is s   # 无 NUL 时不构造新串

    def test_clean_params_lone_surrogate_replaced(self):
        """配对代理合并为真实字符,孤立代理替换为 U+FFFD。"""
        assert _pg_clean_params("a\ud835b") == "a\ufffdb"
        assert _pg_clean_params("\ud835\udc00") == "𝐀"

    def test_executemany_strips_each_row(self):
        conn = _FakeConn()
        _PgConnProxy(conn).executemany(
            "INSERT INTO t VALUES (?)", [("a\x00b",), ("c",)])
        sql, rows = conn.calls[0]
        assert rows == [("ab",), ("c",)]


class TestNulRoundTripLivePg:
    """真实 psycopg 路径:带 NUL 参数写入/读回不再抛库级错误。"""

    def test_nul_param_roundtrip(self):
        from services.db import get_pg_engine
        engine = get_pg_engine()
        with engine.connect() as conn:
            row = conn.execute(
                "SELECT ? AS t", ("X\x00——均值",)).fetchone()
            assert row["t"] == "X——均值"

    def test_lone_surrogate_param_roundtrip(self):
        from services.db import get_pg_engine
        engine = get_pg_engine()
        with engine.connect() as conn:
            row = conn.execute(
                "SELECT ? AS t", ("X\ud835——均值",)).fetchone()
            assert row["t"] == "X\ufffd——均值"
