"""未命中能力 / 业务数据查无 → 知识库链路兜底(跨库检索)→ 引导卡 的行为契约。

主路径:跨可访问库检索有证据 → 证据链回答(与 kb_search 同一条链);
边界:无证据/无可用库/检索异常/ACL 过滤/命中场景不受影响;dry-run 文案;
数据层兜底:受控查询 not_found → 原问题转知识库,无依据回业务通知。
"""
from copy import deepcopy
from types import SimpleNamespace

from domains.chatbi.controlled import ControlledResult
from domains.metrology_assistant import runtime, store
from domains.metrology_assistant.scenario_config import validate_config
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


class StubKgStore:
    """kg 目录桩:list_kbs/get_all_kb_acl/doc_filter 三件套。"""

    def __init__(self, kbs, acl_map=None):
        self._kbs, self._acl = kbs, acl_map or {}

    def list_kbs(self):
        return self._kbs

    def get_all_kb_acl(self):
        return self._acl

    def filter_doc_ids_by_metadata(self, kb_id, flt):
        return None  # None=该库不过滤


def capability():
    return {'label': 'Stock lookup', 'intent': 'look up stock', 'required': {},
            'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'stock',
                       'datasource_id': 'ds-stock', 'params': {}, 'card': '_table_card'}}


def context(llm, conv='kb-fallback'):
    return SimpleNamespace(llm_client=llm, session_state=Mem(), conv_id=conv,
                           user_id='alice', emit=lambda *a: None)


def _kbs():
    return [{'id': 'kb-1', 'name': '检定规程', 'kind': 'kb'},
            {'id': 'kb-f', 'name': '目录', 'kind': 'folder'},
            {'id': 'kb-2', 'name': '内部文件', 'kind': 'kb'}]


def multi_result(answer='送检需 5 个工作日'):
    return {'answer': answer, 'kb': {'id': 'kb-1', 'name': '检定规程'},
            'kbs': [{'id': 'kb-1', 'name': '检定规程'},
                    {'id': 'kb-2', 'name': '内部文件'}],
            'subgraph': {'nodes': [], 'edges': []},
            'sources': {'entities': [], 'chunks': [
                {'docId': 'd1', 'docName': '周期要点.md', 'text': '第五条:常规器具 5 个工作日',
                 'score': 0.9}]},
            'cited_chunks': [1]}


def setup_tool(monkeypatch, kbs=None, acl_map=None, multi=None):
    """统一装配:场景表 + kg 目录桩 + 跨库检索桩。"""
    table = {'warehouse::stock': capability()}
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda: table)
    monkeypatch.setattr('domains.knowledge_graph.runtime.get_kg_store',
                        lambda state: StubKgStore(kbs if kbs is not None else _kbs(), acl_map))
    if multi is not None:
        monkeypatch.setattr('domains.knowledge_graph.retrieval.answer_question_multi', multi)
    return mta.MetrologyAssistantTool(object()), table


def test_fallback_answers_with_evidence(monkeypatch):
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(kbs=kbs, query=query, **kwargs)
        return multi_result()

    tool, _ = setup_tool(monkeypatch, multi=multi)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '送检要几个工作日', 'pack_params': {
        'knowledge_graph': {'user_roles': ['staff'], 'doc_filter': {'team': 'sales'}}}}, ctx)
    # 跨库检索:folder 排除,其余全参与;身份与 doc_filter 透传
    assert [k['id'] for k in seen['kbs']] == ['kb-1', 'kb-2']
    assert seen['query'] == '送检要几个工作日'
    assert seen['doc_filter'] is not None          # doc_filter 生效(桩返回 None=全通过)
    assert result.error_for_llm is None
    assert result.artifact['type'] == 'kg_search_result'
    assert result.artifact['fallback'] is True
    assert result.artifact['scenario'] == '知识库检索'
    assert result.artifact['answer'] == '送检需 5 个工作日'
    assert result.artifact['cited_chunks'] == [1]
    assert result.artifact['kbs'] and len(result.artifact['kbs']) == 2
    assert result.summary.startswith('结论:')
    assert result.formatted['chunkCount'] == 1
    assert store.get_flow(runtime.get_pack_db(), ctx.conv_id) is None


def test_fallback_acl_filters_denied_kbs(monkeypatch):
    """被 ACL 拒的库静默跳过(参与面收窄),不出面向客户的拒权话术。"""
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(kbs=kbs)
        return multi_result()

    acl = {'kb-2': [{'subjectType': 'user', 'subjectId': 'someone-else'}]}
    tool, _ = setup_tool(monkeypatch, acl_map=acl, multi=multi)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '随便问个文档问题'}, ctx)
    assert [k['id'] for k in seen['kbs']] == ['kb-1']   # kb-2 被过滤
    assert result.artifact.get('fallback') is True       # 正常出证据卡,无 forbidden


def test_fallback_all_denied_returns_guidance(monkeypatch):
    called = []
    acl = {'kb-1': [{'subjectType': 'user', 'subjectId': 'x'}],
           'kb-2': [{'subjectType': 'user', 'subjectId': 'x'}]}
    tool, table = setup_tool(
        monkeypatch, acl_map=acl,
        multi=lambda *a, **kw: called.append(1) or multi_result())
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '随便问'}, ctx)
    assert not called                                  # 无可用库不发起检索
    assert result.artifact['status'] == 'unsupported'
    assert '已检索' not in result.artifact['message']
    assert 'Stock lookup' in result.artifact['message']


def test_fallback_without_evidence_returns_guidance(monkeypatch):
    tool, table = setup_tool(
        monkeypatch, multi=lambda *a, **kw: {'sources': {'chunks': []}, 'answer': ''})
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '今天天气怎么样'}, ctx)
    assert result.artifact['type'] == 'metrology_notice'
    assert result.artifact['status'] == 'unsupported'
    assert '已检索知识库,未找到相关资料' in result.artifact['message']
    assert 'Stock lookup' in result.artifact['message']


def test_fallback_answer_without_chunks_still_guidance(monkeypatch):
    """有回答文本但零召回片段:证据红线优先,不采信。"""
    tool, _ = setup_tool(
        monkeypatch, multi=lambda *a, **kw: {'answer': '看起来能答', 'sources': {
            'chunks': [{'text': 'x'}]}} | {'sources': {'chunks': []}})
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '随便问'}, ctx)
    assert result.artifact['status'] == 'unsupported'


def test_fallback_retrieval_failure_honest_error(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError('retrieval down')

    tool, _ = setup_tool(monkeypatch, multi=boom)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '随便问'}, ctx)
    assert result.artifact is None
    assert '知识库检索失败' in result.error_for_llm


def test_matched_scenario_bypasses_fallback(monkeypatch):
    called = []
    tool, _ = setup_tool(
        monkeypatch,
        multi=lambda *a, **kw: called.append(1) or multi_result())
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


# ── 数据层兜底:业务查询 not_found → 原问题转知识库 ──────────────

def _controlled(status, monkeypatch, ok=False):
    monkeypatch.setattr(
        'domains.chatbi.controlled.execute_controlled',
        lambda *a, **kw: ControlledResult(
            ok=ok, status=status,
            error=None if ok else type('E', (), {'message': status})(), source={}))


def _datasource_ok(monkeypatch):
    from domains.chatbi import datasources, runtime as bi_runtime
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource', lambda db, ds: SimpleNamespace(
        host='warehouse', database='', is_active=True, db_type='mcp'))


def test_chatbi_not_found_falls_back_to_kb(monkeypatch):
    """业务表未收录(如价目表外器具)→ 原问题走知识库,有据出证据卡。"""
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(query=query)
        return multi_result(answer='检定周期为 1 年')

    tool, _ = setup_tool(monkeypatch, multi=multi)
    _datasource_ok(monkeypatch)
    _controlled('not_found', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '血压计的检定周期是多久'}, ctx)
    assert seen['query'] == '血压计的检定周期是多久'   # 原问题检索
    assert result.artifact['type'] == 'kg_search_result'
    assert result.artifact.get('fallback') is True
    assert result.artifact['answer'] == '检定周期为 1 年'


def test_chatbi_not_found_kb_empty_keeps_business_notice(monkeypatch):
    """知识库也无依据 → 回业务查无通知(业务事实仍在,不吞进引导卡)。"""
    tool, _ = setup_tool(
        monkeypatch, multi=lambda *a, **kw: {'sources': {'chunks': []}, 'answer': ''})
    _datasource_ok(monkeypatch)
    _controlled('not_found', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '查一个不存在的东西'}, ctx)
    assert result.artifact['type'] == 'metrology_notice'
    assert result.artifact['status'] == 'not_found'
    assert '未找到符合条件的数据' in result.artifact['message']


def test_chatbi_not_found_zero_citation_keeps_business_notice(monkeypatch):
    """弱召回 0 引用的合成"未找到"不顶业务查无事实(委托单查无仍出业务通知)。"""
    weak = {'answer': '知识库中未找到相关内容,建议联系业务窗口确认',
            'kb': {'id': 'kb-1', 'name': '检定规程'},
            'sources': {'chunks': [{'docId': 'd1', 'docName': '弱相关.md', 'text': 'x', 'score': 0.5}]},
            'cited_chunks': []}
    tool, _ = setup_tool(monkeypatch, multi=lambda *a, **kw: weak)
    _datasource_ok(monkeypatch)
    _controlled('not_found', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '查一下 BJ9000 的进度'}, ctx)
    assert result.artifact['type'] == 'metrology_notice'
    assert result.artifact['status'] == 'not_found'


def test_chatbi_forbidden_does_not_fall_back(monkeypatch):
    """拒权是权限事实,不转知识库(装作能答反而错)。"""
    called = []
    tool, _ = setup_tool(
        monkeypatch, multi=lambda *a, **kw: called.append(1) or multi_result())
    _datasource_ok(monkeypatch)
    _controlled('forbidden', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '查别人的单'}, ctx)
    assert not called                                   # 未发起知识库检索
    assert result.artifact['status'] == 'forbidden'
    assert result.artifact['type'] == 'metrology_notice'


# ── dry-run 文案与配置校验 ──────────────────────────────────────

def test_dry_run_note_reflects_fallback():
    from domains.metrology_assistant.config_api import DryRunBody, _dry_run_once
    body = DryRunBody(text='随便问')
    llm = LLM(extract={'scenario': 'other'})
    out = _dry_run_once({'warehouse::stock': capability()}, llm, body)
    assert out['missing'] is True and out['scenario'] is None
    assert '知识库跨库检索兜底' in out['note']


def test_validate_config_without_fallback_kb():
    assert validate_config({'scenarios': {}})
    # fallback_kb 已废除:存量配置里残留该键不再校验(忽略即可)
    assert validate_config({'scenarios': {}, 'fallback_kb': '历史残留'})
