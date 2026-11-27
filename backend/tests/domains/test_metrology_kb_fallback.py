"""未命中能力 / 业务数据查无 → 知识库链路兜底(跨库检索)→ 引导卡 的行为契约。

主路径:跨可访问库检索有证据 → 证据链回答(与 kb_search 同一条链);
边界:无证据/无可用库/检索异常/命中场景不受影响;dry-run 文案;
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
    """kg 目录桩:list_kbs/doc_filter 两件套。"""

    def __init__(self, kbs):
        self._kbs = kbs

    def list_kbs(self):
        return self._kbs

    def filter_doc_ids_by_metadata(self, kb_id, flt):
        return None  # None=该库不过滤


def capability():
    return {'label': 'Stock lookup', 'intent': 'look up stock', 'required': {},
            'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'stock',
                       'datasource_id': 'ds-stock', 'params': {}, 'card': '_table_card'}}


def context(llm, conv='kb-fallback'):
    traces = []

    def _trace(stage, title='', status='info', duration_ms=None, detail=None):
        traces.append({'stage': stage, 'title': title, 'status': status,
                       'duration_ms': duration_ms, 'detail': detail})

    ctx = SimpleNamespace(llm_client=llm, session_state=Mem(), conv_id=conv,
                          user_id='alice', emit=lambda *a: None, trace=_trace)
    ctx.traces = traces
    return ctx


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


def setup_tool(monkeypatch, kbs=None, multi=None):
    """统一装配:场景表 + kg 目录桩 + 跨库检索桩。"""
    table = {'warehouse::stock': capability()}
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda: table)
    monkeypatch.setattr('domains.knowledge_graph.runtime.get_kg_store',
                        lambda state: StubKgStore(kbs if kbs is not None else _kbs()))
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
        'knowledge_graph': {'doc_filter': {'team': 'sales'}}}}, ctx)
    # 跨库检索:folder 排除,其余全参与;doc_filter 透传
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


def test_chatbi_not_found_returns_business_notice_no_kb(monkeypatch):
    """业务查无一律直出业务通知(2026-11-17 产品定案,与筑基侧同源):
    不再降级知识库——检索完全不发起。"""
    called = []

    def multi(app, kbs, query, **kwargs):
        called.append(query)
        return multi_result(answer='检定周期为 1 年')

    tool, _ = setup_tool(monkeypatch, multi=multi)
    _datasource_ok(monkeypatch)
    _controlled('not_found', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    result = tool.execute({'user_input': '血压计的检测费用是多少'}, ctx)
    assert called == [], '业务查无不得发起知识库检索'
    assert result.artifact['type'] == 'metrology_notice'
    assert result.artifact['status'] == 'not_found'
    assert '未找到符合条件的数据' in result.artifact['message']


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


def test_stage_bridge_persists_per_stage_trace(monkeypatch):
    """阶段拆步落库(P3):每阶段一条 metrology.<key> trace 事件,
    duration_ms=上一阶段至此墙钟,detail.prev_stage 标归属(首段为 None)。"""
    def multi(app, kbs, query, **kwargs):
        on_stage = kwargs.get('on_stage')
        if on_stage:
            on_stage('kb_search.intent', '解析检索意图…')
        return multi_result()

    tool, _ = setup_tool(monkeypatch, multi=multi)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '压力表'}, ctx)
    assert result.artifact is not None
    stages = [t['stage'] for t in ctx.traces]
    assert 'metrology.assistant.extract' in stages
    first = next(t for t in ctx.traces if t['stage'] == 'metrology.assistant.extract')
    assert first['duration_ms'] >= 0 and first['detail'] == {'prev_stage': None}
    if 'metrology.kb_search.intent' in stages:
        second = next(t for t in ctx.traces if t['stage'] == 'metrology.kb_search.intent')
        # 兜底路径先发 assistant.retrieve 再进检索(intent 的耗时归属它)
        assert second['detail'] == {'prev_stage': 'assistant.retrieve'}
        assert second['duration_ms'] >= 0



def test_annotation_shortcut_before_routing(monkeypatch):
    """D32 标注短路提升到路由之前(2026-11-16,与筑基侧同源):固定问答
    命中零模型秒回,路由/抽槽/检索全跳过;artifact 带 annotationHit。"""
    from domains.knowledge_graph import annotations as kg_annotations

    def fake_shortcut(app_state, kbs, query):
        assert query == '计量检定业务流程是怎样的'
        return {'kb': {'id': 'kb-bm', 'name': '常州院便民问答'},
                'hit': {'question': '计量检定/校准业务流程是怎样的',
                        'answer': '三种办理入口:窗口送检、邮寄送检、上门取送。',
                        'score': 1.0, 'matchType': 'exact'}}

    monkeypatch.setattr(kg_annotations, 'try_shortcut', fake_shortcut)

    def multi(*a, **k):
        raise AssertionError('短路命中不应走检索')

    tool, _ = setup_tool(monkeypatch, multi=multi)

    class NoModel:
        def chat_json(self, *a, **k):
            raise AssertionError('短路命中不应调用任何模型')

    ctx = context(NoModel())
    traced = []
    ctx.trace = lambda key, title, status, detail=None: traced.append((key, title))
    result = tool.execute({'user_input': '计量检定业务流程是怎样的'}, ctx)
    assert result.artifact['annotationHit'] is True
    assert result.artifact['scenario'] == '标准问答'
    assert result.artifact['answer'].startswith('三种办理入口')
    assert traced and traced[0][0] == 'metrology.annotation'


def test_annotation_shortcut_failure_falls_through(monkeypatch):
    """短路层故障静默降级:照常走场景路由→兜底检索。"""
    from domains.knowledge_graph import annotations as kg_annotations

    def boom(*a, **k):
        raise RuntimeError('store down')

    monkeypatch.setattr(kg_annotations, 'try_shortcut', boom)
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(kbs=kbs)
        return multi_result()

    tool, _ = setup_tool(monkeypatch, multi=multi)
    ctx = context(LLM(extract={'scenario': 'other'}))
    result = tool.execute({'user_input': '送检要几个工作日'}, ctx)
    assert result.artifact['fallback'] is True
    assert seen['kbs']


class _StageLLM:
    """按 stage 分流并记录 prompt 的桩(与筑基侧同源测试形态)。"""

    def __init__(self, select=None, extract=None):
        self._select, self._extract = select, extract
        self.prompts = []

    def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None, **kw):
        self.prompts.append((stage, (messages or [{}])[0].get('content', '')))
        if stage == 'assistant.select':
            return deepcopy(self._select)
        if stage == 'assistant.extract':
            return deepcopy(self._extract or {})
        raise AssertionError(f'非预期 stage: {stage}')


def test_select_condenses_anaphoric_followup(monkeypatch):
    """选择器顺手改写(2026-11-16,condense 零新增调用形态,与筑基侧同源):
    指代式追问→选择调用输出 standalone,抽槽与兜底检索全用独立问句。"""
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(query=query)
        return multi_result()

    tool, _ = setup_tool(monkeypatch, multi=multi)
    llm = _StageLLM(select={'scenario': 'other',
                            'standalone': '数字压力表的检测费用是多少'})
    ctx = context(llm)
    traced = []
    ctx.trace = lambda key, title, status, detail=None: traced.append((key, title))
    ctx.session_state.set('capability_slots',
                          {'_context': {'instrument': '数字压力表'}})
    result = tool.execute({'user_input': '那这单上的器具检测费是多少'}, ctx)
    assert not any('rewrite' == s for s, _ in llm.prompts), '不应有独立改写调用'
    select_prompt = next(p for s, p in llm.prompts if s == 'assistant.select')
    assert 'standalone' in select_prompt and '会话背景' in select_prompt
    assert '数字压力表' in select_prompt
    assert seen['query'] == '数字压力表的检测费用是多少', '兜底检索应使用改写句'
    assert traced and traced[0][0] == 'metrology.rewrite'
    assert result.artifact['fallback'] is True


def test_select_without_standalone_uses_original(monkeypatch):
    """选择器未给 standalone → 原句直通,行为不变。"""
    seen = {}

    def multi(app, kbs, query, **kwargs):
        seen.update(query=query)
        return multi_result()

    tool, _ = setup_tool(monkeypatch, multi=multi)
    llm = _StageLLM(select={'scenario': 'other'})
    ctx = context(llm)
    tool.execute({'user_input': '查一下数字压力表的检测费用是多少'}, ctx)
    assert seen['query'] == '查一下数字压力表的检测费用是多少'


def test_result_context_backfill_for_anaphora(monkeypatch):
    """业务结果器具名回填:"名称"列(进度表)与"器具名称"列(证书表)进
    capability_slots._context;"单位名称"列不碰。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    tool, _ = setup_tool(monkeypatch)
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))

    def _ok_result(columns, rows):
        monkeypatch.setattr(
            'domains.chatbi.controlled.execute_controlled',
            lambda *a, **kw: ControlledResult(
                ok=True, status='ok', data={'columns': columns, 'rows': rows}))

    _ok_result(['业务类别', '名称', '状态'], [['计量检定', '数字压力表', '检测中']])
    ctx = context(LLM(extract={'scenario': 'warehouse::stock'}))
    tool.execute({'user_input': '查一下委托单 CZ2024-001 的办理进度'}, ctx)
    assert ctx.session_state.get('capability_slots')['_context']['instrument'] == '数字压力表'

    _ok_result(['证书编号', '单位名称', '器具名称'],
               [['ZS2024-0001', '常州示例机械有限公司', '数字压力表']])
    ctx2 = context(LLM(extract={'scenario': 'warehouse::stock'}))
    tool.execute({'user_input': '核对证书 ZS2024-0001'}, ctx2)
    assert ctx2.session_state.get('capability_slots')['_context']['instrument'] == '数字压力表'


def _identifier_table():
    order_cap = {
        'label': '委托单进度查询', 'intent': '按委托单号查进度',
        'required': {'slot': 'order_no', 'header': '委托单号'},
        'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                   'datasource_id': 'ds-stock', 'params': {}, 'card': '_table_card'},
    }
    return {'warehouse::order': order_cap, 'warehouse::stock': capability()}


def test_identifier_not_found_returns_business_notice(monkeypatch):
    """标识符查询查无即终态(2026-11-16,与筑基侧同源):凭委托单号查
    MCP not_found 后不转知识库兜底——弱召回的无关规范片段会顶掉业务
    通知;价目/能力类(无标识符槽位)保留兜底。"""
    called = []

    def multi(app, kbs, query, **kwargs):
        called.append(query)
        return multi_result()

    monkeypatch.setattr(mta, '_runtime_scenarios', lambda *a, **k: _identifier_table())
    monkeypatch.setattr(
        'domains.knowledge_graph.runtime.get_kg_store', lambda state: StubKgStore(_kbs()))
    monkeypatch.setattr(
        'domains.knowledge_graph.retrieval.answer_question_multi', multi)
    tool = mta.MetrologyAssistantTool(object())
    _datasource_ok(monkeypatch)
    _controlled('not_found', monkeypatch)
    ctx = context(LLM(extract={'scenario': 'warehouse::order', 'order_no': 'CZ9999-000'}))
    result = tool.execute({'user_input': '查一下委托单 CZ9999-000 的办理进度'}, ctx)
    assert called == [], '标识符查询查无不应转知识库兜底'
    assert result.artifact and result.artifact.get('type') == 'metrology_notice'
    assert result.artifact.get('status') == 'not_found'
    assert '未找到' in result.artifact.get('title', '') + result.artifact.get('message', '')


def test_standalone_does_not_leak_into_slots(monkeypatch):
    """standalone 只当问句用,不得进业务槽位(2026-11-16,与筑基侧同源)。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    tool, _ = setup_tool(monkeypatch)
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr(
        'domains.chatbi.controlled.execute_controlled',
        lambda *a, **kw: ControlledResult(
            ok=True, status='ok', data={'columns': ['库存'], 'rows': [['10']]}))
    llm = _StageLLM(select={'scenario': 'warehouse::stock',
                            'standalone': '数字压力表的检测费用是多少'},
                    extract={})
    ctx = context(llm)
    ctx.trace = lambda *a, **k: None
    tool.execute({'user_input': '那这单上的器具检测费是多少'}, ctx)
    slots = ctx.session_state.get('capability_slots') or {}
    assert 'standalone' not in slots.get('warehouse::stock', {}), 'standalone 泄漏进业务槽位'
    assert 'standalone' not in ctx.session_state.get('slots', {})


def test_identifier_slot_sanitization(monkeypatch):
    """按声明 pattern 清洗槽位值(2026-11-17,纯声明驱动,与筑基同源):
    值内含编号模式→取匹配值;不符声明形态→清空交追问;无 pattern 不清洗。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(
            ok=True, status='ok', data={'columns': ['状态'], 'rows': [['检测中']]})

    order_cap = {
        'label': '委托单进度查询', 'intent': '按委托单号查进度', 'required': {},
        'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                   'datasource_id': 'ds-stock', 'card': '_table_card',
                   'input_schema': {'type': 'object', 'required': ['order_no'],
                                   'properties': {'order_no': {
                                       'type': 'string', 'title': '委托单号',
                                       'pattern': r'^[A-Z]{2,}\d{0,4}-\d+$'}}}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::order': order_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    tool = mta.MetrologyAssistantTool(object())

    ctx = context(LLM(extract={'scenario': 'warehouse::order',
                               'order_no': '查一下委托单 CZ2024-001 的办理进度'}))
    ctx.trace = lambda *a, **k: None
    tool.execute({'user_input': '查一下委托单 CZ2024-001 的办理进度'}, ctx)
    assert seen.get('params', {}).get('order_no') == 'CZ2024-001', seen.get('params')

    ctx2 = context(LLM(extract={'scenario': 'warehouse::order', 'order_no': '那个单子'}))
    ctx2.trace = lambda *a, **k: None
    result2 = tool.execute({'user_input': '那个单子的进度'}, ctx2)
    assert result2.ask is not None, '不符声明形态应触发必填追问而非直传'

    ctx3 = context(LLM(extract={'scenario': 'warehouse::order', 'order_no': 'CZ2024-001'}))
    ctx3.trace = lambda *a, **k: None
    tool.execute({'user_input': '查 CZ2024-001 进度'}, ctx3)
    assert seen['params'].get('order_no') == 'CZ2024-001'


def test_chat_json_retry_on_empty_response():
    """网关空响应重试一次(2026-11-17,与筑基侧同源)。"""
    from domains.metrology_assistant.tools.assistant import _chat_json_retry

    calls = {'n': 0}

    class FlakyLLM:
        def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                return None
            return {'scenario': 'other'}

    data = _chat_json_retry(FlakyLLM(), [{'role': 'user', 'content': 'x'}],
                            conv_id=None, stage='assistant.select')
    assert data == {'scenario': 'other'} and calls['n'] == 2

    class DeadLLM:
        def chat_json(self, *a, **kw):
            raise RuntimeError('gateway down')

    try:
        _chat_json_retry(DeadLLM(), [{'role': 'user', 'content': 'x'}],
                         conv_id=None, stage='assistant.select')
        raise AssertionError('连续失败应抛')
    except RuntimeError:
        pass


def test_select_prompt_intent_priority_and_extract_override():
    """④⑤ 判据强化 + ③ 抽槽覆盖指令落进 prompt(与筑基侧同源)。"""
    captured = []

    class CapLLM:
        def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None, **kw):
            captured.append((stage, (messages or [{}])[0]['content']))
            if stage == 'assistant.select':
                return {'scenario': 'warehouse::order'}
            return {'order_no': 'CZ2024-001'}

    order_cap = {
        'label': '委托单进度查询', 'intent': '按委托单号查进度',
        'required': {'slot': 'order_no', 'header': '委托单号',
                     'question': '请提供委托单号'},
        'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                   'datasource_id': 'ds-stock', 'card': '_table_card'}}
    extracted = mta.extract_slots(
        CapLLM(), '那这单上的器具检测费是多少',
        {'warehouse::order': {'order_no': 'CZ0000-000'},
         '_context': {'instrument': '压力表'}},
        'conv-x', {'warehouse::order': order_cap})
    assert extracted['scenario'] == 'warehouse::order'
    assert extracted.get('order_no') == 'CZ2024-001'
    sel = next(p for st, p in captured if st == 'assistant.select')
    assert 'label/intent' in sel and '判 other' in sel
    ext = next(p for st, p in captured if st == 'assistant.extract')
    assert '以用户消息为准' in ext


def test_sticky_slots_reset_on_value_change(monkeypatch):
    """槽位粘滞治理(2026-11-17,与筑基侧同源):换条件即重置,旧条目
    编号不得压过本轮新器具名;无同名键冲突时背景合法累积保留。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(
            ok=True, status='ok', data={'columns': ['样品名称'], 'rows': [['电子秤']]})

    fee_cap = {
        'label': '价目查询', 'intent': '查检测费', 'required': {},
        'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                   'datasource_id': 'ds-stock', 'card': '_table_card',
                   'input_schema': {'type': 'object', 'properties': {
                       'name': {'type': 'string', 'title': '器具名称'},
                       'item': {'type': 'string', 'title': '条目编号'}}},
                   'params': {'name': 'name', 'item': 'item'}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::fee': fee_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(LLM(extract={'scenario': 'warehouse::fee', 'name': '电子秤'}))
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('capability_slots',
                          {'warehouse::fee': {'name': '压力表', 'item': '170'}})
    tool.execute({'user_input': '电子秤的检测费是多少'}, ctx)
    assert seen['params'].get('name') == '电子秤', seen['params']
    assert 'item' not in seen['params'], f'旧条目编号不得粘滞: {seen["params"]}'


def test_background_kept_when_extract_empty(monkeypatch):
    """本轮一个槽都没抽到 → 沿用背景(2026-11-17 定案,与筑基同源)。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(
            ok=True, status='ok', data={'columns': ['收费'], 'rows': [['80']]})

    fee_cap = {'label': '价目查询', 'intent': '查检测费', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '器具名称'}}},
                          'params': {'name': 'name'}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::fee': fee_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(LLM(extract={'scenario': 'warehouse::fee'}))
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('capability_slots', {'warehouse::fee': {'name': '压力表'}})
    tool.execute({'user_input': '那检定周期呢'}, ctx)
    assert seen['params'].get('name') == '压力表', '空抽取时背景应沿用(兜底)'


def test_required_slot_injected_by_standalone_rewrite_is_dropped():
    """必填槽位不得经改写句从背景搬入(2026-11-17 缺参引导,与筑基侧同源)。

    选择器 standalone 改写会把背景旧值补进省略句("帮我查一下办理进度"
    →"帮我查一下委托单号CZ2023-777的办理进度"),抽槽再当本轮用户提供值
    ——用户没给单号却拿旧单号查了别人的单。判定=必填声明+值级:值不在
    用户原话且等于背景该槽位值 → 丢弃,改写句一并弃用。
    """
    order_cap = {'label': '委托单进度查询', 'intent': '按委托单号查进度',
                 'required': {},
                 'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                            'datasource_id': 'ds-stock', 'card': '_table_card',
                            'input_schema': {'type': 'object', 'required': ['order_no'],
                                             'properties': {'order_no': {
                                                 'type': 'string', 'title': '委托单号'}}}}}
    llm = _StageLLM(select={'scenario': 'warehouse::order',
                            'standalone': '帮我查一下委托单号CZ2023-777的办理进度'},
                    extract={'order_no': 'CZ2023-777'})
    extracted = mta.extract_slots(llm, '帮我查一下办理进度',
                                  {'warehouse::order': {'order_no': 'CZ2023-777'}},
                                  'conv-inject', {'warehouse::order': order_cap})
    assert 'order_no' not in extracted, '背景搬入的必填值不得当本轮用户提供'
    assert not extracted.get('standalone'), '搬运旧值的改写句须一并弃用'


def test_required_slot_stated_in_user_message_survives_rewrite():
    """反向守卫:用户原话给了单号 → 保留(不得误杀)。"""
    order_cap = {'label': '委托单进度查询', 'intent': '按委托单号查进度',
                 'required': {},
                 'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                            'datasource_id': 'ds-stock', 'card': '_table_card',
                            'input_schema': {'type': 'object', 'required': ['order_no'],
                                             'properties': {'order_no': {
                                                 'type': 'string', 'title': '委托单号'}}}}}
    llm = _StageLLM(select={'scenario': 'warehouse::order'},
                    extract={'order_no': 'CZ2024-001'})
    extracted = mta.extract_slots(llm, '查一下委托单 CZ2024-001 的进度',
                                  {'warehouse::order': {'order_no': 'CZ2023-777'}},
                                  'conv-keep', {'warehouse::order': order_cap})
    assert extracted.get('order_no') == 'CZ2024-001'


def test_required_slot_not_carried_on_zero_extract(monkeypatch):
    """必填槽位零槽不沿用(2026-11-17 缺参引导回归,与筑基同源):
    「帮我查一下办理进度」(无单号)不得默默沿用上轮单号直查。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    called = []
    order_cap = {'label': '委托单进度查询', 'intent': '按委托单号查进度',
                 'required': {},
                 'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                            'datasource_id': 'ds-stock', 'card': '_table_card',
                            'input_schema': {'type': 'object', 'required': ['order_no'],
                                             'properties': {'order_no': {
                                                 'type': 'string', 'title': '委托单号',
                                                 'pattern': r'^[A-Z]{2,}\d{0,4}-\d+$'}}}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::order': order_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled',
                        lambda *a, **kw: called.append(1)
                        or ControlledResult(ok=True, status='ok',
                                            data={'columns': ['状态'], 'rows': [['检测中']]}))
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(_StageLLM(select={'scenario': 'warehouse::order'}))  # extract {}
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('capability_slots', {'warehouse::order': {'order_no': 'CZ2023-777'}})
    result = tool.execute({'user_input': '帮我查一下办理进度'}, ctx)
    assert result.ask is not None, '必填单号缺失应追问而非沿用旧值直查'
    assert called == [], '不得发起业务查询'
    assert '委托单号' in str(result.ask.questions[0].question)


def test_optional_slot_still_carried_on_zero_extract(monkeypatch):
    """可选槽位零槽沿用照旧(2026-11-17 C 序列不回归,与筑基同源)。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    seen = {}
    fee_cap = {'label': '价目查询', 'intent': '查检测费与周期', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '器具名称'}}}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::fee': fee_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled',
                        lambda app, ds, tool, params, **kw: seen.update(params=params)
                        or ControlledResult(ok=True, status='ok',
                                            data={'columns': ['周期'], 'rows': [['半年']]}))
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(_StageLLM(select={'scenario': 'warehouse::fee',
                                    'standalone': '压力表的检测周期是多久'}))
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('capability_slots', {'warehouse::fee': {'name': '压力表'}})
    result = tool.execute({'user_input': '检测周期是多久'}, ctx)
    assert seen['params'].get('name') == '压力表', '可选槽零槽沿用不得回归'
    assert result.artifact and result.artifact.get('type') == 'metrology_table'


def test_focus_removed_always_single_hop_list(monkeypatch):
    """二跳已移除(2026-11-27 用户裁决,与筑基同源):select 即便输出 focus
    也被无视,业务查询永远单跳列表;全空列(检测周期整列 null)剔除。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    calls = []

    def controlled(app, ds, tool, params, **kw):
        calls.append(dict(params))
        return ControlledResult(ok=True, status='ok', data={
            'columns': ['条目编号', '样品名称', '测量范围', '检测周期'],
            'rows': [[170, '压力表', '(0～60)MPa', None]]})

    fee_cap = {'label': '检测能力查询', 'intent': '查检测能力与周期', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'capability',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '样品名称'},
                              '条目编号': {'type': 'string', 'title': '条目编号'}}}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::fee': fee_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(_StageLLM(select={'scenario': 'warehouse::fee', 'focus': '检测周期'},
                            extract={'name': '压力表'}))
    ctx.trace = lambda *a, **k: None
    result = tool.execute({'user_input': '压力表的检测周期是多久'}, ctx)
    assert len(calls) == 1, '二跳已移除:必须单跳列表'
    cols = result.artifact['columns']
    assert '检测周期' not in cols, f'全空列应剔除: {cols}'
    assert '条目编号' not in cols, f'内部 id 列应剔除: {cols}'
    assert cols == ['样品名称', '测量范围'], f'剔除后列序不变: {cols}'


def test_focus_field_ignored_keeps_list(monkeypatch):
    """select 输出 focus(模型惯性)→ 字段被无视,列表照常返回。"""
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.chatbi.controlled import ControlledResult

    calls = []

    def controlled(app, ds, tool, params, **kw):
        calls.append(dict(params))
        return ControlledResult(ok=True, status='ok', data={
            'columns': ['样品名称', '检测周期'], 'rows': [['压力表', '半年']]})

    fee_cap = {'label': '检测能力查询', 'intent': '查检测能力与周期', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'capability',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '样品名称'}}}}}
    monkeypatch.setattr(mta, '_runtime_scenarios',
                        lambda *a, **k: {'warehouse::fee': fee_cap})
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    tool = mta.MetrologyAssistantTool(object())
    ctx = context(_StageLLM(select={'scenario': 'warehouse::fee', 'focus': '检测周期'},
                            extract={'name': '压力表'}))
    ctx.trace = lambda *a, **k: None
    result = tool.execute({'user_input': '压力表的检测周期是多久'}, ctx)
    assert len(calls) == 1, 'focus 被无视:单跳'
    assert result.artifact['type'] == 'metrology_table'


def _pending_cert_caps():
    cert_cap = {'label': '证书报告防伪查询', 'intent': '核对证书报告真伪', 'required': {},
                'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'cert',
                           'datasource_id': 'ds-stock', 'card': '_table_card',
                           'input_schema': {'type': 'object', 'required': ['编号', '名称'],
                                            'properties': {'编号': {'type': 'string', 'title': '编号',
                                                                   'pattern': r'^ZS\d+$'},
                                                           '名称': {'type': 'string', 'title': '名称'}}}}}
    order_cap = {'label': '委托单进度查询', 'intent': '按委托单号查进度', 'required': {},
                 'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'progress',
                            'datasource_id': 'ds-stock', 'card': '_table_card',
                            'input_schema': {'type': 'object', 'required': ['委托单号'],
                                             'properties': {'委托单号': {
                                                 'type': 'string', 'title': '委托单号',
                                                 'pattern': r'^[A-Z]{2,}\d{0,4}-\d+$'}}}}}
    return {'warehouse::cert': cert_cap, 'warehouse::order': order_cap}


def _pending_fixture(monkeypatch, table, controlled):
    from domains.chatbi import datasources, runtime as bi_runtime
    from domains.metrology_assistant.capability_plan import declaration_snapshot
    monkeypatch.setattr(mta, '_runtime_scenarios', lambda *a, **k: table)
    monkeypatch.setattr(bi_runtime, 'get_pack_db', lambda: object())
    monkeypatch.setattr(datasources, 'get_datasource',
                        lambda db, ds: SimpleNamespace(host='warehouse', database='',
                                                       is_active=True, db_type='mcp'))
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    return declaration_snapshot(table['warehouse::cert'])


def test_pending_typed_topic_switch_reroutes(monkeypatch):
    """追问态打字换话题(2026-11-17 回归,与筑基同源):追问"请提供名称"时问
    "帮我查一下办理进度"——新问题不得被整句填进所问槽位吞掉;判切换后取消
    pending 按新问题重路由,选择结果经 preselected 复用(整轮一次 select)。"""
    from domains.chatbi.controlled import ControlledResult
    called = []

    def controlled(app, ds, tool, params, **kwargs):
        called.append(dict(params))
        return ControlledResult(ok=True, status='ok', data={'columns': ['状态'], 'rows': [['检测中']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'warehouse::order'}, extract={})
    ctx = context(llm, conv='conv-switch')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q1', 'scenario': 'warehouse::cert',
        'slots': {'编号': 'ZS88880001'}, 'snapshot': snapshot,
        'user_input': '帮我核对一下证书，编号 ZS88880001'})
    result = tool.execute({'user_input': '帮我查一下办理进度',
                           'clarify_answers': {'text': '帮我查一下办理进度', '__question_id': 'q1'}}, ctx)
    assert result.ask is not None, '切换后应按新问题走缺参追问'
    assert '委托单号' in str(result.ask.questions[0].question)
    assert called == [], '不得拿旧证书参数发起业务查询'
    selects = [s for s, _ in llm.prompts if s == 'assistant.select']
    assert len(selects) == 1, '切换判定与重路由须复用同一次选择调用'


def test_pending_typed_fits_declaration_answers_without_select(monkeypatch):
    """打字回答按所问槽位声明校验通过(编号 pattern 命中)→ 直接当答案,
    话题判定零 LLM 调用。"""
    from domains.chatbi.controlled import ControlledResult
    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(ok=True, status='ok', data={'columns': ['结论'], 'rows': [['有效']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'warehouse::order'})  # 不应被调用
    ctx = context(llm, conv='conv-fit')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '编号', 'header': '编号', 'schema': {'type': 'string', 'pattern': r'^ZS\d+$'},
        'options': [], 'question': '请提供编号', 'question_id': 'q2',
        'scenario': 'warehouse::cert', 'slots': {'名称': '常州示例机械有限公司'},
        'snapshot': snapshot, 'user_input': '帮我核对证书'})
    tool.execute({'user_input': 'ZS88880001',
                  'clarify_answers': {'text': 'ZS88880001', '__question_id': 'q2'}}, ctx)
    assert seen.get('params', {}).get('编号') == 'ZS88880001', seen
    assert seen['params'].get('名称') == '常州示例机械有限公司'
    assert not [s for s, _ in llm.prompts if s == 'assistant.select'], '声明可验不得烧选择调用'


def test_pending_typed_same_scenario_still_answer(monkeypatch):
    """打字内容被判为同场景(裸名词答案/同能力追问)→ 维持答案语义,不误取消追问。"""
    from domains.chatbi.controlled import ControlledResult
    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(ok=True, status='ok', data={'columns': ['结论'], 'rows': [['有效']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'warehouse::cert'})
    ctx = context(llm, conv='conv-same')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q3', 'scenario': 'warehouse::cert',
        'slots': {'编号': 'ZS88880001'}, 'snapshot': snapshot,
        'user_input': '帮我核对一下证书，编号 ZS88880001'})
    tool.execute({'user_input': '常州示例机械有限公司',
                  'clarify_answers': {'text': '常州示例机械有限公司', '__question_id': 'q3'}}, ctx)
    assert seen.get('params', {}).get('名称') == '常州示例机械有限公司', seen


def test_pending_card_answer_never_switches(monkeypatch):
    """追问卡点选(结构化 answers 带 header 键)= 用户点了选项,永不判切换、零调用。"""
    from domains.chatbi.controlled import ControlledResult
    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(ok=True, status='ok', data={'columns': ['结论'], 'rows': [['有效']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'warehouse::order'})  # 不应被调用
    ctx = context(llm, conv='conv-card')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q4', 'scenario': 'warehouse::cert',
        'slots': {'编号': 'ZS88880001'}, 'snapshot': snapshot,
        'user_input': '帮我核对一下证书，编号 ZS88880001'})
    tool.execute({'user_input': '常州示例机械有限公司',
                  'clarify_answers': {'名称': '常州示例机械有限公司',
                                      'text': '常州示例机械有限公司',
                                      '__question_id': 'q4'}}, ctx)
    assert seen.get('params', {}).get('名称') == '常州示例机械有限公司', seen
    assert not [s for s, _ in llm.prompts if s == 'assistant.select'], '点卡答案零调用'


def test_pending_typed_question_shaped_reroutes_to_kb(monkeypatch):
    """pending×超纲输入(2026-11-17 回归,与筑基同源):追问时问"今天天气怎么样"
    ——无场景意图且疑问句形态,不得整句填槽;取消追问按新问题路由落知识库。"""
    from domains.chatbi.controlled import ControlledResult
    called = []

    def controlled(app, ds, tool, params, **kwargs):
        called.append(dict(params))
        return ControlledResult(ok=True, status='ok', data={'columns': ['x'], 'rows': [['y']]})

    tool, _ = setup_tool(
        monkeypatch, multi=lambda *a, **kw: {'sources': {'chunks': []}, 'answer': ''})
    monkeypatch.setattr('domains.chatbi.controlled.execute_controlled', controlled)
    llm = _StageLLM(select={'scenario': 'other'})
    ctx = context(llm, conv='conv-weather')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q5', 'scenario': 'warehouse::stock',
        'slots': {}, 'user_input': '帮我核对一下证书'})
    result = tool.execute({'user_input': '今天天气怎么样',
                           'clarify_answers': {'text': '今天天气怎么样', '__question_id': 'q5'}}, ctx)
    assert called == [], '超纲输入不得当答案填槽发起业务查询'
    assert result.artifact['type'] == 'metrology_notice', '应落知识库兜底引导'
    assert '已检索知识库' in result.artifact['message']
    assert ctx.session_state.get('pending') is None, '追问须取消'


def test_pending_typed_bare_value_with_other_select_still_answer(monkeypatch):
    """反向守卫:select=other 但输入是裸值形态(公司名)→ 仍是答案,不误判切换。"""
    from domains.chatbi.controlled import ControlledResult
    seen = {}

    def controlled(app, ds, tool, params, **kwargs):
        seen.update(params=params)
        return ControlledResult(ok=True, status='ok', data={'columns': ['结论'], 'rows': [['有效']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'other'})
    ctx = context(llm, conv='conv-bare')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q6', 'scenario': 'warehouse::cert',
        'slots': {'编号': 'ZS88880001'}, 'snapshot': snapshot,
        'user_input': '帮我核对一下证书，编号 ZS88880001'})
    tool.execute({'user_input': '常州示例机械有限公司',
                  'clarify_answers': {'text': '常州示例机械有限公司', '__question_id': 'q6'}}, ctx)
    assert seen.get('params', {}).get('名称') == '常州示例机械有限公司', seen


def test_pending_typed_question_shaped_same_scenario_reroutes(monkeypatch):
    """同场景但疑问句形态("这个证书的编号是多少")→ 不当答案填名称,按新问题重路由。"""
    from domains.chatbi.controlled import ControlledResult
    called = []

    def controlled(app, ds, tool, params, **kwargs):
        called.append(dict(params))
        return ControlledResult(ok=True, status='ok', data={'columns': ['x'], 'rows': [['y']]})

    table = _pending_cert_caps()
    snapshot = _pending_fixture(monkeypatch, table, controlled)
    tool = mta.MetrologyAssistantTool(object())
    llm = _StageLLM(select={'scenario': 'warehouse::cert'}, extract={})
    ctx = context(llm, conv='conv-qsame')
    ctx.trace = lambda *a, **k: None
    ctx.session_state.set('pending', {
        'slot': '名称', 'header': '名称', 'schema': None, 'options': [],
        'question': '请提供名称', 'question_id': 'q7', 'scenario': 'warehouse::cert',
        'slots': {'编号': 'ZS88880001'}, 'snapshot': snapshot,
        'user_input': '帮我核对一下证书，编号 ZS88880001'})
    result = tool.execute({'user_input': '这个证书的编号是多少',
                           'clarify_answers': {'text': '这个证书的编号是多少',
                                               '__question_id': 'q7'}}, ctx)
    assert called == [], '疑问句形态不得整句填槽'
    assert result.ask is not None, '重路由后按声明缺参追问'


def test_standalone_replaced_entity_is_dropped():
    """实体保真守卫(2026-11-17 复合长句实测,与筑基同源):改写器把用户原话
    实体替换成变体("压力表"→"压力真空表")→ 弃用改写以原句抽槽。"""
    fee_cap = {'label': '价目查询', 'intent': '查检测费', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '器具名称'}}}}}
    llm = _StageLLM(select={'scenario': 'warehouse::fee',
                            'standalone': '压力真空表检测费多少钱，检定要多久'},
                    extract={'name': '压力表'})
    extracted = mta.extract_slots(llm, '压力表检测费多少钱来着，顺便问下检定要多久',
                                  {'warehouse::fee': {'name': '压力表'}},
                                  'conv-ef', {'warehouse::fee': fee_cap})
    extract_prompts = [p for s, p in llm.prompts if s == 'assistant.extract']
    assert '压力表检测费多少钱来着' in extract_prompts[0], '抽槽须用用户原句'
    assert '压力真空表' not in extract_prompts[0], '被替换的实体不得进抽槽'
    assert not extracted.get('standalone'), '实体被替换的改写句须弃用'


def test_standalone_entity_kept_passes():
    """反向守卫:改写保留了用户原话实体(只去口语词)→ 改写照常使用。"""
    fee_cap = {'label': '价目查询', 'intent': '查检测费', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '器具名称'}}}}}
    llm = _StageLLM(select={'scenario': 'warehouse::fee',
                            'standalone': '压力表检测费多少钱，检定要多久'},
                    extract={'name': '压力表'})
    extracted = mta.extract_slots(llm, '压力表检测费多少钱来着，顺便问下检定要多久',
                                  {'warehouse::fee': {'name': '压力表'}},
                                  'conv-ef2', {'warehouse::fee': fee_cap})
    assert extracted.get('standalone') == '压力表检测费多少钱，检定要多久'


def test_standalone_kept_when_context_duplicates_slot_value():
    """拼接防御去重(2026-11-17 审查发现,与筑基同源):器具名称与
    _context.instrument 同值(结果回填常态)时,重复实体不得让 distinct ≥2
    误杀忠实改写。"""
    fee_cap = {'label': '价目查询', 'intent': '查检测费', 'required': {},
               'action': {'kind': 'chatbi', 'server': 'warehouse', 'tool': 'fee',
                          'datasource_id': 'ds-stock', 'card': '_table_card',
                          'input_schema': {'type': 'object', 'properties': {
                              'name': {'type': 'string', 'title': '器具名称'}}}}}
    llm = _StageLLM(select={'scenario': 'warehouse::fee',
                            'standalone': '压力表检测费多少钱，检定要多久'},
                    extract={'name': '压力表'})
    extracted = mta.extract_slots(llm, '压力表检测费多少钱来着，顺便问下检定要多久',
                                  {'warehouse::fee': {'name': '压力表'},
                                   '_context': {'instrument': '压力表'}},
                                  'conv-dup', {'warehouse::fee': fee_cap})
    assert extracted.get('standalone') == '压力表检测费多少钱，检定要多久', '同值重复不得误判拼接'
