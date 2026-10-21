"""mcp 类型数据源: CRUD 全链路 + 密码空串兼容 + resolve 可选中。

契约: db_type="mcp", host=server code, database=tool_name(可空), port=0,
username/encrypted_password=空串——密码空串跳过 Fernet 直存(无凭据可加密)。

跑法: cd backend && .venv/bin/python -m pytest tests/domains/test_chatbi_mcp_datasource.py -v
"""
import pytest

from domains.chatbi.datasources import (
    create_datasource,
    get_datasource,
    list_datasources,
    resolve_datasource,
    update_datasource,
)


@pytest.fixture()
def db():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store
    d = PackRelationalDB("chatbi")
    init_store(d)
    yield d


def _create_mcp(db, name="外部报表", host="srv_a"):
    return create_datasource(db, name=name, db_type="mcp", host=host,
                             port=0, database="", username="", password="")


def test_create_and_get_mcp(db):
    info = _create_mcp(db)
    assert info.db_type == "mcp" and info.host == "srv_a"
    got = get_datasource(db, info.id, decrypt=True)
    assert got.db_type == "mcp" and got.host == "srv_a"
    # 密码空串直存: 不经 Fernet, 解密往返仍为空串
    assert got.encrypted_password == ""
    assert got.password_plain == ""


def test_mcp_requires_server_code(db):
    with pytest.raises(ValueError):
        create_datasource(db, name="坏", db_type="mcp", host="",
                          port=0, database="", username="", password="")


def test_mcp_in_list_and_resolve(db):
    info = _create_mcp(db)
    names = [i.name for i in list_datasources(db)]
    assert "外部报表" in names
    resolved = resolve_datasource(db, None)
    assert resolved is not None and resolved.db_type == "mcp"
    assert resolved.id == info.id


def test_update_mcp_password_empty_stays_plain(db):
    info = _create_mcp(db)
    assert update_datasource(db, info.id, name="外部报表2", password="")
    got = get_datasource(db, info.id, decrypt=True)
    assert got.name == "外部报表2" and got.encrypted_password == ""


def test_update_rejects_unknown_db_type(db):
    info = _create_mcp(db)
    with pytest.raises(ValueError):
        update_datasource(db, info.id, db_type="oracle")
