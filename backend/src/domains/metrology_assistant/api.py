"""metrology_assistant 自有 HTTP API(挂 /api/packs/metrology_assistant)。

仅一个只读端点 GET /test/env:供管理端「智能客服-测试」页可视化运行
环境——表驱动读 DB 场景配置。历史 legacy_demo 静态视图已随演示装置
下线(2026-11-20 同步自筑基AI)。
"""
import logging

from fastapi import APIRouter, Depends, Request

from sdk.pack_api import admin_required

from domains.metrology_assistant.tools import assistant as cfg

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/test/env", dependencies=[Depends(admin_required)])
async def test_env(request: Request) -> dict:
    table = cfg._runtime_scenarios()
    names = set()
    for scenario in table.values():
        action = scenario['action']
        if action.get('kind') == 'kg':
            if action.get('kb'):
                names.add(action['kb'])
            names.update(r['kb'] for r in action.get('kb_rules') or [] if r.get('kb'))
    errors, kbs = [], []
    if names:
        from domains.knowledge_graph import runtime as kg_runtime
        try:
            store = kg_runtime.get_kg_store(request.app.state)
            for name in sorted(names):
                kb = store.get_kb_by_name(name)
                docs = store.list_documents(kb['id']) if kb else []
                kbs.append({'name': name, 'exists': kb is not None, 'docs': len(docs),
                            'imported': sum(d.get('importStatus') == 'succeeded' for d in docs),
                            'status': 'ok' if kb else 'missing'})
        except Exception:
            logger.exception('Assistant diagnostics dependency unavailable')
            errors.append('知识库状态读取失败,请查看服务日志')
    # 单号夹具:场景表里"委托单号"类槽位的追问选项(表驱动,配置自带)。
    fixtures = []
    for scenario in table.values():
        req = scenario.get('required') or {}
        if '单号' in str(req.get('header') or ''):
            # options 经装载 _rehydrate 复原为 AskOption 对象——dict/对象双形状兼容
            for o in (req.get('options') or []):
                label = o.get('label') if isinstance(o, dict) else getattr(o, 'label', None)
                desc = (o.get('description') if isinstance(o, dict)
                        else getattr(o, 'description', '')) or ''
                if label:
                    fixtures.append({'order': label, 'desc': desc})
    return {'errors': errors, 'kbMapping': {}, 'kbs': kbs, 'progress': {},
            'orderFixtures': fixtures, 'capabilities': [
                {'key': key, 'label': c.get('label'), 'kind': c['action']['kind'],
                 'server': c['action'].get('server'), 'tool': c['action'].get('tool')}
                for key, c in table.items()]}

