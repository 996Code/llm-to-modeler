"""SQL 契约共享模块测试：三契约+白名单+宏+占位符+XML转义（纯函数零上下文）。"""
from domains.njmind_form.tools._sql_contract import (
    check_sql_fragment, xml_escape_sql,
)

COLS = {"keys": {"create_time", "status"}, "text": "", "empty": False}


def test_check_sql_相对年份禁字面量():
    errs = check_sql_fragment("EXTRACT(YEAR FROM create_time) = '2026-01-01'",
        target_columns=COLS, own_keys=set(), is_sub_table=False, user_text="只看当年的数据")
    assert any("年份字面量" in e for e in errs)


def test_check_sql_禁表别名前缀():
    errs = check_sql_fragment("t.status = 1", target_columns=COLS,
        own_keys=set(), is_sub_table=False, user_text="")
    assert any("表别名" in e for e in errs)


def test_check_sql_白名单外列名():
    errs = check_sql_fragment("ghost_col = 1", target_columns=COLS,
        own_keys=set(), is_sub_table=False, user_text="")
    assert any("白名单外" in e for e in errs)


def test_xml_escape_幂等且保留占位符():
    once = xml_escape_sql("a < 10 AND #{k} > '&amp;'")
    assert once == xml_escape_sql(once) and "#{k}" in once and "&lt;" in once
