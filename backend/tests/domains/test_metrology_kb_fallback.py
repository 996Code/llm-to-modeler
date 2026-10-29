"""未命中能力 → 知识库兜底 → 引导卡 的行为契约。

主路径:默认库有证据 → 证据链回答(与 kg 场景同一条红线);
边界:无证据/未配置/拒权/命中场景不受影响;dry-run 文案随配置变化。
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from domains.chatbi.controlled import ControlledResult
from domains.metrology_assistant import runtime, store
from domains.metrology_assistant.scenario_config import (
    ConfigValidationError, validate_config,
)
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
    """按 stage 分流:能力选择/抽槽 → extract;结论生成 → conclusion。"""

    def __init__(self, extract=None, conclusion=None):
        self._extract, self._conclusion = extract or {}, conclusion

    def chat_json(self, messages=None, temperature=None, stage=None,
                  conv_id=None, **kw):
        if stage == 'assistant.conclude':
            return deepcopy(self._conclusion)
        return deepcopy(self._extract)


def capability():
    return {'label': 'Stock lookup', 'intent': 'look up stock', 'required': {},
            'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'stock',
                       'datasource_id': 'ds-stock', 'params': {}, 'card': '_table_card'}}


def context(llm, conv='kb-fallback'):
    return SimpleNamespace(llm_client=llm, session_state=Mem(), conv_id=conv,
                           user_id='alice', emit=lambda *a: None)


def answered():
    return {'decision': 'answered', 'conclusion': '送检需 5 个工作日',
            'evidence': [{'text': '第五条:常规器具 5 个工作日', 'chunk_refs': [1]}],
            'suggestion': '提前预约'}


def setup_tool(monkeypatch, cfg=None, fallback='检定规程', retrieve=None):
    """统一装配:场景表 + 兜底配置 + kg 检索桩。"""
    table = {'warehouse::stock': cfg or capability()}
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda: table)
    monkeypatch.setattr('domains.metrology_assistant.scenario_config.load_fallback_kb',
                        lambda: fallback)
    if retrieve is not None:
        monkeypatch.setattr('domains.knowledge_graph.service.retrieve_evidence', retrieve)
    return mta.MetrologyAssistantTool(object()), table


def test_fallback_answers_with_evidence(monkeypatch):
    seen = {}

    def retrieve(app, kb, query, **kwargs):
        seen.update(kb=kb, query=query, **kwargs)
        return {'kb': {'id': 'kb-1', 'name': '检定规程'},
                'chunks': [{'text': '第五条:常规器具 5 个工作日', 'score': 0.9}],
                'subgraph': {'nodes': [], 'edges': []}}

    tool, _ = setup_tool(monkeypatch, retrieve=retrieve)
    ctx = context(LLM(extract={'scenario': 'other'}, conclusion=answered()))
    result = tool.execute({'user_input': '送检要几个工作日', 'pack_params': {
        'knowledge_graph': {'user_roles': ['staff'], 'doc_filter': {'team': 'sales'}}}}, ctx)
    assert seen['kb'] == '检定规程' and seen['query'] == '送检要几个工作日'
    assert seen['roles'] == ['staff'] and seen['metadata_filter'] == {'team': 'sales'}
    assert result.error_for_llm is None
    assert result.artifact['type'] == 'kg_search_result'
    assert result.artifact['answer_kind'] == 'answer'
    assert result.artifact['fallback'] is True
    assert result.artifact['scenario'] == '知识库检索'
    assert result.artifact['cited_chunks'] == [1]
    assert result.summary.startswith('结论:')
    assert result.formatted['chunkCount'] == 1
    assert store.get_flow(runtime.get_pack_db(), ctx.conv_id) is None


def test_fallback_without_evidence_returns_guidance(monkeypatch):
    tool, table = setup_tool(
        monkeypatch, retrieve=lambda *a, **kw: {'chunks': []})
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '今天天气怎么样'}, ctx)
    assert result.artifact['type'] == 'metrology_notice'
    assert result.artifact['status'] == 'unsupported'
    assert '已检索知识库「检定规程」' in result.artifact['message']
    assert 'Stock lookup' in result.artifact['message']


def test_fallback_not_configured_skips_retrieval(monkeypatch):
    called = []
    tool, _ = setup_tool(
        monkeypatch, fallback='',
        retrieve=lambda *a, **kw: called.append(1) or {'chunks': [{'text': 'x'}]})
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '今天天气怎么样'}, ctx)
    assert not called
    assert result.artifact['status'] == 'unsupported'
    assert '已检索知识库' not in result.artifact['message']


def test_fallback_permission_denied_is_forbidden_notice(monkeypatch):
    def denied(*a, **kw):
        raise PermissionError('denied')

    tool, _ = setup_tool(monkeypatch, retrieve=denied)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '随便问'}, ctx)
    assert result.artifact['status'] == 'forbidden'
    assert '无权访问' in result.artifact['message']


def test_fallback_config_error_degrades_to_guidance(monkeypatch, caplog):
    def broken():
        raise RuntimeError('config store down')

    table = {'warehouse::stock': capability()}
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda: table)
    monkeypatch.setattr('domains.metrology_assistant.scenario_config.load_fallback_kb', broken)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = mta.MetrologyAssistantTool(object()).execute({'user_input': '随便问'}, ctx)
    assert result.artifact['status'] == 'unsupported'


def test_matched_scenario_bypasses_fallback(monkeypatch):
    called = []
    tool, _ = setup_tool(
        monkeypatch,
        retrieve=lambda *a, **kw: called.append(1) or {'chunks': [{'text': 'x'}]})
    from domains.chatbi import datasources, runtime as bi_runtime
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource', lambda db, ds: SimpleNamespace(
        host='warehouse', database='', is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled',
                        lambda app, ds, tool, params: ControlledResult(
                            ok=True, status='ok', data={'columns': [], 'rows': []}))
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '查库存'}, ctx)
    assert not called
    assert result.artifact['type'] != 'kg_search_result'
    assert result.error_for_llm is None


# ── dry-run 文案与配置校验 ──────────────────────────────────────

def test_dry_run_note_reflects_fallback():
    from domains.metrology_assistant.config_api import DryRunBody, _dry_run_once
    body = DryRunBody(text='随便问')
    llm = LLM(extract={'scenario': 'other'})
    out = _dry_run_once({'warehouse::stock': capability()}, llm, body, '检定规程')
    assert out['missing'] is True and out['scenario'] is None
    assert '「检定规程」兜底' in out['note']
    out = _dry_run_once({'warehouse::stock': capability()}, llm, body)
    assert '暂不支持' in out['note']


def test_validate_config_fallback_kb_rules():
    assert validate_config({'scenarios': {}, 'fallback_kb': '检定规程'})
    assert validate_config({'scenarios': {}})
    with pytest.raises(ConfigValidationError):
        validate_config({'scenarios': {}, 'fallback_kb': ''})
    with pytest.raises(ConfigValidationError):
        validate_config({'scenarios': {}, 'fallback_kb': 3})


def test_runtime_cache_carries_fallback_kb():
    runtime.set_cached_config({'a': {}}, 7, '检定规程')
    assert runtime.cached_fallback_kb() == '检定规程'
    runtime.reset_runtime_cache()
    assert runtime.cached_fallback_kb() == ''
