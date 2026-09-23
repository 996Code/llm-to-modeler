"""JS 脚本 Profile 与外部字段目录测试。"""
from unittest.mock import MagicMock

import pytest

from domains.njmind_form.pack import create_prompt_loader
from domains.njmind_form.keys import FIELDS
from domains.njmind_form.tools._script_common import (
    JS_SCRIPT_PROFILES,
    build_external_field_catalog,
    get_js_script_profile,
)
from domains.njmind_form.tools.generate_js_script import (
    GenerateJsScriptTool,
    _url_requirement_error,
)
from sdk.tool import ToolContext


def _make_ctx():
    return ToolContext(
        llm_client=MagicMock(),
        asset_client=None,
        conversation=None,
        emit=lambda *a, **k: None,
    )


def _list_profile_state(profile, slot, fields):
    return {
        'user_input': '格式化金额',
        'pack_params': {'njmind_form': {
            'script_profile': profile,
            'script_slot': slot,
            'fields': fields,
        }},
    }


def test_unknown_profile_returns_none():
    assert get_js_script_profile('not-a-profile') is None


def test_form_and_list_profiles_are_distinct():
    assert get_js_script_profile('field_change')['context'] == 'form'
    assert get_js_script_profile('table_formatter')['context'] == 'list'


def test_external_catalog_uses_normalized_field_keys():
    catalog = build_external_field_catalog([
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    assert catalog['keys'] == {'amount'}
    assert '#1 amount | 金额 | NUMBER' in catalog['text']


def test_field_change_requires_form_artifact():
    tool = GenerateJsScriptTool()
    state = {
        'pack_params': {'njmind_form': {
            'script_profile': 'field_change',
            'script_field': 'amount',
            'script_slot': 'onChange',
        }},
    }
    assert '表单配置' in tool.validate_input(state)


def test_table_formatter_requires_list_fields():
    tool = GenerateJsScriptTool()
    state = {
        'pack_params': {'njmind_form': {
            'script_profile': 'table_formatter',
            'script_slot': 'formatterScript',
        }},
    }
    assert '列表字段' in tool.validate_input(state)


def test_table_formatter_uses_list_field_directory():
    state = _list_profile_state('table_formatter', 'formatterScript', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    ctx = _make_ctx()
    GenerateJsScriptTool()._step_locate(state, ctx)
    assert state['known_keys'] == {'amount'}
    assert state['script_slot'] == 'formatterScript'
    ctx.llm_client.chat_json.assert_not_called()


def test_profile_generation_uses_registered_prompt_and_interaction_artifact():
    state = _list_profile_state('table_formatter', 'formatterScript', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    ctx = _make_ctx()
    object.__setattr__(ctx, 'prompt_loader', MagicMock())
    ctx.llm_client.chat_json.return_value = {
        'script': '({ row }) => { return row.amount; }',
        'note': '返回金额',
        'error': '',
    }

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact['scriptType'] == 'js_interaction'
    assert result.artifact['target'] == {
        'profile': 'table_formatter',
        'slot': 'formatterScript',
        'targetDesc': '',
    }
    ctx.prompt_loader.render.assert_called_once_with(
        'njmind_form', 'js_table_formatter_generate', profile='table_formatter')


def test_profile_error_returns_no_artifact():
    state = _list_profile_state('table_formatter', 'formatterScript', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    ctx = _make_ctx()
    ctx.llm_client.chat_json.return_value = {
        'script': '',
        'note': '',
        'error': '请提供格式化规则',
    }

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact is None
    assert '请提供' in result.summary


def test_profile_return_and_row_field_checks_are_enforced():
    state = _list_profile_state('table_formatter', 'formatterScript', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    state.update({
        'script_profile': 'table_formatter',
        'known_keys': {'amount'},
        'script': '({ row }) => { return row.unknown; }',
        'retry_count': 99,
        'check_errors': [],
    })

    GenerateJsScriptTool()._step_check(state, _make_ctx())

    assert any('row 引用了不存在' in error for error in state['check_errors'])
    assert 'script' not in state


def test_profile_return_and_row_field_checks_are_enforced():
    state = _list_profile_state('table_formatter', 'formatterScript', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'typeName': 'NUMBER'},
    ])
    state.update({
        'script_profile': 'table_formatter',
        'known_keys': {'amount'},
        'script': '({ row }) => { return row.unknown; }',
        'retry_count': 99,
        'check_errors': [],
    })

    GenerateJsScriptTool()._step_check(state, _make_ctx())

    assert any('row 引用了不存在' in error for error in state['check_errors'])
    assert 'script' not in state


@pytest.mark.parametrize('script', [
    "({ row }) => { return row?.unknown; }",
    "({ row }) => { return row['unknown']; }",
    "({ row }) => { return row?.['unknown']; }",
])
def test_profile_row_field_checks_cover_optional_and_bracket_access(script):
    state = {
        'script_profile': 'table_formatter',
        'known_keys': {'amount'},
        'script': script,
        'retry_count': 99,
        'check_errors': [],
    }

    GenerateJsScriptTool()._step_check(state, _make_ctx())

    assert any('row 引用了不存在' in error for error in state['check_errors'])
    assert 'script' not in state


def test_profile_context_rejects_field_directory_without_keys():
    tool = GenerateJsScriptTool()
    assert '列表字段' in tool.validate_input(_list_profile_state(
        'table_formatter', 'formatterScript', [{'fieldTitleText': '金额'}]))


def test_url_requirement_rejects_vague_dynamic_description():
    assert _url_requirement_error('根据当前行拼接链接', {'amount'})
    assert _url_requirement_error('根据当前行 amount 拼接链接', {'amount'}) is None


def _form_profile_state(profile, slot, fields):
    return {
        'user_input': '按状态处理',
        'source_artifact': {FIELDS: fields},
        'pack_params': {'njmind_form': {
            'script_profile': profile,
            'script_slot': slot,
        }},
    }


def test_profile_prompts_render_real_runtime_contracts():
    loader = create_prompt_loader()

    field_change = loader.render('njmind_form', 'js_field_change_generate',
                                 profile='field_change')
    field_url = loader.render('njmind_form', 'js_url_generate',
                              profile='field_url')
    button_url = loader.render('njmind_form', 'js_url_generate',
                               profile='button_url')
    button = loader.render('njmind_form', 'js_button_generate',
                           profile='button_custom')
    formatter = loader.render('njmind_form', 'js_table_formatter_generate',
                              profile='table_formatter')
    visibility = loader.render('njmind_form', 'js_table_visibility_generate',
                               profile='table_visibility')

    assert '({ value, option, formData, message, userInfo, service }) => any' in field_change
    assert '({ row }) => string' in field_url
    assert '不可使用 formData' in field_url
    assert ('{"script":"({ row, checkedRowKeys, message, userInfo, service, '
            'checkedRows, listPageCondition, extension }) => { return \'...\'; }"') in button_url
    assert ('({ row, checkedRowKeys, message, userInfo, service, checkedRows, '
            'listPageCondition, extension }) => any') in button
    assert 'API 地址、方法和必要参数' in button
    assert '({ row }) => { return String(row.amount); }' in formatter
    assert '({ catalog, tab, userInfo }) => boolean' in visibility


def test_field_change_checks_form_data_keys():
    state = _form_profile_state('field_change', 'onChange', [
        {'fieldTitleKey': 'amount', 'fieldTitleText': '金额', 'formFieldType': 1},
    ])
    GenerateJsScriptTool()._step_locate(state, _make_ctx())
    state.update({
        'script': '({ value, option, formData }) => { formData.unknown = value; }',
        'retry_count': 99,
        'check_errors': [],
    })

    GenerateJsScriptTool()._step_check(state, _make_ctx())

    assert any('表单中不存在' in error for error in state['check_errors'])
    assert 'script' not in state


def test_field_url_and_form_button_check_row_keys_from_canvas():
    fields = [{'fieldTitleKey': 'status', 'fieldTitleText': '状态', 'formFieldType': 4}]
    for profile, slot in [('field_url', 'urlScript'), ('button_hidden', 'hiddenScript')]:
        state = _form_profile_state(profile, slot, fields)
        GenerateJsScriptTool()._step_locate(state, _make_ctx())
        state.update({
            'script': '({ row }) => { return row.unknown === 1; }',
            'retry_count': 99,
            'check_errors': [],
        })

        GenerateJsScriptTool()._step_check(state, _make_ctx())

        assert state['known_keys'] == {'status'}
        assert any('row 引用了不存在' in error for error in state['check_errors'])
        assert 'script' not in state


def test_row_profiles_reject_form_data_references():
    tool = GenerateJsScriptTool()
    row_profiles = [
        profile for profile in JS_SCRIPT_PROFILES.values()
        if profile['field_namespace'] == 'row'
    ]

    for profile in row_profiles:
        state = {
            'script': '({ row }) => { return formData.status; }',
            'known_keys': {'status'},
            'retry_count': 99,
            'check_errors': [],
        }

        tool._check_profile_script(state, _make_ctx(), profile)

        assert any('只提供 row' in error for error in state['check_errors'])
        assert 'script' not in state


def test_list_button_uses_list_field_directory_without_context_kind():
    state = _list_profile_state('button_hidden', 'hiddenScript', [
        {'fieldTitleKey': 'status', 'fieldTitleText': '状态', 'typeName': 'SELECT'},
    ])
    ctx = _make_ctx()

    assert GenerateJsScriptTool().validate_input(state) is None
    GenerateJsScriptTool()._step_locate(state, ctx)
    state.update({
        'script': '({ row }) => { return row.unknown === 1; }',
        'retry_count': 99,
        'check_errors': [],
    })
    GenerateJsScriptTool()._step_check(state, ctx)

    assert state['known_keys'] == {'status'}
    assert 'context_kind' not in state
    assert any('row 引用了不存在' in error for error in state['check_errors'])
    ctx.llm_client.chat_json.assert_not_called()


def test_url_missing_rule_returns_ordinary_no_artifact_error():
    state = _form_profile_state('field_url', 'urlScript', [
        {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
    ])
    ctx = _make_ctx()
    ctx.llm_client.chat_json.return_value = {
        'script': '', 'note': '', 'error': '请提供固定 URL 或链接拼接规则',
    }

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact is None
    assert '固定 URL' in result.summary


def test_custom_api_and_empty_script_errors_return_no_artifact():
    state = _form_profile_state('button_custom', 'customScript', [
        {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
    ])
    ctx = _make_ctx()
    ctx.llm_client.chat_json.return_value = {
        'script': '', 'note': '', 'error': '请提供 API 地址、方法和必要参数',
    }

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact is None
    assert 'API 地址' in result.summary

    empty_script_state = _form_profile_state('field_url', 'urlScript', [
        {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
    ])
    empty_script_state['user_input'] = '跳转到 /detail'
    empty_script_ctx = _make_ctx()
    empty_script_ctx.llm_client.chat_json.return_value = {
        'script': '', 'note': '', 'error': '',
    }
    empty_script_result = GenerateJsScriptTool().execute(empty_script_state,
                                                          empty_script_ctx)

    assert empty_script_result.artifact is None
    assert '脚本生成失败' in empty_script_result.summary


@pytest.mark.parametrize(
    ('profile', 'user_input', 'script'),
    [
        ('field_url', '跳转到 https://example.com/detail',
         "({ row }) => { return 'https://example.com/detail'; }"),
        ('button_url', '打开内部路径 /detail',
         "({ row }) => { return '/detail'; }"),
        ('table_url', '使用当前行 id 拼接详情链接',
         "({ row }) => { return '/detail/' + row.id; }"),
    ],
)
def test_url_profiles_allow_explicit_url_or_dynamic_rule(profile, user_input,
                                                          script):
    if profile == 'table_url':
        state = _list_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'typeName': 'TEXT'},
        ])
    else:
        state = _form_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
        ])
    state['user_input'] = user_input
    ctx = _make_ctx()
    ctx.llm_client.chat_json.return_value = {
        'script': script, 'note': '', 'error': '',
    }

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact is not None
    ctx.llm_client.chat_json.assert_called_once()


@pytest.mark.parametrize('profile', ['field_url', 'button_url', 'table_url'])
def test_url_profiles_reject_ambiguous_detail_request_even_with_existing_script(
        profile):
    current_script = "({ row }) => { return '/old-detail/' + row.id; }"
    if profile == 'table_url':
        state = _list_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'typeName': 'TEXT'},
        ])
    else:
        state = _form_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
        ])
    state['user_input'] = '只跳转详情'
    state['pack_params']['njmind_form']['current_script'] = current_script
    ctx = _make_ctx()

    result = GenerateJsScriptTool().execute(state, ctx)

    assert result.artifact is None
    assert 'URL' in result.summary or '链接' in result.summary
    ctx.llm_client.chat_json.assert_not_called()


@pytest.mark.parametrize(
    ('user_input', 'is_error'),
    [
        ('跳转到 https://example.com/detail', False),
        ('打开内部路径 /detail', False),
        ('使用当前行 id 拼接详情链接', False),
        ('只跳转详情', True),
    ],
)
def test_url_requirement_error_requires_url_or_dynamic_rule(user_input, is_error):
    error = _url_requirement_error(user_input)

    assert (error is not None) is is_error


@pytest.mark.parametrize('profile', ['field_url', 'button_url', 'table_url'])
def test_url_requirement_gate_ignores_existing_script(profile):
    current_script = "({ row }) => { return '/old-detail/' + row.id; }"
    if profile == 'table_url':
        state = _list_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'typeName': 'TEXT'},
        ])
    else:
        state = _form_profile_state(profile, 'urlScript', [
            {'fieldTitleKey': 'id', 'fieldTitleText': '编号', 'formFieldType': 0},
        ])
    state['user_input'] = '只跳转详情'
    state['pack_params']['njmind_form']['current_script'] = current_script
    ctx = _make_ctx()
    events = []
    ctx.emit = lambda *args, **kwargs: events.append(args)

    GenerateJsScriptTool().execute(state, ctx)

    assert state['generation_error']
    assert any(event[:2] == ('stage', 'generate') for event in events)
    ctx.llm_client.chat_json.assert_not_called()
