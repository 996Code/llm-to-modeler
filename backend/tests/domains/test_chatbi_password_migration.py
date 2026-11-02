"""Plain password CRUD, legacy DDL and secret redaction contracts."""
import uuid

import pytest

from domains.chatbi import datasources, runtime
from domains.chatbi.models import CHATBI_DDL, DataSourceInfo
from sdk.relational_store import PackRelationalDB


@pytest.fixture
def legacy_db():
    db = PackRelationalDB("chatbi_pw_" + uuid.uuid4().hex)
    db.init_schema(CHATBI_DDL)
    with db.connect() as conn:
        conn.execute("ALTER TABLE chatbi_data_sources RENAME COLUMN password TO encrypted_password")
        conn.execute("ALTER TABLE chatbi_data_sources ALTER COLUMN encrypted_password DROP DEFAULT")
    yield db
    with db.engine.connect() as conn:
        conn.execute(f'DROP SCHEMA "{db.schema}" CASCADE')


def test_password_contract_without_key(monkeypatch):
    monkeypatch.delenv("FERNET_KEY", raising=False)
    db = PackRelationalDB("chatbi")
    info = datasources.create_datasource(db, "secret test", "postgresql", "localhost", 5432, "db", "user", "private-value")
    assert info.password == "private-value"
    assert "private-value" not in repr(info)
    with db.connect() as conn:
        assert conn.execute("SELECT password FROM chatbi_data_sources WHERE id = ?", (info.id,)).fetchone()["password"] == "private-value"
    assert datasources.get_datasource(db, info.id).password == "private-value"
    assert datasources.list_datasources(db)[0].password == "private-value"
    datasources.update_datasource(db, info.id, name="new name")
    assert datasources.get_datasource(db, info.id).password == "private-value"
    datasources.update_datasource(db, info.id, password="replacement")
    assert datasources.resolve_datasource(db, info.id).password == "replacement"
    from domains.chatbi.api import _info_to_dict
    assert "password" not in _info_to_dict(info)
    assert "private-value" not in str(_info_to_dict(info))


@pytest.mark.parametrize("initialize", [runtime._init_pack_schema, datasources.init_store])
def test_old_schema_adds_password_preserves_rows_and_allows_new_insert(legacy_db, initialize):
    with legacy_db.connect() as conn:
        conn.execute("INSERT INTO chatbi_data_sources (id,name,db_type,host,port,database,username,encrypted_password,is_active,created_at,updated_at) VALUES ('broken','old','postgresql','localhost',5432,'db','user','not-plaintext',0,'now','now')")
    initialize(legacy_db)
    initialize(legacy_db)
    with legacy_db.connect() as conn:
        row = conn.execute("SELECT encrypted_password,password,is_active FROM chatbi_data_sources WHERE id='broken'").fetchone()
    assert dict(row) == {"encrypted_password": "not-plaintext", "password": "", "is_active": 0}
    info = datasources.get_datasource(legacy_db, "broken")
    assert info.password == "" and info.is_active is False
    with pytest.raises(ValueError, match="停用"):
        datasources.resolve_datasource(legacy_db, "broken")
    created = datasources.create_datasource(legacy_db, "new", "postgresql", "localhost", 5432, "db", "user", "new-secret")
    assert created.password == "new-secret"
    with legacy_db.connect() as conn:
        row = conn.execute("SELECT encrypted_password,password FROM chatbi_data_sources WHERE id=?", (created.id,)).fetchone()
    assert dict(row) == {"encrypted_password": "", "password": "new-secret"}


def test_old_ciphertext_column_is_never_selected(legacy_db, monkeypatch):
    from contextlib import contextmanager
    runtime._init_pack_schema(legacy_db)
    created = datasources.create_datasource(legacy_db, "new", "mcp", "metrology", 0, "", "", "")
    connect = legacy_db.connect

    @contextmanager
    def checked_connect():
        with connect() as conn:
            class Checked:
                def execute(self, query, params=()):
                    assert "SELECT *" not in query.upper()
                    assert "encrypted_password" not in query
                    return conn.execute(query, params)
            yield Checked()

    monkeypatch.setattr(legacy_db, "connect", checked_connect)
    assert datasources.get_datasource(legacy_db, created.id).id == created.id
    assert datasources.list_datasources(legacy_db)[0].id == created.id


@pytest.fixture()
def pg_engine():
    from sdk.relational_store import PackRelationalDB
    db = PackRelationalDB("chatbi")
    runtime._init_pack_schema(db)
    return db


def test_scan_failure_error_and_state_do_not_leak_password(
        pg_engine, monkeypatch):
    """业务库扫描异常:任务错误与 scan_error 均不得包含数据源密码。"""
    from datetime import datetime, timezone
    from domains.chatbi import semantic, tasks

    secret = "never-print-this"
    ds = "scan-leak-ds"
    now = datetime.now(timezone.utc).isoformat()
    with pg_engine.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_data_sources "
            "(id, name, db_type, host, port, database, username, "
            "password, is_active, created_at, updated_at) "
            "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', ?, 1, ?, ?)",
            (ds, secret, now, now))

    def _boom(**kw):
        raise RuntimeError(f"connection failed: password={secret} host=h")
    monkeypatch.setattr("domains.chatbi.semantic.scan_datasource", _boom)
    monkeypatch.setattr("domains.chatbi.tasks.RunLease.acquire",
                        lambda self: True)
    monkeypatch.setattr("domains.chatbi.tasks.RunLease.release",
                        lambda self: None)
    monkeypatch.setattr("domains.chatbi.tasks.RunLease.heartbeat",
                        lambda self: True)
    monkeypatch.setattr("domains.chatbi.tasks.RunLease.assert_owned",
                        lambda self: True)

    class _H:
        task_id = "scan-leak-handle"
        payload = {"datasource_id": ds}

        def log(self, msg, **kw):
            pass

        def set_progress(self, *a, **k):
            pass

    import types
    with pytest.raises(Exception) as ei:
        tasks._task_scan_datasource(_H(), types.SimpleNamespace(llm_client=None))
    assert secret not in str(ei.value)
    with pg_engine.connect() as conn:
        row = conn.execute(
            "SELECT scan_error FROM chatbi_data_sources WHERE id = ?",
            (ds,)).fetchone()
    assert secret not in (row["scan_error"] or "")


def test_empty_password_remains_valid_for_runtime_and_mcp():
    db = PackRelationalDB("chatbi")
    for kind in ("postgresql", "mcp"):
        info = datasources.create_datasource(db, kind, kind, "localhost", 5432, "db", "user", "")
        assert datasources.resolve_datasource(db, info.id).password == ""


def test_driver_errors_do_not_leak_password(monkeypatch, caplog):
    info = DataSourceInfo(password="never-print-this")
    def fail(*args, **kwargs):
        raise RuntimeError("password=never-print-this host=localhost")
    monkeypatch.setattr(datasources, "_pg_connect", fail)
    result = datasources.execute_readonly(info, "SELECT 1")
    health = datasources.check_health(info)
    assert "never-print-this" not in result.error
    assert "never-print-this" not in str(health)
    assert "never-print-this" not in caplog.text
