"""Only explicit MCP bindings are accepted by the current runtime."""
import pytest

from domains.metrology_assistant.capability_import import _action_identity
from domains.metrology_assistant.capability_plan import prepare_plan, resolve_datasource
from domains.metrology_assistant.scenario_config import ConfigValidationError, validate_config


def config(action):
    return {'scenarios': {'custom': {
        'label': 'Custom', 'intent': 'lookup', 'required': {},
        'action': {'kind': 'chatbi', 'tool': 'lookup', 'params': {},
                   'card': '_table_card', 'notices': {}, **action}}}}


@pytest.mark.parametrize('value', ['', None, 'OLD_BINDING'])
@pytest.mark.parametrize('strict', [False, True])
def test_config_rejects_even_empty_env_keys(value, strict):
    with pytest.raises(ConfigValidationError, match='env'):
        validate_config(config({'server': 'metrology', 'env': value}), strict=strict)


def test_environment_cannot_supply_capability_identity(monkeypatch):
    monkeypatch.setenv('OLD_BINDING', 'old-ds')
    assert _action_identity({'env': 'OLD_BINDING', 'tool': 'lookup'}, {'old-ds': 'metrology'}) == ('', 'lookup')


def test_environment_cannot_supply_execution_binding(monkeypatch):
    monkeypatch.setenv('OLD_BINDING', 'old-ds')
    with pytest.raises(ValueError, match='env'):
        resolve_datasource({'datasource_env': 'OLD_BINDING', 'tool': 'lookup'})


def test_plan_rejects_env_instead_of_projecting_it():
    cfg = config({'env': 'OLD_BINDING'})['scenarios']['custom']
    assert 'env' in prepare_plan(cfg, {}).get('error', '')


def test_default_demo_has_only_explicit_server_tool_bindings():
    from domains.metrology_assistant.legacy_demo import demo_scenarios
    for cfg in demo_scenarios().values():
        action = cfg['action']
        assert 'env' not in action
        if action['kind'] == 'chatbi':
            assert action['server'] == 'metrology'
            assert action['tool']
