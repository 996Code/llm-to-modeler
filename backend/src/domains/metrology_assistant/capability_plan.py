"""Pure capability plans shared by conversation execution and admin previews."""
import hashlib
import json
import math
from decimal import Decimal, InvalidOperation

_MISSING = object()
_FULL_WIDTH = str.maketrans("０１２３４５６７８９，", "0123456789,")


def present(value):
    return value is not None and not (isinstance(value, str) and not value.strip())


def declaration_snapshot(cfg):
    def plain(value):
        return value.model_dump() if hasattr(value, 'model_dump') else str(value)
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False,
                                     default=plain).encode()).hexdigest()


def parameter_specs(cfg):
    action = cfg['action']
    schema = action.get('input_schema')
    main = cfg.get('required') or {}
    if isinstance(schema, dict):
        mapping = action.get('params') or {}
        required = schema.get('required') or []
        specs = []
        for name, prop in (schema.get('properties') or {}).items():
            slot = mapping.get(name, name)
            custom = main if main.get('slot') == slot else {}
            header = custom.get('header') or prop.get('title') or name
            specs.append({'name': name, 'slot': slot, 'schema': prop,
                          'required': name in required, 'header': str(header)[:12],
                          'question': custom.get('question') or f'请提供{header}',
                          'options': custom.get('options') or [
                              {'label': str(v), 'description': ''} for v in prop.get('enum', [])],
                          **{k: v for k, v in custom.items() if k not in ('slot', 'header')}})
        _append_rule_slots(action, specs)
        return specs
    specs = []
    if main.get('slot'):
        specs.append({**main, 'name': main['slot'], 'required': True, 'schema': None})
    known = {s['slot'] for s in specs}
    hints = cfg.get('_param_hints') or {}
    for slot in ([main['subject_slot']] if main.get('subject_slot') else []) + list((action.get('params') or {}).values()) + list(hints):
        if slot not in known:
            known.add(slot)
            spec = {'name': slot, 'slot': slot, 'required': False, 'schema': None}
            if slot in hints:  # 提示词靠 hint 声明槽位语义,缺了就抽不到值
                spec['extract_hint'] = f"{slot}: {hints[slot]};没提 → 空串"
            specs.append(spec)
    _append_rule_slots(action, specs, known)
    return specs


def _append_rule_slots(action, specs, known=None):
    """选库规则引用的槽位也要能被提取,否则规则永远不命中。"""
    known = known if known is not None else {s['slot'] for s in specs}
    for rule in action.get('kb_rules') or []:
        slot = rule.get('slot')
        if slot and slot not in known:
            known.add(slot)
            specs.append({'name': slot, 'slot': slot, 'required': False, 'schema': None})


def typed_value(value, schema):
    if not isinstance(schema, dict):
        return value
    kind = schema.get('type')
    if kind is None or isinstance(kind, list):
        candidates = [value]
        if isinstance(value, str):
            try:
                candidates.append(json.loads(value))
            except ValueError:
                pass
        from domains.chatbi.controlled import validate_against_schema
        for candidate in candidates:
            if validate_against_schema(candidate, schema) is None:
                return candidate
        raise ValueError('值不符合声明的 JSON Schema')
    if kind == 'string':
        if not isinstance(value, str):
            raise ValueError('请提供文本')
        value = value.strip()
    elif kind in ('integer', 'number'):
        if isinstance(value, bool):
            raise ValueError('请提供数字')
        try:
            if kind == 'integer':
                if isinstance(value, int):
                    number = value
                else:
                    number = int(Decimal(str(value).translate(_FULL_WIDTH).replace(',', '')))
            else:
                number = float(str(value).translate(_FULL_WIDTH).replace(',', ''))
        except (InvalidOperation, TypeError, ValueError, ArithmeticError):
            raise ValueError('请提供数字') from None
        if kind == 'number' and not math.isfinite(number):
            raise ValueError('请提供有限数字')
        value = number
    elif kind == 'boolean':
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            value = value.strip().lower() == 'true'
        if type(value) is not bool:
            raise ValueError('请提供 true 或 false')
    elif kind in ('object', 'array'):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError('请提供合法 JSON') from None
        if not isinstance(value, dict if kind == 'object' else list):
            raise ValueError('请提供 JSON 对象' if kind == 'object' else '请提供 JSON 数组')
        if kind == 'object':
            for name in schema.get('required') or []:
                if name not in value:
                    raise ValueError(f'JSON 缺少 {name}')
            props = schema.get('properties') or {}
            value = {k: typed_value(v, props[k]) if k in props else v for k, v in value.items()}
            if schema.get('additionalProperties') is False and any(k not in props for k in value):
                raise ValueError('JSON 包含未声明属性')
        elif schema.get('items'):
            value = [typed_value(v, schema['items']) for v in value]
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('请从声明的选项中选择')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            raise ValueError(f"不得小于 {schema['minimum']}")
        if 'maximum' in schema and value > schema['maximum']:
            raise ValueError(f"不得大于 {schema['maximum']}")
    return value


def kb_from_rules(action, slots):
    for rule in action.get('kb_rules') or []:
        if slots.get(rule.get('slot')) in (rule.get('in') or []):
            return rule.get('kb')
    return None


def prepare_plan(cfg, slots, user_input=''):
    from domains.metrology_assistant.tools.assistant import normalize_slot
    clean = {}
    ask = None
    for spec in parameter_specs(cfg):
        slot = spec['slot']
        raw = slots.get(slot, _MISSING)
        # 显式 null 只在声明允许(schema 分支)时算已提供,交给 typed_value 判定
        if raw is _MISSING or (raw is None and not isinstance(spec.get('schema'), dict)) \
                or (isinstance(raw, str) and not raw.strip()):
            if spec['required'] and ask is None:
                ask = dict(spec)
            continue
        value = raw
        try:
            if spec['schema'] is not None:
                value = typed_value(value, spec['schema'])
            elif spec.get('slot_type'):
                value, error = normalize_slot(value, spec['slot_type'], spec.get('enum_options'))
                if error:
                    raise ValueError(error)
            if isinstance(value, str):
                value = value.strip()
                normalization = spec.get('normalize')
                if normalization == 'upper':
                    value = value.upper()
                elif normalization == 'lower':
                    value = value.lower()
                elif normalization == 'strip':
                    value = value.strip()
            clean[slot] = value
        except ValueError as error:
            if ask is None:
                ask = {**spec, 'question': f"{spec.get('question') or slot} ({error})",
                       'header': spec.get('header', slot)[:12], 'options': spec.get('options') or []}
    action = cfg['action']
    plan = {'slots': clean, 'will_ask': ask, 'kind': action['kind']}
    if ask:
        return plan
    if action['kind'] == 'kg':
        kb = action.get('kb') or kb_from_rules(action, clean)
        mode = action.get('conclusion') or 'answer'
        if not kb:
            plan['error'] = '请明确绑定知识库或配置命中的选库规则,不能自动猜测知识库。'
            return plan
        try:
            # 槽位值优先:同名槽(如用户提炼的 query)覆盖缺省的原文占位;
            # 非 answer 模式必须显式模板(校验强制),无回退拼接。
            # 模板引用但本轮缺失的槽位补空串——追问流中 subject 可能
            # 还没提供(purpose-only 会话),缺参不是错误
            context = {'user_input': user_input, 'query': user_input}
            context.update(clean)
            if action.get('query_template'):
                from string import Formatter
                for field in {f for _, f, _, _ in
                              Formatter().parse(action['query_template']) if f}:
                    context.setdefault(field, '')
            query = action['query_template'].format(**context) if action.get('query_template') else user_input
        except KeyError as error:
            plan['error'] = f'查询模板缺少参数: {error.args[0]}'
            return plan
        plan.update(kb=kb, query=query, conclusion=mode, answer_prompt=action.get('answer_prompt', ''))
    else:
        mapping = action.get('params')
        if mapping is None:
            mapping = {s['name']: s['slot'] for s in parameter_specs(cfg)}
        plan.update(tool=action['tool'], datasource_id=action.get('datasource_id') or '',
                    datasource_env=action.get('env') or '', server=action.get('server') or '',
                    params={name: clean[slot] for name, slot in mapping.items() if slot in clean})
        if 'input_schema' in action:
            from domains.chatbi.controlled import validate_against_schema
            error = validate_against_schema(plan['params'], action['input_schema'])
            if error:
                plan['error'] = error
    return plan


def resolve_datasource(plan):
    import os
    ds = str(plan.get('datasource_id') or os.getenv(plan.get('datasource_env') or '', '')).strip()
    if not ds and plan.get('server'):
        from domains.metrology_assistant.capability_import import resolve_datasource_for_tool
        ds = resolve_datasource_for_tool(plan['server'], plan['tool']) or ''
    if ds and plan.get('server'):
        from domains.chatbi import datasources, runtime as bi_runtime
        info = datasources.get_datasource(bi_runtime.get_pack_db(), ds)
        if info is None or not info.is_active or info.db_type != 'mcp':
            raise ValueError('绑定的数据源不存在、已停用或不是 MCP 数据源')
        if info.host != plan['server']:
            raise ValueError('绑定的数据源与声明的 MCP 服务不一致,已拒绝执行')
        if info.database and info.database != plan['tool']:
            raise ValueError('绑定的数据源固定工具与能力工具不一致,已拒绝执行')
    return ds
