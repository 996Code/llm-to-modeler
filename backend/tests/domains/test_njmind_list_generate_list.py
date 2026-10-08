"""GenerateListTool 六步管线测试（B5）——全 fake（api/llm），真 PromptLoader 渲染 B4 模板。

覆盖：金路径（身份域=宿主值/开关全 int/@sql 全替换/按钮继承/宿主 id 脱敏）/
身份剥离/确认门带候选/确认门 resume 回填后重跑 validate/确认门 header 全量
（>12 字符缺失键）/SQL 降级置空不阻断/首验即过时 BOOL 归一不穿透 artifact/
机械修复循环/重生成触发（remaining 非空不静默）/merge 在 validate 后/
fetch_guide 失败 fail-closed。
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from domains.njmind_list import service_locator
from sdk.tool import ToolContext, ClarificationRaised


@pytest.fixture(autouse=True)
def list_api():
    api = MagicMock()
    service_locator.reset_list_transport()
    service_locator.set_api_for_testing(api)
    yield api
    service_locator.reset_list_transport()


@pytest.fixture
def loader():
    from sdk.prompt_loader import PromptLoader
    packs_root = Path(__file__).resolve().parents[2] / "src" / "domains"
    return PromptLoader(packs_root)


def make_ctx(llm, loader=None):
    events = []

    def _emit(*args):
        events.append(args)

    ctx = ToolContext(llm_client=llm, asset_client=None, conversation=None,
                      emit=_emit, conv_id="t")
    if loader is not None:
        object.__setattr__(ctx, "prompt_loader", loader)
    return ctx, events


def llm_returning(seq):
    m = MagicMock()
    m.chat_json.side_effect = list(seq)
    return m


def stages(events):
    return [e[1] for e in events if e[0] == "stage"]


# ── 宿主上下文（pack_params.njmind_list = INIT artifact {config, fields}）──

HOST_CONFIG = {
    "listConfigId": 100, "listCode": "list_dev_001", "listName": "设备台账",
    "listType": "list", "serverKey": "svc_main", "tableCode": "t_device",
    "partTableCode": "", "mainTableField": "", "dataVersion": 2,
    "listState": "published",
    "tableConfig": {
        "tableShowFields": [
            {"fieldTitleKey": "device_name", "fieldTitleText": "设备名称"}],
        "rowButtons": [
            {"buttonName": "编辑", "actionType": "edit", "buttonId": "btn_real_edit",
             "hiddenScript": "return false"}],
    },
    "buttonGroupConfig": {"buttonConfigList": [
        {"buttonName": "新增", "actionType": "add", "buttonId": "btn_real_add"}]},
    "queryPage": {"defPageSize": 20, "pageSizes": [10, 20, 50]},
}

HOST_FIELDS = [
    {"fieldKey": "device_name", "fieldTitle": "设备名称", "fieldType": 1, "source": "base"},
    {"fieldKey": "device_model", "fieldTitle": "设备型号", "fieldType": 1, "source": "base"},
    {"fieldKey": "create_time", "fieldTitle": "创建时间", "fieldType": 3, "source": "base"},
]

GUIDE = {
    "fieldCatalog": [
        {"fieldKey": "device_name", "fieldTitle": "设备名称", "fieldType": 1, "source": "base"},
        {"fieldKey": "device_model", "fieldTitle": "设备型号", "fieldType": 1, "source": "base"},
        {"fieldKey": "create_time", "fieldTitle": "创建时间", "fieldType": 3, "source": "base"},
    ],
    "enums": {"align": ["left", "center", "right"], "conditionMode": [10, 20],
              "listType": ["list", "card"], "pageType": [0, 1],
              "sortType": ["asc", "desc"]},
    "buttonCatalog": [
        {"commonAction": "add", "buttonName": "新增",
         "defaultButtonEventConfig": {"actionType": "add"}},
        {"commonAction": "edit", "buttonName": "编辑",
         "defaultButtonEventConfig": {"actionType": "edit"}},
    ],
}

COLUMN_TEMPLATE = [{"fieldType": 1, "renderType": "text", "width": 120,
                    "align": "left", "defaults": {"hideColumn": 0, "fieldShow": 1,
                                                  "defaultShow": 1}}]
BUTTON_TEMPLATE = [{"buttonType": "default"}]

PASS_VALIDATE = {"pass": True, "errors": [], "warnings": []}

INTENTS = {
    "needsClarification": False, "clarificationQuestions": [], "listSummary": "设备台账",
    "columnIntents": [{"fieldKey": "device_name", "note": "主列"}],
    "queryIntents": {"fuzzyFields": ["device_name"], "advanced": []},
    "buttonIntents": [
        {"buttonName": "新增", "actionType": "add", "location": "top"},
        {"buttonName": "编辑", "actionType": "edit", "location": "row"}],
    "paginationIntent": {"defPageSize": 20, "pageSizes": [10, 20, 50]},
    "filterIntents": [{"slot": "default", "mode": "sql", "desc": "只看今年创建"}],
    "permissionSqlIntent": {"mode": "sql", "desc": "本部门可见"},
}


def base_draft():
    """LLM 产物样例：带自造身份值 + 两个 @sql 占位 + 全 int 开关。"""
    return {
        "listCode": "llm_x", "listConfigId": 999, "listName": "LLM 起的名",
        "tableConfig": {
            "tableShowFields": [
                {"fieldTitleKey": "device_name", "fieldTitleText": "设备名称",
                 "fieldType": 1, "renderConfig": {"type": "text"}, "width": 120,
                 "align": "left", "hideColumn": 0, "fieldShow": 1, "defaultShow": 1,
                 "defaultFilter": 0, "defaultSort": 0, "defaultFixed": 0,
                 "defaultCalculate": 0}],
            "rowButtons": [
                {"buttonName": "编辑", "buttonId": "btn_edit", "buttonConfirm": 0,
                 "isTextButton": 0, "buttonEventConfig": {"actionType": "edit"}}],
            "tableQuerySort": [{"fieldKey": "create_time", "sortType": 20, "sort": 1}],
            "listTableQueryCondition": {"conditionMode": 20,
                                        "conditionWhereSqlText": "@sql:只看今年创建"},
            "dataPermissionSqlText": "@sql:本部门可见",
        },
        "conditionConfig": {"queryCondition": ["device_name"],
                            "defQueryCondition": ["device_name"]},
        "buttonGroupConfig": {"buttonConfigList": [
            {"buttonName": "新增", "buttonId": "btn_add", "buttonConfirm": 0,
             "isTextButton": 0, "buttonEventConfig": {"actionType": "add"}}]},
        "queryPage": {"defPageSize": 20, "pageSizes": [10, 20, 50], "pageType": 0},
    }


SQL_DEFAULT_OK = {"sql": "EXTRACT(YEAR FROM create_time) = "
                        "EXTRACT(YEAR FROM CURRENT_TIMESTAMP)", "note": ""}
SQL_PERM_OK = {"sql": "create_dep_id = #{NJMIND_LOGIN_USER_DEP_ID}", "note": ""}

SQL_BAD = {"sql": "t.create_time > TIMESTAMP '2026-01-01 00:00:00'", "note": ""}


def make_state(clarify=None, config=None, fields=None):
    state = {
        "user_input": "生成设备台账列表，只看今年创建的数据，本部门可见",
        "pack_params": {"njmind_list": {
            "config": HOST_CONFIG if config is None else config,
            "fields": HOST_FIELDS if fields is None else fields,
        }},
    }
    if clarify is not None:
        state["clarify_answers"] = clarify
    return state


def wire_passing_api(api, guide=GUIDE):
    api.get_guide.return_value = guide
    api.get_template.return_value = COLUMN_TEMPLATE
    api.validate.return_value = PASS_VALIDATE
    return api


# ── 金路径 ──────────────────────────────────────────────────────────

def test_金路径_生成校验通过产artifact(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    llm = llm_returning([INTENTS, base_draft(), SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx, events = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    art = result.artifact
    assert art is not None and art["type"] == "list-config"
    cfg = art["config"]
    # 身份域 = 宿主值（LLM 自造值被剥离）
    assert cfg["listCode"] == "list_dev_001"
    assert cfg["listConfigId"] == 100
    assert cfg["listName"] == "设备台账"
    assert art["hostMeta"] == {"listCode": "list_dev_001", "listName": "设备台账"}
    # 开关字段全 int
    col = cfg["tableConfig"]["tableShowFields"][0]
    for k in ("hideColumn", "fieldShow", "defaultShow", "defaultFilter",
              "defaultSort", "defaultFixed", "defaultCalculate"):
        assert isinstance(col[k], int) and not isinstance(col[k], bool), k
    # @sql 占位全被替换为校验通过的 SQL（无 t. 前缀、无年份字面量）
    assert "@sql:" not in json.dumps(cfg, ensure_ascii=False)
    assert cfg["tableConfig"]["listTableQueryCondition"]["conditionWhereSqlText"] == \
        "EXTRACT(YEAR FROM create_time) = EXTRACT(YEAR FROM CURRENT_TIMESTAMP)"
    assert cfg["tableConfig"]["dataPermissionSqlText"] == \
        "create_dep_id = #{NJMIND_LOGIN_USER_DEP_ID}"
    # 按钮继承：宿主 buttonId/脚本按语义键回填
    assert cfg["buttonGroupConfig"]["buttonConfigList"][0]["buttonId"] == "btn_real_add"
    assert cfg["tableConfig"]["rowButtons"][0]["buttonId"] == "btn_real_edit"
    assert cfg["tableConfig"]["rowButtons"][0]["hiddenScript"] == "return false"
    # validate 收到的是全量 VO + guide 目录
    sent_cfg, sent_catalog = list_api.validate.call_args.args
    assert sent_cfg["listCode"] == "list_dev_001"
    assert sent_catalog == GUIDE["fieldCatalog"]
    # SQL 子阶段 emit + 跨 pack 渲染 njmind_form sql_generate
    assert "generate_sql" in stages(events)
    sql_calls = [c for c in llm.chat_json.call_args_list
                 if c.kwargs.get("stage") == "generate_list.sql"]
    assert len(sql_calls) == 2
    assert "WHERE 片段" in sql_calls[0].args[0][0]["content"]


def test_金路径_宿主配置脱敏入prompt(loader, list_api):
    """fetch_existing：宿主 buttonId 换 button_host_N 占位、脚本整段剔除。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    llm = llm_returning([INTENTS, base_draft(), SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx, _ = make_ctx(llm, loader)
    GenerateListTool().execute(make_state(), ctx)
    gen_call = [c for c in llm.chat_json.call_args_list
                if c.kwargs.get("stage") == "generate_list.generate"][0]
    msg = gen_call.args[0][1]["content"]
    assert "btn_real_add" not in msg and "btn_real_edit" not in msg
    assert "return false" not in msg
    assert "button_host_1" in msg and "button_host_2" in msg


def test_身份剥离_llm自造listCode被覆盖(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    draft = base_draft()
    draft["listCode"] = "llm_hacked"
    draft["listDirConfig"] = {"hack": 1}
    draft["tableConfig"].pop("listTableQueryCondition")
    draft["tableConfig"].pop("dataPermissionSqlText")
    llm = llm_returning([INTENTS, draft])
    ctx, _ = make_ctx(llm, loader)
    result = GenerateListTool().execute(make_state(), ctx)
    cfg = result.artifact["config"]
    assert cfg["listCode"] == "list_dev_001"
    assert "llm_hacked" not in json.dumps(cfg, ensure_ascii=False)


# ── 确认门 ──────────────────────────────────────────────────────────

def ghost_draft():
    d = base_draft()
    d["tableConfig"]["tableShowFields"].append(
        {"fieldTitleKey": "ghost", "fieldTitleText": "幽灵列", "fieldType": 1,
         "renderConfig": {"type": "text"}})
    return d


def test_确认门_字段不在目录_带候选追问(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    llm = llm_returning([INTENTS, ghost_draft()])
    ctx, events = make_ctx(llm, loader)

    with pytest.raises(ClarificationRaised) as ei:
        GenerateListTool().execute(make_state(), ctx)

    q = json.loads(ei.value.questions[0])
    assert q["header"] == "ghost"
    labels = [o["label"] for o in q["options"]]
    assert "设备名称" in labels and "创建时间" in labels
    # 确认门先于 validate：未消耗上游校验
    list_api.validate.assert_not_called()


def test_确认门_resume_点选回填后重跑validate(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    # 首轮：ghost → 追问
    llm1 = llm_returning([INTENTS, ghost_draft()])
    ctx1, _ = make_ctx(llm1, loader)
    with pytest.raises(ClarificationRaised):
        GenerateListTool().execute(make_state(), ctx1)

    # resume：引擎注入 clarify_answers 后整管线重跑，回填不过 LLM
    llm2 = llm_returning([INTENTS, ghost_draft(), SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx2, _ = make_ctx(llm2, loader)
    result = GenerateListTool().execute(
        make_state(clarify={"ghost": "设备名称"}), ctx2)

    assert result.artifact is not None
    keys = [c["fieldTitleKey"] for c in
            result.artifact["config"]["tableConfig"]["tableShowFields"]]
    assert keys == ["device_name", "device_name"]   # ghost 已确定性回填
    # 回填后重跑 collect 判空再 validate
    list_api.validate.assert_called_once()


LONG_GHOST_KEY = "purchase_order_date"   # 19 字符 > 12，旧实现 [:12] 必截断


def long_ghost_draft():
    d = base_draft()
    d["tableConfig"]["tableShowFields"].append(
        {"fieldTitleKey": LONG_GHOST_KEY, "fieldTitleText": "采购单日期",
         "fieldType": 1, "renderConfig": {"type": "text"}})
    return d


def test_确认门_header全量_超12字符缺失键追问与回填闭环(loader, list_api):
    """I-2：header 是前端点选回传的答案键（{[q.header]: opt.label}），
    apply_field_answers 以完整缺失键匹配——header 截断会让回填失配、
    同一问题重复追问。必须用全量 missing（BPM 同款）。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    assert len(LONG_GHOST_KEY) > 12

    # 首轮：长键目录外 → 追问 header 必须是全量键（非截断）
    llm1 = llm_returning([INTENTS, long_ghost_draft()])
    ctx1, _ = make_ctx(llm1, loader)
    with pytest.raises(ClarificationRaised) as ei:
        GenerateListTool().execute(make_state(), ctx1)
    q = json.loads(ei.value.questions[0])
    assert q["header"] == LONG_GHOST_KEY

    # resume：前端按 header 键控回传 → 精确命中回填，不重复追问
    llm2 = llm_returning([INTENTS, long_ghost_draft(),
                          SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx2, _ = make_ctx(llm2, loader)
    result = GenerateListTool().execute(
        make_state(clarify={LONG_GHOST_KEY: "设备名称"}), ctx2)

    assert result.artifact is not None
    keys = [c["fieldTitleKey"] for c in
            result.artifact["config"]["tableConfig"]["tableShowFields"]]
    assert keys == ["device_name", "device_name"]   # 长键已确定性回填
    list_api.validate.assert_called_once()


# ── SQL 子管线降级 ──────────────────────────────────────────────────

def test_SQL降级_校验不过置空并警告不阻断(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    # 两个 @sql 位各尝试 3 次（初次+2 重试）全失败
    llm = llm_returning([INTENTS, base_draft()] + [SQL_BAD] * 6)
    ctx, events = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    assert result.artifact is not None          # 不阻断整单
    cfg = result.artifact["config"]
    # 默认过滤位回退中性不过滤形态（空 SQL 文本会 STRUCTURE_INVALID，不能只置空串）
    assert cfg["tableConfig"]["listTableQueryCondition"] == \
        {"conditionMode": 10, "queryCondition": {"logic": 10}}
    assert cfg["tableConfig"]["dataPermissionSqlText"] == ""
    warns = result.artifact["warnings"]
    assert any("默认过滤" in w and "置空" in w for w in warns)
    assert any("数据权限" in w and "置空" in w for w in warns)
    sql_calls = [c for c in llm.chat_json.call_args_list
                 if c.kwargs.get("stage") == "generate_list.sql"]
    assert len(sql_calls) == 6
    # 重试时错误回填给 LLM
    assert "上轮校验失败" in sql_calls[1].args[0][1]["content"]


def test_SQL_违规写真SQL文本也当占位重生成(loader, list_api):
    """LLM 不写 @sql: 前缀直接写真 SQL → 仍走子管线重生成替换。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    draft = base_draft()
    draft["tableConfig"]["dataPermissionSqlText"] = ""   # 只留默认过滤位
    draft["tableConfig"]["listTableQueryCondition"]["conditionWhereSqlText"] = \
        "select * from t_device"                          # 违规真 SQL
    llm = llm_returning([INTENTS, draft, SQL_DEFAULT_OK])
    ctx, _ = make_ctx(llm, loader)
    result = GenerateListTool().execute(make_state(), ctx)
    assert result.artifact["config"]["tableConfig"]["listTableQueryCondition"][
        "conditionWhereSqlText"] == SQL_DEFAULT_OK["sql"]


# ── 机械修复 / 重生成 ───────────────────────────────────────────────

def test_机械修复循环_bool转int后校验通过(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    draft = base_draft()
    draft["tableConfig"]["tableShowFields"][0]["defaultShow"] = True  # LLM 违规 boolean
    draft["tableConfig"].pop("listTableQueryCondition")
    draft["tableConfig"].pop("dataPermissionSqlText")
    list_api.validate.side_effect = [
        {"pass": False, "errors": [
            {"code": "BOOL_FIELD_NOT_INT",
             "field": "tableConfig.tableShowFields[0].defaultShow",
             "message": "开关字段必须 0|1 整数"}]},
        PASS_VALIDATE,
    ]
    llm = llm_returning([INTENTS, draft])
    ctx, events = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    assert result.artifact is not None
    col = result.artifact["config"]["tableConfig"]["tableShowFields"][0]
    assert col["defaultShow"] == 1 and isinstance(col["defaultShow"], int)
    assert list_api.validate.call_count == 2
    assert "validate_pass" in stages(events)


def test_首验即过_bool已归一_artifact无boolean(loader, list_api):
    """I-1：真实 A2 端点 FastJSON 对 JSON true 宽松强转 1（不报
    BOOL_FIELD_NOT_INT）——validate 首验即过时，归一必须已在首验前完成，
    否则 boolean 开关穿透进 artifact，前端应用后 Jackson 保存流整包拒绝。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)               # fake validate 首验即 pass
    draft = base_draft()
    draft["tableConfig"]["tableShowFields"][0]["defaultShow"] = True
    draft["tableConfig"]["tableShowFields"][0]["fieldShow"] = False
    draft["tableConfig"]["rowButtons"][0]["buttonConfirm"] = True
    draft["tableConfig"].pop("listTableQueryCondition")
    draft["tableConfig"].pop("dataPermissionSqlText")
    llm = llm_returning([INTENTS, draft])
    ctx, _ = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    assert result.artifact is not None
    assert list_api.validate.call_count == 1  # 首验即过，无修复轮

    def _bool_paths(node, prefix="cfg"):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, bool):
                    yield f"{prefix}.{k}={v}"
                else:
                    yield from _bool_paths(v, f"{prefix}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                yield from _bool_paths(item, f"{prefix}[{i}]")

    leaked = list(_bool_paths(result.artifact["config"]))
    assert leaked == [], f"boolean 穿透进 artifact: {leaked}"
    col = result.artifact["config"]["tableConfig"]["tableShowFields"][0]
    assert col["defaultShow"] == 1 and isinstance(col["defaultShow"], int)
    assert col["fieldShow"] == 0 and isinstance(col["fieldShow"], int)
    row = result.artifact["config"]["tableConfig"]["rowButtons"][0]
    assert row["buttonConfirm"] == 1 and isinstance(row["buttonConfirm"], int)
    # validate 收到的快照同样已归一
    sent_cfg = list_api.validate.call_args.args[0]
    assert not list(_bool_paths(sent_cfg, "sent"))


def test_重生成触发_remaining非空不静默失败(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    list_api.validate.side_effect = [
        {"pass": False, "errors": [
            {"code": "REQUIRED_MISSING", "field": "queryPage.defPageSize",
             "message": "分页缺省条数缺失"}]}] * 100
    draft = base_draft()
    draft["tableConfig"].pop("listTableQueryCondition")
    draft["tableConfig"].pop("dataPermissionSqlText")
    llm = llm_returning([INTENTS, draft, draft, draft, draft])
    ctx, events = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    assert result.artifact is None                       # 绝不静默产半成品
    assert result.validation_errors                      # 错误如实上抛
    gens = [c for c in llm.chat_json.call_args_list
            if c.kwargs.get("stage") == "generate_list.generate"]
    assert len(gens) == 4                                # 初次 + 3 次重生成
    assert "校验失败" in gens[1].args[0][1]["content"]    # 错误回填
    assert "当前草稿" in gens[1].args[0][1]["content"]    # 附当前草稿（BPM 同款）
    assert "validate_retry" in stages(events)
    assert "validate_fail" in stages(events)


def test_merge在validate后_宿主脚本不进校验(loader, list_api):
    """validate 检 LLM 产物纯度：宿主 hiddenScript 只在 validate 全过后经 merge 注入。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    draft = base_draft()
    draft["tableConfig"].pop("listTableQueryCondition")
    draft["tableConfig"].pop("dataPermissionSqlText")
    llm = llm_returning([INTENTS, draft])
    ctx, _ = make_ctx(llm, loader)

    result = GenerateListTool().execute(make_state(), ctx)

    sent_cfg = list_api.validate.call_args.args[0]
    assert "hiddenScript" not in sent_cfg["tableConfig"]["rowButtons"][0]
    row = result.artifact["config"]["tableConfig"]["rowButtons"][0]
    assert row["hiddenScript"] == "return false"         # merge 注入继承脚本
    assert row["buttonId"] == "btn_real_edit"


# ── fail-closed ─────────────────────────────────────────────────────

def test_fetch_guide失败_fail_closed(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    list_api.get_guide.return_value = {}
    llm = llm_returning([INTENTS, base_draft()])
    ctx, _ = make_ctx(llm, loader)

    with pytest.raises(ClarificationRaised):
        GenerateListTool().execute(make_state(), ctx)
    llm.chat_json.assert_not_called()


def test_宿主缺身份_fail_closed_不调validate(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    broken = {k: v for k, v in HOST_CONFIG.items() if k != "listCode"}
    llm = llm_returning([INTENTS, base_draft()])
    ctx, events = make_ctx(llm, loader)
    result = GenerateListTool().execute(make_state(config=broken), ctx)
    assert result.artifact is None
    list_api.validate.assert_not_called()
    assert "validate_fail" in stages(events)
    assert any("宿主" in str(e.get("message", "")) for e in result.validation_errors)


# ── C-1：source_artifact 通道回退（UI 聊天路径） ────────────────────

def test_host_context_source_artifact回退(loader, list_api):
    """C-1：designer UI 路径只有 context.artifact（无 pack_params）——
    host_context_from 应回退 source_artifact={config, fields} 解出 tableCode。"""
    from domains.njmind_list.tools.generate_list import host_context_from
    ctx = {"source_artifact": {"config": HOST_CONFIG, "fields": HOST_FIELDS}}
    host = host_context_from(ctx)
    assert host["config"]["tableCode"] == "t_device"
    assert host["config"]["listCode"] == "list_dev_001"
    assert len(host["fields"]) == 3


def test_source_artifact_整体是config本体防御(loader, list_api):
    """artifact 无 config/fields 键但含 tableShowFields → 视为 config 本体。"""
    from domains.njmind_list.tools.generate_list import host_context_from
    ctx = {"source_artifact": HOST_CONFIG}
    host = host_context_from(ctx)
    assert host["config"]["tableCode"] == "t_device"
    assert host["fields"] == []


def test_source_artifact通道全管线跑通(loader, list_api):
    """pack_params 空 + source_artifact 有值 → 六步管线用 artifact 通道宿主态。"""
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    llm = llm_returning([INTENTS, base_draft(), SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx, _ = make_ctx(llm, loader)

    result = GenerateListTool().execute({
        "user_input": "生成设备台账列表",
        "source_artifact": {"config": HOST_CONFIG, "fields": HOST_FIELDS},
    }, ctx)

    assert result.artifact is not None
    assert result.artifact["config"]["listCode"] == "list_dev_001"
    # guide 按宿主 tableCode 请求
    assert list_api.get_guide.call_args.args[0] == "t_device"


# ── 数据源确认门（tableCode 空 → 点选数据表） ────────────────────────

def no_table_state(clarify=None):
    state = {
        "user_input": "生成设备台账列表",
        "pack_params": {"njmind_list": {
            "config": {k: v for k, v in HOST_CONFIG.items()
                       if k not in ("tableCode", "partTableCode")},
            "fields": HOST_FIELDS,
        }},
    }
    if clarify is not None:
        state["clarify_answers"] = clarify
    return state


TABLES = [{"code": "t_device", "name": "设备台账表", "serverKey": "/njmind-modeler", "tableType": 1},
          {"code": "ceshi001", "name": "ceshi001表", "serverKey": "/njmind-modeler", "tableType": 1}]


def test_数据源确认门_tableCode空有表清单_追问点选(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    list_api.list_tables.return_value = TABLES
    llm = llm_returning([INTENTS, base_draft()])
    ctx, _ = make_ctx(llm, loader)

    with pytest.raises(ClarificationRaised) as ei:
        GenerateListTool().execute(no_table_state(), ctx)

    q = json.loads(ei.value.questions[0])
    assert q["header"] == "__select_table"
    assert q["multi_select"] is False
    # I-2（终审轮2）：候选数=上游返回数（前端可搜索下拉承载），
    # 截 20 会让 50 表租户约六成目标表点不到
    assert len(q["options"]) == len(TABLES)
    labels = [o["label"] for o in q["options"]]
    assert "ceshi001表" in labels
    # 页面同源口径：description 携带服务源与表类型
    assert "njmind-modeler" in q["options"][1]["description"]
    assert "低码表" in q["options"][1]["description"]
    # 追问先于 guide/LLM
    list_api.get_guide.assert_not_called()
    llm.chat_json.assert_not_called()


def test_数据源确认门_resume点选回填tableCode后全管线(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    list_api.list_tables.return_value = TABLES
    llm = llm_returning([INTENTS, base_draft(), SQL_DEFAULT_OK, SQL_PERM_OK])
    ctx, _ = make_ctx(llm, loader)

    no_server = no_table_state(clarify={"__select_table": "ceshi001表"})
    no_server["pack_params"]["njmind_list"]["config"]["serverKey"] = ""
    result = GenerateListTool().execute(no_server, ctx)

    assert result.artifact is not None
    # 回填后的 tableCode/serverKey 二元组进 guide 请求与最终 config 身份域
    assert list_api.get_guide.call_args.args[0] == "ceshi001"
    assert result.artifact["config"]["tableCode"] == "ceshi001"
    assert result.artifact["config"]["serverKey"] == "/njmind-modeler"


def test_数据源确认门_表清单空_维持fail_closed(loader, list_api):
    from domains.njmind_list.tools.generate_list import GenerateListTool
    wire_passing_api(list_api)
    list_api.list_tables.return_value = []
    llm = llm_returning([INTENTS, base_draft()])
    ctx, _ = make_ctx(llm, loader)

    with pytest.raises(ClarificationRaised) as ei:
        GenerateListTool().execute(no_table_state(), ctx)

    assert "__select_table" not in str(ei.value.questions[0])
    assert "获取列表字段目录失败" in ei.value.questions[0]
    llm.chat_json.assert_not_called()
