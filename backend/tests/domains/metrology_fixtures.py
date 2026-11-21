# -*- coding: utf-8 -*-
"""B 仓 metrology 测试共享夹具(2026-11-20 随 demo 装置与存量升级层下线)。

configured_scenarios(): "已配置环境"的纯 JSON 场景表——B 形态(强检免追问、
chatbi 走 server 绑定),测试造数唯一事实源。
"""
import copy

_C = {
    'mandatory_check': {
        'label': '器具是否强制检定(强检判定,按用途检索依据)',
        'intent': '问器具是否需要强制检定/属于强检吗',
        '_param_hints': {'instrument': '所问器具名称'},
        'action': {
            'kind': 'kg',
            'conclusion': 'mandatory',
            'query_template': '{instrument} {purpose} 强制检定 检定周期',
            'kb_rules': [
                {'slot': 'purpose',
                 'in': ['贸易结算', '安全防护', '医疗卫生', '环境监测', '执法鉴定', ''],
                 'kb': '强检目录'},
                {'slot': 'purpose', 'in': ['内部使用', '校准'], 'kb': '检定规程'},
            ],
        },
    },
    'progress': {
        'label': '委托单检测进度查询',
        'intent': '问检测进度/委托单/报告状态',
        'required': {
            'slot': 'order_no', 'header': '委托单号',
            'question': '请提供委托单号(BJ 开头,可在委托单回执上查看)',
            'options': [
                {'label': 'BJ2026', 'description': '演示单(检测中)'},
                {'label': 'BJ2027', 'description': '演示单(已完成)'},
            ],
            'normalize': 'upper',
            'extract_hint': 'order_no: 委托单号(如 BJ2026);没有 → 空串',
        },
        'action': {
            'kind': 'chatbi', 'server': 'metrology', 'ds_name': '计量业务平台',
            'tool': 'query_progress', 'params': {'order_no': 'order_no'},
            'stage': '向业务平台查询委托单 {order_no}…', 'error_noun': '业务接口',
            'notices': {
                'forbidden': {
                    'title': '无权查询',
                    'message': '委托单 {order_no} 不属于当前主体,无权查询。'
                               '请核对单号,或联系业务窗口。',
                    'order': '{order_no}'},
                'not_found': {
                    'title': '未找到委托单',
                    'message': '未找到委托单 {order_no},请核对单号后重试。',
                    'order': '{order_no}'},
            },
            'card': '_progress_card',
        },
    },
    'turnaround': {
        'label': '器具检测耗时/工作日咨询',
        'intent': "问送检后要多久出结果/多少个工作日/多长时间出报告出证"
                  "(问的是'这一次检测的耗时',不是'多久检一次')",
        'required': {
            'slot': 'instrument', 'header': '器具名称',
            'question': '请提供要咨询的器具名称(如 压力表/角膜曲率计)',
            'options': [],
            'extract_hint': 'instrument: 器具名称;指代("它/同一台")从会话背景解析',
        },
        'action': {
            'kind': 'chatbi', 'server': 'metrology', 'ds_name': '计量业务平台(通用)',
            'tool': 'query_turnaround', 'params': {'name': 'instrument'},
            'stage': '查询「{instrument}」检测时限…', 'error_noun': '检测时限',
            'notices': {
                'not_found': {
                    'title': '未找到该器具时限',
                    'message': '检测时限表中未找到「{instrument}」,请核对器具名称'
                               '后重试,或联系业务窗口确认。'},
            },
            'card': '_table_card',
        },
    },
    'qualification': {
        'label': '资质能力范围咨询(能否检测某器具)',
        'intent': '问你们能不能检/有没有资质检测某器具/是否承接',
        'required': {
            'slot': 'instrument', 'header': '器具名称',
            'question': '请提供要查询资质能力的器具名称',
            'options': [],
            'extract_hint': 'instrument: 器具名称;指代("它/同一台")从会话背景解析',
        },
        'action': {
            'kind': 'chatbi', 'server': 'metrology', 'ds_name': '计量业务平台(通用)',
            'tool': 'query_qualification', 'params': {'name': 'instrument'},
            'stage': '查询「{instrument}」的资质能力…', 'error_noun': '资质能力',
            'notices': {
                'not_found': {
                    'title': '未收录该器具资质',
                    'message': '资质能力表中未收录「{instrument}」,暂不具备该器具'
                               '的检测资质,或需人工确认承接范围。'},
            },
            'card': '_table_card',
        },
    },
    'instrument_info': {
        'label': '器具检测费/测量范围/准确度/检校周期/强检归类等价目信息查询',
        'intent': '问器具的检测费/收费标准/测量范围/准确度/检校周期(多久检'
                  '一次/复检时间间隔,不是单次耗时)/强检目录归类/技术规程号',
        'required': {
            'slot': 'instrument', 'header': '器具名称',
            'question': '请提供要查询的器具名称(如 角膜曲率计/电子秤)',
            'options': [],
            'extract_hint': 'instrument: 器具名称(如 压力表/血压计/电子秤);'
                            '指代("它/同一台")从会话背景解析',
        },
        'action': {
            'kind': 'chatbi', 'server': 'metrology', 'ds_name': '计量器具价目',
            'tool': 'query_instrument', 'params': {'name': 'instrument'},
            'stage': '查询器具「{instrument}」的价目信息…', 'error_noun': '器具价目',
            'notices': {
                'not_found': {
                    'title': '未找到该器具',
                    'message': '价目表中未找到「{instrument}」,请核对器具名称'
                               '后重试,或联系业务窗口确认。'},
            },
            'card': '_instrument_card',
        },
    },
}


def configured_scenarios() -> dict:
    return copy.deepcopy(_C)
