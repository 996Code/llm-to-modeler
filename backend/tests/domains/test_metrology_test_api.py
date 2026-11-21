"""Read-only metrology diagnostics: real store contracts and safe failures."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.metrology_assistant import api

URL = "/api/packs/metrology_assistant/test/env"


@pytest.fixture(autouse=True)
def _fresh_pack():
    from domains.metrology_assistant import runtime
    runtime.reset_runtime_cache()
    yield
    runtime.reset_runtime_cache()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/packs/metrology_assistant")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"},
                                    {"X-User-Id": "nobody"}])
def test_env_rejects_unauthorized_before_reading_dependencies(env, monkeypatch, headers):
    from domains.knowledge_graph import runtime as kg_runtime
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    def must_not_read(*args):
        pytest.fail("unauthorized request reached diagnostics dependencies")
    monkeypatch.setattr(kg_runtime, "get_kg_store", must_not_read)
    assert env.get(URL, headers=headers).status_code == 401


def test_empty_diagnostics_does_not_invent_demo_resources(env, monkeypatch):
    from domains.metrology_assistant.tools import assistant
    monkeypatch.setattr(assistant, '_runtime_scenarios', lambda: {})
    response = env.get(URL)
    assert response.status_code == 200
    assert response.json()['kbs'] == []
    assert response.json()['kbMapping'] == {}
    # 空场景表不虚构库/映射/单号夹具
    assert response.json()['orderFixtures'] == []


def test_env_accepts_valid_admin_token(env, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    response = env.get(URL, headers={"X-Admin-Token": "test-admin-token"})
    assert response.status_code == 200
    assert "kbMapping" in response.json()


def test_order_fixtures_survive_askoption_rehydration(env, monkeypatch):
    """存量场景经 load_scenarios 装载后 options 是 AskOption 对象——
    夹具提取必须双形状兼容(通用化回归:dict-only 过滤把 BJ2026 全滤掉)。"""
    from domains.metrology_assistant import store, runtime
    from domains.metrology_assistant.scenario_config import load_scenarios
    from tests.domains.metrology_fixtures import configured_scenarios
    content = {'scenarios': configured_scenarios()}
    store.seed_config_if_absent(runtime.get_pack_db(), content)
    monkeypatch.setattr(api.cfg, '_runtime_scenarios', load_scenarios)
    response = env.get(URL)
    assert response.status_code == 200
    orders = [f['order'] for f in response.json()['orderFixtures']]
    assert 'BJ2026' in orders and 'BJ2027' in orders


def test_env_kg_read_failure_is_reported(env, monkeypatch):
    """KG 状态读取失败(P3 覆盖回补):errors 如实上报,响应仍 200。"""
    from domains.knowledge_graph import runtime as kg_runtime
    from domains.metrology_assistant.tools import assistant

    table = {'强检判定': {'label': '强检判定', 'intent': '问强检',
                          'required': {}, 'action': {'kind': 'kg', 'kb': '强检目录'}}}
    monkeypatch.setattr(assistant, '_runtime_scenarios', lambda *a, **k: table)

    def boom(state):
        raise RuntimeError('kg down')

    monkeypatch.setattr(kg_runtime, 'get_kg_store', boom)
    response = env.get('/api/packs/metrology_assistant/test/env')
    assert response.status_code == 200
    body = response.json()
    assert body['errors'] == ['知识库状态读取失败,请查看服务日志']
    # store 获取在逐库循环之前:失败时 kbs 保持空(不虚构库状态行)
    assert body['kbs'] == []
