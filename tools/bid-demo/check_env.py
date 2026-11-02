#!/usr/bin/env python
"""Read-only demo checks using stored assistant bindings, never environment IDs."""
import argparse
import socket
import sys

import requests

SERVER_CODE = 'metrology'
_failed = 0


def ok(name, condition, evidence=''):
    global _failed
    _failed += not bool(condition)
    print(f"{'OK' if condition else 'FAIL'} {name}" + (f' - {evidence}' if evidence else ''))
    return bool(condition)


def _items(data):
    return data if isinstance(data, list) else data.get('items', [])


def bound_datasource(content, items, scenario):
    action = ((content.get('scenarios') or {}).get(scenario) or {}).get('action') or {}
    if 'env' in action or 'datasource_env' in action:
        return None
    server, tool, ds_id = action.get('server'), action.get('tool'), action.get('datasource_id')
    candidates = [d for d in items if d.get('isActive') and d.get('dbType') == 'mcp'
                  and (not server or d.get('host') == server)
                  and (not d.get('database') or d.get('database') == tool)]
    if ds_id:
        return next((d for d in candidates if d.get('id') == ds_id), None)
    if not server or not tool:
        return None
    fixed = [d for d in candidates if d.get('database') == tool]
    candidates = fixed or candidates
    return candidates[0] if len(candidates) == 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base', default='http://localhost:18080')
    ap.add_argument('--frontend', default='http://localhost:13081')
    ap.add_argument('--mock-port', type=int, default=9771)
    args = ap.parse_args()
    client = requests.Session()

    def get(path):
        response = client.get(f'{args.base}{path}', timeout=5)
        response.raise_for_status()
        return response.json()

    try:
        with socket.create_connection(('127.0.0.1', args.mock_port), timeout=1):
            ok('Mock MCP port', True)
    except OSError:
        ok('Mock MCP port', False)
    try:
        get('/api/health')
        ok('Backend health', True)
    except requests.RequestException:
        ok('Backend health', False)
        return 1
    try:
        response = client.get(f'{args.frontend}/ai-modeler/', timeout=3)
        ok('Frontend', response.status_code == 200)
    except requests.RequestException:
        ok('Frontend', False)
    try:
        servers = _items(get('/api/admin/mcp/servers'))
        server = next((s for s in servers if s.get('code') == SERVER_CODE), None)
        ok('MCP connected and enabled', server and server.get('enabled')
           and server.get('status') == 'connected')
        tools = {t.get('name') for t in (server or {}).get('tools_snapshot', [])}
        ok('MCP tools', {'query_progress', 'query_instrument'} <= tools)
        ok('MCP demo timeouts', server and 0 < float(server.get('timeout') or 0) <= 10
           and 0 < float(server.get('read_timeout') or 0) <= 10)
        content = get('/api/packs/metrology_assistant/config')['content']
        items = _items(get('/api/packs/chatbi/datasources'))
        for key in ('progress', 'instrument_info'):
            ds = bound_datasource(content, items, key)
            ok(f'{key} stored binding', ds is not None,
               ds.get('name', '') if ds else 'Bind datasource_id or a unique server/tool in admin')
        kbs = {kb.get('name'): kb for kb in _items(get('/api/packs/knowledge_graph/kbs'))}
        for name in ('强检目录', '检定规程'):
            kb = kbs.get(name)
            if ok(f'Knowledge base {name}', kb is not None):
                docs = _items(get(f"/api/packs/knowledge_graph/kbs/{kb['id']}/documents"))
                ok(f'{name} imported documents', any(d.get('importStatus') == 'succeeded' for d in docs))
    except (requests.RequestException, ValueError, KeyError, TypeError):
        ok('Demo dependencies', False, 'Check backend availability and admin authorization')
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
