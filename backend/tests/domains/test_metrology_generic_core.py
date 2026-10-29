"""Generic capability execution contracts, independent from demo seeds."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from domains.chatbi.controlled import ControlledResult
from domains.metrology_assistant import runtime, store
from domains.metrology_assistant.tools import assistant as mta


class Mem:
    def __init__(self):
        self.data = {}
    def get(self, key, default=None):
        return self.data.get(key, default)
    def set(self, key, value):
        self.data[key] = value
    def pop(self, key, default=None):
        return self.data.pop(key, default)


class LLM:
    def __init__(self, data):
        self.data, self.prompts = data, []
    def chat_json(self, messages, **kwargs):
        self.prompts.append((kwargs.get('stage'), messages[0]['content']))
        return deepcopy(self.data)


def capability(properties=None, required=None):
    return {'label': 'Stock lookup', 'intent': 'look up stock', 'required': {},
            'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'stock',
                       'datasource_id': 'ds-stock', 'params': {k: k for k in (properties or {})},
                       'input_schema': {'type': 'object', 'properties': properties or {},
                                        'required': required or []}, 'card': '_table_card'}}


def context(data, conv='generic-core'):
    return SimpleNamespace(llm_client=LLM(data), session_state=Mem(), conv_id=conv,
                           user_id='alice', emit=lambda *a: None)


@pytest.fixture(autouse=True)
def clean_runtime():
    runtime.reset_runtime_cache()
    yield
    runtime.reset_runtime_cache()


def answer(tool, ctx, payload):
    pending = ctx.session_state.get('pending') or {}
    return tool.execute({'clarify_answers': {**payload, '__question_id': pending.get('question_id')}}, ctx)


def setup_tool(monkeypatch, cfg):
    table = {'warehouse::stock': cfg}
    from domains.chatbi import datasources, runtime as bi_runtime
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource', lambda db, ds: SimpleNamespace(
        host='warehouse', database='', is_active=True, db_type='mcp'))
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda: table)
    seen = []
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled',
                        lambda app, ds, tool, params: (
                            seen.append((ds, tool, params)) or ControlledResult(
                                ok=True, status='ok', data={'columns': [], 'rows': []})))
    return mta.MetrologyAssistantTool(object()), table, seen


def test_select_before_extract_only_selected_typed_parameters():
    a = capability({'count': {'type': 'integer'}, 'active': {'type': 'boolean'}})
    b = capability({'secret_other': {'type': 'string'}})
    llm = LLM({'scenario': 'a', 'count': 0, 'active': False, 'secret_other': 'leak'})
    result = mta.extract_slots(llm, 'stock zero', {'secret_other': 'old'}, None, {'a': a, 'b': b})
    assert result == {'scenario': 'a', 'count': 0, 'active': False}
    assert len(llm.prompts) == 2
    assert 'secret_other' not in llm.prompts[1][1]


def test_merge_keeps_typed_falsy_values():
    assert mta.merge_slots({'count': 0, 'active': False, 'query': {'x': 1}}, {}) == {
        'count': 0, 'active': False, 'query': {'x': 1}}


@pytest.mark.parametrize('properties', [{}, {'limit': {'type': 'integer'}}])
def test_zero_and_optional_parameters_execute(monkeypatch, properties):
    tool, _, seen = setup_tool(monkeypatch, capability(properties))
    result = tool.execute({'user_input': 'stock'}, context({'scenario': 'warehouse::stock'}))
    assert result.ask is None and result.error_for_llm is None
    assert seen == [('ds-stock', 'stock', {})]
    assert store.get_flow(runtime.get_pack_db(), 'generic-core') is None


def test_required_parameters_asked_sequentially_and_typed_outbound(monkeypatch):
    tool, _, seen = setup_tool(monkeypatch, capability(
        {'count': {'type': 'integer'}, 'active': {'type': 'boolean'},
         'filters': {'type': 'object'}}, ['count', 'active', 'filters']))
    ctx = context({'scenario': 'warehouse::stock'})
    result = tool.execute({'user_input': 'stock'}, ctx)
    assert result.ask.questions[0].header == 'count'
    for text, header in [('0', 'active'), ('false', 'filters')]:
        result = answer(tool, ctx, {'text': text})
        assert result.ask.questions[0].header == header
    result = answer(tool, ctx, {'text': '{"region":"north"}'})
    assert result.error_for_llm is None and result.ask is None
    assert seen[0][2] == {'count': 0, 'active': False, 'filters': {'region': 'north'}}


def test_invalid_answer_repersists_pending_and_recovers(monkeypatch):
    tool, _, seen = setup_tool(monkeypatch, capability({'count': {'type': 'integer'}}, ['count']))
    ctx = context({'scenario': 'warehouse::stock'})
    tool.execute({'user_input': 'stock'}, ctx)
    result = answer(tool, ctx, {'text': 'wrong'})
    assert result.ask is not None
    flow = store.get_flow(runtime.get_pack_db(), ctx.conv_id)
    assert flow['pendingQuestion']['slot'] == 'count'
    assert flow['pendingQuestion']['snapshot']
    result = answer(tool, ctx, {'text': '2'})
    assert result.ask is None and result.error_for_llm is None
    assert seen[0][2]['count'] == 2


def test_changed_declaration_expires_pending(monkeypatch):
    tool, table, seen = setup_tool(monkeypatch, capability({'count': {'type': 'integer'}}, ['count']))
    ctx = context({'scenario': 'warehouse::stock'})
    tool.execute({'user_input': 'stock'}, ctx)
    table['warehouse::stock']['action']['input_schema']['properties']['count']['type'] = 'boolean'
    result = answer(tool, ctx, {'text': 'true'})
    assert result.error_for_llm and '过期' in result.error_for_llm
    assert not seen


def test_memory_not_reused_between_capabilities(monkeypatch):
    tool, _, seen = setup_tool(monkeypatch, capability({'count': {'type': 'integer'}}, ['count']))
    ctx = context({'scenario': 'warehouse::stock'})
    ctx.session_state.set('scenario', 'another::stock')
    ctx.session_state.set('slots', {'count': 99})
    result = tool.execute({'user_input': 'stock'}, ctx)
    assert result.ask is not None and not seen


@pytest.mark.parametrize('status', ['not_found', 'forbidden', 'unavailable'])
def test_generic_outcomes_close_original_flow(monkeypatch, status):
    tool, _, _ = setup_tool(monkeypatch, capability())
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', lambda *a: ControlledResult(
        ok=False, status=status))
    result = tool.execute({'user_input': 'stock'}, context({'scenario': 'warehouse::stock'}))
    if status == 'unavailable':
        assert result.error_for_llm
    else:
        assert result.artifact['status'] == status and result.error_for_llm is None
    assert store.get_flow(runtime.get_pack_db(), 'generic-core') is None


def test_late_result_cannot_finish_replacement_flow(monkeypatch):
    tool, _, _ = setup_tool(monkeypatch, capability())
    def replace(*a):
        db = runtime.get_pack_db()
        store.close_running_flows(db, 'generic-core')
        store.create_flow(db, 'generic-core', 'alice', 'new-task', {}, None)
        return ControlledResult(ok=True, status='ok', data={'columns': [], 'rows': []})
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', replace)
    tool.execute({'user_input': 'stock'}, context({'scenario': 'warehouse::stock'}))
    assert store.get_flow(runtime.get_pack_db(), 'generic-core')['scenario'] == 'new-task'


def test_kg_generic_question_and_trusted_identity(monkeypatch):
    cfg = {'label': 'HR', 'intent': 'policy', 'required': {},
           'action': {'kind': 'kg', 'kb': 'HR', 'answer_prompt': 'Explain leave rules'}}
    tool, table, _ = setup_tool(monkeypatch, cfg)
    seen = {}
    def retrieve(app, kb, query, **kwargs):
        seen.update(kb=kb, query=query, **kwargs)
        return {'chunks': []}
    monkeypatch.setattr('domains.knowledge_graph.service.retrieve_evidence', retrieve)
    ctx = context({'scenario': 'warehouse::stock'})
    result = tool.execute({'user_input': 'How many leave days?', 'pack_params': {
        'knowledge_graph': {'user_roles': ['staff'], 'doc_filter': {'team': 'sales'}}}}, ctx)
    assert seen['query'] == 'How many leave days?'
    assert seen['user_id'] == 'alice' and seen['roles'] == ['staff']
    assert seen['metadata_filter'] == {'team': 'sales'}
    assert result.artifact['answer_kind'] == 'answer'
    table['warehouse::stock']['action'].pop('kb')
    result = tool.execute({'user_input': 'policy'}, ctx)
    assert result.error_for_llm and '知识库' in result.error_for_llm


def test_kg_permission_denied_is_business_notice(monkeypatch):
    tool, _, _ = setup_tool(monkeypatch, {'label': 'HR', 'intent': 'policy', 'required': {},
                                         'action': {'kind': 'kg', 'kb': 'HR'}})
    def denied(*a, **kw):
        raise PermissionError('denied')
    monkeypatch.setattr('domains.knowledge_graph.service.retrieve_evidence', denied)
    result = tool.execute({'user_input': 'policy'}, context({'scenario': 'warehouse::stock'}))
    assert result.artifact['status'] == 'forbidden'
    assert store.get_flow(runtime.get_pack_db(), 'generic-core') is None


def test_demo_export_is_explicit_pure_json_and_independent():
    import json
    from domains.metrology_assistant.legacy_demo import demo_scenarios
    first = demo_scenarios()
    assert first['mandatory_check']['action']['conclusion'] == 'mandatory'
    assert json.loads(json.dumps(first)) == first
    first['progress']['required']['question'] = 'changed'
    assert demo_scenarios()['progress']['required']['question'] != 'changed'


def test_missing_and_old_question_tokens_do_not_consume_pending(monkeypatch):
    tool, _, _ = setup_tool(monkeypatch, capability({'count': {'type': 'integer'}}, ['count']))
    ctx = context({'scenario': 'warehouse::stock'})
    first = tool.execute({'user_input': 'stock'}, ctx)
    assert first.ask.questions[0].question_id
    before = store.get_flow(runtime.get_pack_db(), ctx.conv_id)
    # 错误 token=旧卡重放,拒绝且不消费 pending
    result = tool.execute({'clarify_answers': {'text': '2', '__question_id': 'old-token'}}, ctx)
    assert result.error_for_llm and '过期' in result.error_for_llm
    assert store.get_flow(runtime.get_pack_db(), ctx.conv_id) == before
    # 不带 token=嵌入宿主/未升级前端,保持旧放行语义(消费 pending 续跑)
    invalid = tool.execute({'clarify_answers': {
        'text': 'bad', '__question_id': first.ask.questions[0].question_id}}, ctx)
    assert invalid.ask.questions[0].question_id != first.ask.questions[0].question_id


def test_explicit_datasource_cannot_cross_server_or_fixed_tool(monkeypatch):
    from domains.metrology_assistant.capability_plan import resolve_datasource
    from domains.chatbi import datasources, runtime as bi_runtime
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    info = SimpleNamespace(host='assets', database='', is_active=True, db_type='mcp')
    monkeypatch.setattr(datasources, 'get_datasource', lambda db, ds: info)
    plan = {'datasource_id': 'assets-ds', 'server': 'sales', 'tool': 'lookup'}
    with pytest.raises(ValueError, match='服务'):
        resolve_datasource(plan)
    info.host, info.database = 'sales', 'another_tool'
    with pytest.raises(ValueError, match='工具'):
        resolve_datasource(plan)


def test_only_exact_stored_legacy_configuration_is_upgraded():
    from domains.metrology_assistant.legacy_demo import SCENARIOS, upgrade_legacy_scenario
    original = deepcopy(SCENARIOS['mandatory_check'])
    got = upgrade_legacy_scenario('mandatory_check', original)
    assert got['action']['conclusion'] == 'mandatory'
    assert 'conclusion' not in original['action']
    generic = {'label': 'HR', 'intent': 'HR', 'required': {}, 'action': {'kind': 'kg', 'kb': 'HR'}}
    assert upgrade_legacy_scenario('mandatory_check', generic) == generic
    assert upgrade_legacy_scenario('custom', original) == original


def test_stored_v1_instrument_intent_is_upgraded_to_current_wording():
    """存量未改写配置的价目意图原文 → 装载时原位升级为含测量范围/准确度的现行文本。

    路由 LLM 只读 label/intent 选能力,初版未列"测量范围",字段级提问
    (查 X 的测量范围)选不中能力而落知识库兜底——升级后无需改 DB 即命中。
    """
    from domains.metrology_assistant.legacy_demo import (
        SCENARIOS, upgrade_legacy_scenario, _INSTRUMENT_INFO_INTENT_V1,
    )
    stored = {**deepcopy(SCENARIOS['instrument_info']),
              'intent': _INSTRUMENT_INFO_INTENT_V1}
    got = upgrade_legacy_scenario('instrument_info', stored)
    assert got['intent'] == SCENARIOS['instrument_info']['intent']
    assert '测量范围' in got['intent'] and '准确度' in got['intent']
    assert got['label'] == SCENARIOS['instrument_info']['label']
    assert stored['intent'] == _INSTRUMENT_INFO_INTENT_V1  # 原对象不被改写
    # 人工润色过的 intent 不匹配原文,保持不动
    polished = {**deepcopy(SCENARIOS['instrument_info']), 'intent': '人工改过的意图'}
    assert upgrade_legacy_scenario('instrument_info', polished) == polished
    # 现行种子文本(已是新版)不触发重复改写
    current = deepcopy(SCENARIOS['instrument_info'])
    assert upgrade_legacy_scenario('instrument_info', current) == current


def test_subject_slot_query_alias_and_normalization():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = {'label': 'x', 'intent': 'x', 'required': {
        'slot': 'kind', 'header': 'Kind', 'question': 'Kind?', 'options': [],
        'subject_slot': 'subject', 'normalize': 'lower'},
        'action': {'kind': 'kg', 'kb': 'X', 'query_template': '{query} {kind} {subject}'}}
    plan = prepare_plan(cfg, {'kind': ' A ', 'subject': 'Item'}, 'Question')
    assert plan['query'] == 'Question a Item'


def test_union_schema_preserves_json_and_full_validation():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = capability({'value': {'oneOf': [{'type': 'object'}, {'type': 'boolean'}]},
                      'code': {'type': 'string', 'pattern': '^X'}}, ['value'])
    plan = prepare_plan(cfg, {'value': {'a': 1}, 'code': 'wrong'})
    assert plan.get('error') or plan.get('will_ask')
    plan = prepare_plan(cfg, {'value': False, 'code': 'X1'})
    assert plan['params'] == {'value': False, 'code': 'X1'}


def test_rehydrated_options_pending_is_json_serializable(monkeypatch):
    from sdk.tool import AskOption
    cfg = capability({'name': {'type': 'string'}}, ['name'])
    cfg['required'] = {'slot': 'name', 'header': 'Name', 'question': 'Name?',
                       'options': [AskOption(label='A', description='A')]}
    tool, _, _ = setup_tool(monkeypatch, cfg)
    result = tool.execute({'user_input': 'stock'}, context({'scenario': 'warehouse::stock'}))
    assert result.ask is not None
    assert store.get_flow(runtime.get_pack_db(), 'generic-core')['pendingQuestion']['options'][0]['label'] == 'A'


def test_dry_run_uses_same_kb_query_and_typed_params():
    from domains.metrology_assistant.config_api import DryRunBody, _dry_run_once
    cfg = capability({'count': {'type': 'integer'}}, ['count'])
    result = _dry_run_once({'a': cfg}, LLM({'scenario': 'a', 'count': 0}), DryRunBody(text='stock'))
    assert result['action']['params'] == {'count': 0}
    cfg = {'label': 'HR', 'intent': 'policy', 'required': {}, 'action': {
        'kind': 'kg', 'kb_rules': [{'slot': 'team', 'in': ['A'], 'kb': 'Rules'}],
        'query_template': 'policy {team}'}}
    cfg['required'] = {'slot': 'team', 'header': 'team', 'question': 'team?', 'options': []}
    result = _dry_run_once({'a': cfg}, LLM({'scenario': 'a', 'team': 'A'}), DryRunBody(text='policy'))
    assert result['action']['kb'] == 'Rules' and result['action']['query'] == 'policy A'


def test_big_integer_keeps_exact_value():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = capability({'count': {'type': 'integer'}}, ['count'])
    plan = prepare_plan(cfg, {'count': '9007199254740993'})
    assert plan['params'] == {'count': 9007199254740993}


def test_query_template_tolerates_slot_named_query():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = {'label': 'x', 'intent': 'x', 'required': {
        'slot': 'query', 'header': 'Query', 'question': 'Query?', 'options': []},
        'action': {'kind': 'kg', 'kb': 'X', 'query_template': '{query}'}}
    plan = prepare_plan(cfg, {'query': 'keyword'}, 'user words')
    assert plan.get('error') is None
    assert plan['query'] == 'keyword'


def test_nullable_required_none_is_provided_not_missing():
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = capability({'count': {'type': ['integer', 'null']}}, ['count'])
    plan = prepare_plan(cfg, {'count': None})
    assert plan.get('will_ask') is None
    assert plan['params'] == {'count': None}


def test_kb_rule_slots_participate_in_extraction_specs():
    from domains.metrology_assistant.capability_plan import parameter_specs
    cfg = {'label': 'HR', 'intent': 'policy', 'required': {}, 'action': {
        'kind': 'kg', 'input_schema': {'type': 'object', 'properties': {}},
        'kb_rules': [{'slot': 'team', 'in': ['A'], 'kb': 'Rules'}]}}
    assert 'team' in {spec['slot'] for spec in parameter_specs(cfg)}


def test_query_template_missing_subject_slot_degrades_to_empty():
    """器具名未提供(purpose-only 会话)时模板缺参不是错误,按旧语义检索。"""
    from domains.metrology_assistant.capability_plan import prepare_plan
    cfg = {'label': '强检', 'intent': '强检判定', 'required': {
        'slot': 'purpose', 'header': '用途', 'question': '用途?', 'options': [],
        'subject_slot': 'instrument', 'subject_default': '该器具'},
        'action': {'kind': 'kg', 'kb_rules': [{'slot': 'purpose', 'in': ['贸易结算'], 'kb': '强检目录'}],
                   'conclusion': 'mandatory',
                   'query_template': '{instrument} {purpose} 强制检定 检定周期'}}
    plan = prepare_plan(cfg, {'purpose': '贸易结算'}, '贸易结算的器具要强检吗')
    assert plan.get('error') is None
    assert '强制检定' in plan['query']
    assert plan['query'].strip().startswith('贸易结算')


def test_answer_without_token_keeps_legacy_compatibility(monkeypatch):
    """嵌入宿主/未升级前端不带 __question_id:保持旧放行语义。"""
    tool, table, _ = setup_tool(monkeypatch, capability({'name': {'type': 'string'}}, ['name']))
    ctx = context({'scenario': 'warehouse::stock'})
    first = tool.execute({'user_input': '查库存'}, ctx)
    assert first.ask is not None
    # 同一会话上下文(mem 里存着 pending),补答不带 token
    resumed = tool.execute({'clarify_answers': {'text': '扳手'}}, ctx)
    assert resumed.ask is None, '无 token 的补答应按旧语义放行续跑'
