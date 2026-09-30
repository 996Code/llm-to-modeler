"""njmind_list 管线公共件测试（B3）——身份注入/字段确认门/按钮合并/机械修复。

错误码与错误条目形态对齐 A2 ListConfigValidator 契约
（{code, field, message}，field 为点路径）。
"""
from domains.njmind_list.tools._pipeline_common import (
    inject_host_identity, merge_buttons, mechanical_repair,
    collect_unknown_fields, apply_field_answers)

HOST = {"listCode": "real_code", "serverKey": "svc", "tableCode": "t1",
        "listName": "设备台账", "listType": "list"}

CATALOG = [{"value": "f1", "label": "设备名称"},
           {"value": "f2", "label": "设备编号"}]


# ── inject_host_identity ──────────────────────────────────────────

def test_inject_host_identity_剥自造listCode():
    out, err = inject_host_identity({"listCode": "llm_made_up", "tableConfig": {}}, HOST)
    assert err is None and out["listCode"] == "real_code"


def test_inject_host_identity_缺身份_fail_closed():
    out, err = inject_host_identity({}, {"listCode": "c"})
    assert err and "宿主" in err


def test_inject_host_identity_13身份字段全剥全注入():
    draft = {"listCode": "x", "dataVersion": 999, "listState": "llm_draft",
             "processDefinitionKey": "llm_key", "tableConfig": {"keep": 1}}
    host = dict(HOST, dataVersion=2, listState="published",
                processDefinitionKey="proc_1", partTableCode="")
    out, err = inject_host_identity(draft, host)
    assert err is None
    assert out["dataVersion"] == 2 and out["listState"] == "published"
    assert out["processDefinitionKey"] == "proc_1"
    assert out["tableConfig"] == {"keep": 1}
    # LLM 自造身份值被剥：listState 不再是 llm_draft
    assert "llm_draft" not in out.values()


def test_inject_host_identity_宿主缺serverKey也拒绝():
    host = {"listCode": "c", "tableCode": "t"}
    out, err = inject_host_identity({"tableConfig": {}}, host)
    assert err and "宿主" in err


# ── collect_unknown_fields ────────────────────────────────────────

def test_collect_unknown_fields_列与筛选字段():
    catalog = [{"value": "f1", "label": "设备名称"}]
    draft = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "f1"}, {"fieldTitleKey": "ghost"}]}}
    unknown = collect_unknown_fields(draft, catalog)
    assert [u["missing"] for u in unknown] == ["ghost"]
    assert unknown[0]["candidates"][0]["label"] == "设备名称"


def test_collect_unknown_fields_基础消费点与去重():
    draft = {
        "tableConfig": {
            "tableShowFields": [{"fieldTitleKey": "ghost"}],
            "cardTitleFields": ["f1", "ghost"],
        },
        "conditionConfig": {"queryCondition": ["ghost", "f2"]},
        "advanceConfig": [{"advanceConfigField": [{"bizTableField": "ghost2"}]}],
    }
    unknown = collect_unknown_fields(draft, CATALOG)
    assert [u["missing"] for u in unknown] == ["ghost", "ghost2"]
    # 候选是全量目录（BPM 同款，不截断）
    assert [c["label"] for c in unknown[0]["candidates"]] == ["设备名称", "设备编号"]
    assert all(c.get("description") for c in unknown[0]["candidates"])


def test_collect_unknown_fields_全命中与空目录():
    ok = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "f1"}],
                          "cardTitleFields": ["f2"]},
          "conditionConfig": {"queryCondition": ["f1"]},
          "advanceConfig": [{"advanceConfigField": [{"bizTableField": "f2"}]}]}
    assert collect_unknown_fields(ok, CATALOG) == []
    # 空目录视同无法判定（fetch_guide 已 fail-closed），不出确认门
    assert collect_unknown_fields({"conditionConfig": {"queryCondition": ["x"]}}, []) == []


def test_collect_unknown_fields_排序与嵌套查询条件():
    # I2：与 A2 validateFieldReferences 同口径——sort fieldKey（空跳过）+
    # boolCond 树递归（tableConfig 查询条件卡 + combineConfig 嵌套卡）
    draft = {
        "tableConfig": {
            "tableQuerySort": [{"fieldKey": "ghost", "sortType": 10, "sort": 1},
                               {"fieldKey": "", "sortType": 10, "sort": 1}],
            "listTableQueryCondition": {
                "conditionMode": 10,
                "queryCondition": {"fieldKey": "", "conditions": [
                    {"fieldKey": "ghost"},
                    {"conditions": [{"fieldKey": "f1"}]}]}},
        },
        "combineConfig": [
            {"name": "我的待办", "condition": [
                {"name": "全部", "listTableQueryCondition": {
                    "conditionMode": 10,
                    "queryCondition": {"fieldKey": "ghost2"}}}]},
        ],
    }
    unknown = collect_unknown_fields(draft, CATALOG)
    assert [u["missing"] for u in unknown] == ["ghost", "ghost2"]
    assert all(u["candidates"][0]["label"] == "设备名称" for u in unknown)


# ── apply_field_answers ───────────────────────────────────────────

def test_apply_field_answers_点选回填不过LLM():
    draft = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "ghost", "fieldTitleText": "设备"}]},
             "conditionConfig": {"queryCondition": ["ghost"]}}
    assert apply_field_answers(draft, {"ghost": "设备名称"}, CATALOG) is None
    assert draft["tableConfig"]["tableShowFields"][0]["fieldTitleKey"] == "f1"
    assert draft["conditionConfig"]["queryCondition"] == ["f1"]


def test_apply_field_answers_筛选与卡片位也回填():
    draft = {"advanceConfig": [{"advanceConfigField": [{"bizTableField": "ghost"}]}],
             "tableConfig": {"cardTitleFields": ["ghost"]}}
    apply_field_answers(draft, {"ghost": "设备编号"}, CATALOG)
    assert draft["advanceConfig"][0]["advanceConfigField"][0]["bizTableField"] == "f2"
    assert draft["tableConfig"]["cardTitleFields"] == ["f2"]


def test_apply_field_answers_未答与无法翻译保持原样():
    draft = {"conditionConfig": {"queryCondition": ["ghost", "f1"]}}
    apply_field_answers(draft, {"ghost": "不存在的标签"}, CATALOG)
    assert draft["conditionConfig"]["queryCondition"] == ["ghost", "f1"]


def test_apply_field_answers_排序与嵌套查询条件也回填():
    # _field_slots 为 collect/apply 共用扫描器：I2 扩展点两侧同时生效
    draft = {"tableConfig": {"tableQuerySort": [{"fieldKey": "ghost"}],
                             "listTableQueryCondition": {
                                 "queryCondition": {"fieldKey": "ghost"}}},
             "combineConfig": [{"condition": [{"listTableQueryCondition": {
                 "queryCondition": {"conditions": [{"fieldKey": "ghost"}]}}}]}]}
    apply_field_answers(draft, {"ghost": "设备名称"}, CATALOG)
    assert draft["tableConfig"]["tableQuerySort"][0]["fieldKey"] == "f1"
    assert draft["tableConfig"]["listTableQueryCondition"]["queryCondition"]["fieldKey"] == "f1"
    cond = draft["combineConfig"][0]["condition"][0]["listTableQueryCondition"]
    assert cond["queryCondition"]["conditions"][0]["fieldKey"] == "f1"


# ── merge_buttons ─────────────────────────────────────────────────

def test_merge_buttons_语义键继承脚本与buttonId():
    host = {"tableConfig": {"rowButtons": [
        {"buttonId": "button-abc", "buttonName": "编辑", "actionType": "edit",
         "hiddenScript": "return false", "permissionCode": ["p1"]}]}}
    draft = {"tableConfig": {"rowButtons": [
        {"buttonName": "编辑", "actionType": "edit", "buttonId": "button_list_1"}]}}
    merge_buttons(draft, host)
    b = draft["tableConfig"]["rowButtons"][0]
    assert b["buttonId"] == "button-abc" and b["hiddenScript"] == "return false"
    assert b["permissionCode"] == ["p1"]


def test_merge_buttons_同名单按钮按顺序对位():
    # host 两个同名"删除"，draft 两个 → 按出现序对位不交叉继承
    host = {"tableConfig": {"rowButtons": [
        {"buttonId": "del-1", "buttonName": "删除", "actionType": "jump",
         "disableScript": "s1"},
        {"buttonId": "del-2", "buttonName": "删除", "actionType": "jump",
         "disableScript": "s2"}]}}
    draft = {"tableConfig": {"rowButtons": [
        {"buttonName": "删除", "actionType": "jump", "buttonId": "button_list_1"},
        {"buttonName": "删除", "actionType": "jump", "buttonId": "button_list_2"}]}}
    merge_buttons(draft, host)
    bs = draft["tableConfig"]["rowButtons"]
    assert (bs[0]["buttonId"], bs[0]["disableScript"]) == ("del-1", "s1")
    assert (bs[1]["buttonId"], bs[1]["disableScript"]) == ("del-2", "s2")


def test_merge_buttons_事件脚本与顶部按钮组继承():
    host = {"buttonGroupConfig": {"buttonConfigList": [
        {"buttonId": "top-1", "buttonName": "导出", "actionType": "commonAction",
         "buttonEventConfig": {"eventScript": "es", "actionType": "commonAction"},
         "beforeNotifyScript": "bns", "notifyScript": "ns"}]}}
    draft = {"buttonGroupConfig": {"buttonConfigList": [
        {"buttonName": "导出", "buttonId": "button_list_1",
         "buttonEventConfig": {"actionType": "commonAction"}}]}}
    merge_buttons(draft, host)
    b = draft["buttonGroupConfig"]["buttonConfigList"][0]
    assert b["buttonId"] == "top-1"
    assert b["buttonEventConfig"]["eventScript"] == "es"
    assert b["beforeNotifyScript"] == "bns" and b["notifyScript"] == "ns"


def test_merge_buttons_新按钮保留LLM产物并补占位id():
    draft = {"tableConfig": {"rowButtons": [
        {"buttonName": "全新按钮", "actionType": "jump", "buttonId": "button_list_9"}]}}
    merge_buttons(draft, HOST)
    b = draft["tableConfig"]["rowButtons"][0]
    assert b["buttonId"] == "button_list_9"   # 保留 LLM 占位


def test_merge_buttons_组合筛选id与conditionId按name继承():
    host = {"combineConfig": [
        {"id": "comb-1", "name": "我的待办", "defaultConditionId": "cond-a",
         "condition": [{"conditionId": "cond-a", "name": "全部"},
                       {"conditionId": "cond-b", "name": "未完成"}]}]}
    draft = {"combineConfig": [
        {"id": "llm-id", "name": "我的待办", "defaultConditionId": "llm-cond",
         "condition": [{"conditionId": "llm-cond", "name": "全部"},
                       {"conditionId": "llm-cond2", "name": "未完成"}]}]}
    merge_buttons(draft, host)
    c = draft["combineConfig"][0]
    assert c["id"] == "comb-1"
    assert [x["conditionId"] for x in c["condition"]] == ["cond-a", "cond-b"]
    # defaultConditionId 随映射改写，不断链
    assert c["defaultConditionId"] == "cond-a"


def test_merge_buttons_同名不同actionType不误继承():
    host = {"tableConfig": {"rowButtons": [
        {"buttonId": "h1", "buttonName": "删除", "actionType": "jump",
         "hiddenScript": "hs"}]}}
    draft = {"tableConfig": {"rowButtons": [
        {"buttonName": "删除", "actionType": "custom", "buttonId": "button_list_1"}]}}
    merge_buttons(draft, host)
    b = draft["tableConfig"]["rowButtons"][0]
    assert b["buttonId"] == "button_list_1" and "hiddenScript" not in b


# ── mechanical_repair ─────────────────────────────────────────────

def test_mechanical_repair_bool转0_1():
    draft = {"tableConfig": {"tableShowFields": [{"defaultShow": True}]}}
    errors = [{"code": "BOOL_FIELD_NOT_INT",
               "field": "tableConfig.tableShowFields[0].defaultShow",
               "message": "defaultShow 必须为 0|1 整数"}]
    out, remaining = mechanical_repair(draft, errors)
    assert out["tableConfig"]["tableShowFields"][0]["defaultShow"] == 1
    assert remaining == []


def test_mechanical_repair_布尔前置归一无需错误驱动():
    # Jackson 对 Integer 遇 true 整包拒绝：未发 validate 前就要全表递归归一
    draft = {"tableConfig": {"tableShowFields": [{"hideColumn": False, "defaultShow": True}],
                             "rowButtons": [{"buttonConfirm": True, "isTextButton": False}]},
             "buttonGroupConfig": {"buttonConfigList": [{"buttonConfirm": False}]},
             "combineConfig": [{"condition": [{"resetFilter": True, "showNum": False}]}]}
    out, remaining = mechanical_repair(draft, [])
    col = out["tableConfig"]["tableShowFields"][0]
    assert (col["hideColumn"], col["defaultShow"]) == (0, 1)
    rb = out["tableConfig"]["rowButtons"][0]
    assert (rb["buttonConfirm"], rb["isTextButton"]) == (1, 0)
    assert out["buttonGroupConfig"]["buttonConfigList"][0]["buttonConfirm"] == 0
    cond = out["combineConfig"][0]["condition"][0]
    assert (cond["resetFilter"], cond["showNum"]) == (1, 0)
    assert remaining == []
    # 纯函数：不就地改入参
    assert draft["tableConfig"]["tableShowFields"][0]["defaultShow"] is True


def test_mechanical_repair_越界开关钳到合法域():
    draft = {"tableConfig": {"tableShowFields": [{"defaultShow": 2}]}}
    errors = [{"code": "BOOL_FIELD_NOT_INT",
               "field": "tableConfig.tableShowFields[0].defaultShow",
               "message": "开关字段必须是整数 0 或 1"}]
    out, remaining = mechanical_repair(draft, errors)
    assert out["tableConfig"]["tableShowFields"][0]["defaultShow"] == 1
    assert remaining == []


def test_mechanical_repair_枚举越界取message合法值首项():
    draft = {"tableConfig": {"tableShowFields": [{"align": "middle"}]}}
    errors = [{"code": "ENUM_OUT_OF_RANGE",
               "field": "tableConfig.tableShowFields[0].align",
               "message": "枚举值 middle 非法，合法值: [left, center, right]"}]
    out, remaining = mechanical_repair(draft, errors)
    assert out["tableConfig"]["tableShowFields"][0]["align"] == "left"
    assert remaining == []


def test_mechanical_repair_枚举数值域转int():
    draft = {"queryPage": {"pageType": 5, "defPageSize": 20, "pageSizes": [20, 50]}}
    errors = [{"code": "ENUM_OUT_OF_RANGE", "field": "queryPage.pageType",
               "message": "枚举值 5 非法，合法值: [0, 1]"}]
    out, remaining = mechanical_repair(draft, errors)
    assert out["queryPage"]["pageType"] == 0 and remaining == []


def test_mechanical_repair_列名精确匹配catalog回填key():
    draft = {"tableConfig": {"tableShowFields": [
        {"fieldTitleKey": "设备名称", "fieldTitleText": "设备名称"}]}}
    errors = [{"code": "FIELD_NOT_IN_CATALOG",
               "field": "tableConfig.tableShowFields[0].fieldTitleKey",
               "message": "字段 设备名称 不在 catalog 中，合法字段: [f1, f2]"}]
    out, remaining = mechanical_repair(draft, errors, catalog=CATALOG)
    assert out["tableConfig"]["tableShowFields"][0]["fieldTitleKey"] == "f1"
    assert remaining == []


def test_mechanical_repair_列名匹配不上保留错误():
    draft = {"tableConfig": {"tableShowFields": [
        {"fieldTitleKey": "ghost", "fieldTitleText": "不存在的列"}]}}
    errors = [{"code": "FIELD_NOT_IN_CATALOG",
               "field": "tableConfig.tableShowFields[0].fieldTitleKey",
               "message": "字段 ghost 不在 catalog 中，合法字段: [f1, f2]"}]
    out, remaining = mechanical_repair(draft, errors, catalog=CATALOG)
    assert out["tableConfig"]["tableShowFields"][0]["fieldTitleKey"] == "ghost"
    assert remaining == errors


def test_mechanical_repair_结构坏列不删除错误保留():
    # B3 审查裁决（C1+I1）：STRUCTURE_INVALID 不做删除式修复，坏条目与错误
    # 原样保留 remaining，由 ≤3 次重生成兜底
    draft = {"tableConfig": {"tableShowFields": [
        {"fieldTitleKey": "f1", "renderConfig": {"type": "text"}},
        {"fieldTitleKey": "f2"}]}}   # 第 2 列缺 renderConfig
    errors = [{"code": "STRUCTURE_INVALID",
               "field": "tableConfig.tableShowFields[1]",
               "message": "列配置及 renderConfig 不能为空"}]
    out, remaining = mechanical_repair(draft, errors)
    assert [c["fieldTitleKey"] for c in out["tableConfig"]["tableShowFields"]] == ["f1", "f2"]
    assert remaining == errors


def test_mechanical_repair_SQL空文本不删卡错误保留():
    # C1 复现用例：SQL 空文本是内容级错误——机械修复不得删 holder/整卡，
    # 也不得误标已修（旧实现 del d["tableConfig"] 且 remaining==[]）
    draft = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "f1"}],
                             "rowButtons": [{"buttonName": "删除"}],
                             "cardTitleFields": ["f1"],
                             "listTableQueryCondition": {"conditionMode": 20,
                                                         "conditionWhereSqlText": ""}}}
    errors = [{"code": "STRUCTURE_INVALID",
               "field": "tableConfig.listTableQueryCondition.conditionWhereSqlText",
               "message": "SQL 模式下 conditionWhereSqlText 不能为空"}]
    out, remaining = mechanical_repair(draft, errors)
    assert remaining == errors                       # 未真正修复，逐条保留
    table = out["tableConfig"]
    assert table["tableShowFields"] == [{"fieldTitleKey": "f1"}]      # draft 完整不丢
    assert table["rowButtons"] == [{"buttonName": "删除"}]
    assert table["cardTitleFields"] == ["f1"]
    assert table["listTableQueryCondition"] == {"conditionMode": 20,
                                                "conditionWhereSqlText": ""}
    assert draft["tableConfig"]["listTableQueryCondition"] is not table["listTableQueryCondition"]


def test_mechanical_repair_同数组多条结构错误不删好条目():
    # I1 复现用例：同数组两条坏列——不做删除，好列全保、坏列存活、错误逐条保留
    cols = [{"fieldTitleKey": "f1", "renderConfig": {"type": "text"}},   # 好
            {"fieldTitleKey": "f5"},                                     # 坏：缺 renderConfig
            {"fieldTitleKey": "f6"},                                     # 坏：缺 renderConfig
            {"fieldTitleKey": "f2", "renderConfig": {"type": "text"}}]   # 好
    draft = {"tableConfig": {"tableShowFields": cols}}
    errors = [{"code": "STRUCTURE_INVALID", "field": "tableConfig.tableShowFields[1]",
               "message": "列配置及 renderConfig 不能为空"},
              {"code": "STRUCTURE_INVALID", "field": "tableConfig.tableShowFields[2]",
               "message": "列配置及 renderConfig 不能为空"}]
    out, remaining = mechanical_repair(draft, errors)
    assert [c["fieldTitleKey"] for c in out["tableConfig"]["tableShowFields"]] \
        == ["f1", "f5", "f6", "f2"]
    assert remaining == errors


def test_mechanical_repair_查询条件卡queryCondition缺失不补数组():
    # N1 复现用例（路径 A）：mode=10 漏 queryCondition——该字段是单对象
    # POJO（非数组），机械补 [] 会毒化成 Jackson 整包拒绝且误标已修；
    # 不修保留 remaining 走重生成，tableConfig 四要素完整不丢
    draft = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "f1"}],
                             "rowButtons": [{"buttonName": "删除"}],
                             "cardTitleFields": ["f1"],
                             "listTableQueryCondition": {"conditionMode": 10}}}
    errors = [{"code": "STRUCTURE_INVALID",
               "field": "tableConfig.listTableQueryCondition.queryCondition",
               "message": "字段过滤模式下 queryCondition 不能为空"}]
    out, remaining = mechanical_repair(draft, errors)
    assert remaining == errors                       # 未真正修复，逐条保留
    lqc = out["tableConfig"]["listTableQueryCondition"]
    assert lqc == {"conditionMode": 10}              # 不写入数组形状
    assert "queryCondition" not in lqc
    table = out["tableConfig"]
    assert table["tableShowFields"] == [{"fieldTitleKey": "f1"}]      # 完整不丢
    assert table["rowButtons"] == [{"buttonName": "删除"}]
    assert table["cardTitleFields"] == ["f1"]


def test_mechanical_repair_conditionMode缺省不补值():
    # N1 复现用例（路径 B）：整卡漏 conditionMode——旧实现补 mode 10 的
    # 同时把 queryCondition 侧补成 []，连同 mode 补值一并收敛为不修
    draft = {"tableConfig": {"tableShowFields": [{"fieldTitleKey": "f1"}],
                             "listTableQueryCondition": {"queryCondition":
                                 {"fieldKey": "f1", "conditions": []}}}}
    errors = [{"code": "STRUCTURE_INVALID",
               "field": "tableConfig.listTableQueryCondition.conditionMode",
               "message": "conditionMode 不能为空"}]
    out, remaining = mechanical_repair(draft, errors)
    assert remaining == errors                       # 未修，逐条保留走重生成
    assert out["tableConfig"]["listTableQueryCondition"] == \
        {"queryCondition": {"fieldKey": "f1", "conditions": []}}
    assert "conditionMode" not in out["tableConfig"]["listTableQueryCondition"]


def test_mechanical_repair_pageSizes去重与defPageSize对齐():
    draft = {"queryPage": {"pageSizes": [20, 20, 0, 50], "defPageSize": 999}}
    errors = [{"code": "STRUCTURE_INVALID", "field": "queryPage.pageSizes",
               "message": "pageSizes 必须是互不重复的正整数"},
              {"code": "STRUCTURE_INVALID", "field": "queryPage.defPageSize",
               "message": "defPageSize 必须属于 pageSizes"}]
    out, remaining = mechanical_repair(draft, errors)
    assert out["queryPage"]["pageSizes"] == [20, 50]
    assert out["queryPage"]["defPageSize"] == 20
    assert remaining == []


def test_mechanical_repair_修不掉的保留():
    draft = {"tableConfig": {"tableShowFields": [
        {"fieldTitleKey": "f1", "formatterScript": "return 1"}]}}
    errors = [{"code": "SCRIPT_NOT_ALLOWED",
               "field": "tableConfig.tableShowFields[0].formatterScript",
               "message": "AI 产物不允许生成脚本"},
              {"code": "REQUIRED_MISSING", "field": "queryPage",
               "message": "queryPage 必须存在"}]
    out, remaining = mechanical_repair(draft, errors)
    assert remaining == errors   # 脚本位/必填缺失非机械可修，走重生成
