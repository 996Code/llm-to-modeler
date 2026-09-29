"""BPM 生成/修改管线测试:mock LLM + mock API,断言承接义务。"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from domains.njmind_bpm import service_locator
from domains.njmind_bpm.tools._pipeline_common import (
    inject_host_identity, merge_rule_details, mechanical_repair, host_context_from,
)


@pytest.fixture(autouse=True)
def bpm_api(monkeypatch):
    api = MagicMock()
    service_locator.reset_bpm_transport()
    service_locator.set_api_for_testing(api)
    yield api
    service_locator.reset_bpm_transport()


def make_ctx(llm, emit=None):
    """构造最小 ToolContext(prompt_loader=None,emit 收集)。"""
    from sdk.tool import ToolContext
    events = []

    def _emit(*args):
        events.append(args)

    return ToolContext(
        llm_client=llm, asset_client=None, conversation=None,
        emit=emit or _emit, conv_id="t"), events


def llm_returning(seq):
    """chat_json 依次返回 seq 的 fake LLM。"""
    m = MagicMock()
    m.chat_json.side_effect = list(seq)
    return m


VALID_RENDER = {
    "bpmnXml": "<xml/>",
    "rules": {"formKey": "leave_form", "approveRules": [{"taskId": "Activity_1"}],
              "ccRules": [], "branchRules": []},
    "validation": {"pass": True, "errors": [], "warnings": []},
}

HOST_ART = {
    "modelKey": "leave_process",
    "name": "请假流程",
    "formKey": "leave_form",
    "bpmnXml": None,
    "rules": {},
}

GOOD_INTENTS = {
    "needsClarification": False,
    "processSummary": "请假审批",
    "nodeIntents": [
        {"name": "主管审批", "nodeType": "USER_TASK", "approver": "主管(角色)"},
        {"name": "抄送HR", "nodeType": "CC", "approver": "HR(角色)"},
    ],
    "branchIntents": [],
}

CURRENT_DRAFT = {
    "processKey": "leave_process", "processName": "请假流程",
    "formKey": "leave_form",
    "topology": {
        "nodes": [
            {"key": "start", "type": "START_EVENT", "name": "开始"},
            {"key": "manager", "type": "USER_TASK", "name": "主管审批"},
            {"key": "end", "type": "END_EVENT", "name": "结束"},
        ],
        "edges": [
            {"key": "e1", "from": "start", "to": "manager"},
            {"key": "e2", "from": "manager", "to": "end"},
        ],
    },
    "approveRules": [{"nodeKey": "manager", "approveOptRange": 30,
                      "buttonName": "通过"}],
    "ccRules": [],
    "branchRules": [],
}


GOOD_DRAFT = {
    "formKey": "leave_form",
    "topology": {
        "nodes": [
            {"key": "start", "type": "START_EVENT", "name": "开始"},
            {"key": "manager", "type": "USER_TASK", "name": "主管审批"},
            {"key": "end", "type": "END_EVENT", "name": "结束"},
        ],
        "edges": [
            {"key": "e1", "from": "start", "to": "manager"},
            {"key": "e2", "from": "manager", "to": "end"},
        ],
    },
    "approveRules": [{"nodeKey": "manager", "approveOptRange": 20}],
    "ccRules": [],
    "branchRules": [],
}


# ── F1:宿主身份注入 ─────────────────────────────────────────

def test_f1_strips_llm_invented_identity_and_injects_host():
    draft = {"processKey": "hacked", "processName": "x", "topology": {}}
    out, err = inject_host_identity(draft, "leave_process", "请假流程")
    assert err is None
    assert out["processKey"] == "leave_process"
    assert out["processName"] == "请假流程"


def test_f1_fails_closed_without_host_identity():
    out, err = inject_host_identity({"processKey": "hacked"}, None, None)
    assert err and "processKey" not in out


def test_host_context_reads_source_artifact():
    host = host_context_from({"source_artifact": dict(HOST_ART)})
    assert host["processKey"] == "leave_process"
    assert host["formKey"] == "leave_form"


# ── I2:规则明细合并 ─────────────────────────────────────────

def test_i2_unmentioned_nodes_inherit_host_details():
    # 宿主 rules 是最终落库形态:键=taskId/nodeId/sequenceFlowId(BPMN id)
    host_rules = {
        "formKey": "f",
        "approveRules": [
            {"taskId": "Activity_a", "approveOptRange": 30, "buttonName": "通过",
             "deepDetail": {"x": 1}},
            {"taskId": "Activity_b", "approveOptRange": 20, "extra": "keep-me"},
        ],
        "ccRules": [{"nodeId": "cc1", "v": 1}],
        "branchRules": [{"sequenceFlowId": "Flow_e1", "rules": [{"formFieldKey": "days"}]}],
    }
    new_rules = {
        "approveRules": [{"nodeKey": "a", "approveOptRange": 40}],
        "ccRules": [],
        "branchRules": [],
    }
    # 旧拓扑(parse 产物,key=BPMN id+name) 与 新拓扑(LLM 重造,语义 key+name)
    host_topology = {
        "nodes": [{"key": "Activity_a", "name": "主管审批"},
                  {"key": "Activity_b", "name": "人事审批"}],
        "edges": [{"key": "Flow_e1", "name": "大于3天", "from": "x", "to": "y"}],
    }
    new_topology = {
        "nodes": [{"key": "a", "name": "主管审批"}, {"key": "b", "name": "人事审批"}],
        "edges": [{"key": "e1", "name": "大于3天", "from": "a", "to": "b"}],
    }
    merged = merge_rule_details(new_rules, host_rules,
                                host_topology=host_topology,
                                new_topology=new_topology)
    a = next(r for r in merged["approveRules"] if r["nodeKey"] == "a")
    b = next(r for r in merged["approveRules"] if r["nodeKey"] == "b")
    assert a["approveOptRange"] == 40          # LLM 覆盖
    assert a["deepDetail"] == {"x": 1}          # 明细经名称桥接继承
    assert b["extra"] == "keep-me"              # 未提及整条继承(键已译回语义域)
    # 分支规则同理:Flow_e1 → e1
    assert merged["branchRules"][0]["edgeKey"] == "e1"
    assert merged["branchRules"][0]["rules"] == [{"formFieldKey": "days"}]


def test_i2_host_rows_without_name_bridge_are_not_inherited():
    """桥接不上(名称缺失/不唯一)的宿主行不继承——宁缺勿错,绝不让 BPMN id 混进草稿域。"""
    host_rules = {
        "formKey": "f",
        "approveRules": [{"taskId": "Activity_x", "deepDetail": {"x": 1}}],
        "ccRules": [], "branchRules": [],
    }
    host_topology = {"nodes": [{"key": "Activity_x", "name": ""}], "edges": []}  # 无名
    new_topology = {"nodes": [{"key": "x", "name": "审批"}], "edges": []}
    merged = merge_rule_details({"approveRules": [], "ccRules": [], "branchRules": []},
                                host_rules, host_topology=host_topology,
                                new_topology=new_topology)
    assert merged["approveRules"] == []


def test_i2_missing_topologies_skip_inheritance():
    """拓扑缺位(空画布等)时桥接表为空,宿主行全部不继承(此前是假合并:键域错位全漏)。"""
    host_rules = {
        "formKey": "f",
        "approveRules": [{"taskId": "Activity_a", "deepDetail": {"x": 1}}],
        "ccRules": [], "branchRules": []}
    merged = merge_rule_details({"approveRules": [{"nodeKey": "a", "approveOptRange": 80}],
                                 "ccRules": [], "branchRules": []}, host_rules)
    assert len(merged["approveRules"]) == 1
    assert "deepDetail" not in merged["approveRules"][0]


# ── 机械修复 ─────────────────────────────────────────────────

def test_mechanical_repair_fixes_dangling_key_by_name():
    draft = {
        "unknownTop": 1,
        "topology": {"nodes": [{"key": "manager", "type": "USER_TASK", "name": "主管审批"}],
                     "edges": []},
        "approveRules": [{"nodeKey": "missing", "nodeName": "主管审批"}],
    }
    # Java BpmValidationIssue 总带 field 路径(如 approveRules[0].nodeKey)
    fixed, remaining = mechanical_repair(draft, [
        {"code": "RULE_TARGET_NOT_FOUND", "field": "approveRules[0].nodeKey",
         "message": "审批规则 nodeKey 未出现在拓扑中: missing"}])
    assert fixed["approveRules"][0]["nodeKey"] == "manager"
    assert "unknownTop" not in fixed
    assert remaining == []               # 修掉的那条不再残留


def test_mechanical_repair_strips_unknown_top_keys():
    fixed, _ = mechanical_repair({"bogus": 1, "topology": {}}, [])
    assert "bogus" not in fixed


def test_mechanical_repair_keeps_unfixed_rule_target_errors():
    """I-2:remaining 只剔除真正修掉的条目。两条 RULE_TARGET_NOT_FOUND,
    一条按名命中、一条不命中——remaining 只剩未修那条(修复回合1)。"""
    draft = {
        "topology": {"nodes": [
            {"key": "manager", "type": "USER_TASK", "name": "主管审批"}],
            "edges": []},
        "approveRules": [
            {"nodeKey": "missing", "nodeName": "主管审批"},   # 可按名回填
            {"nodeKey": "gone", "nodeName": "不存在节点"}],    # 无名可匹配
    }
    fixed, remaining = mechanical_repair(draft, [
        {"code": "RULE_TARGET_NOT_FOUND", "field": "approveRules[0].nodeKey",
         "message": "审批规则 nodeKey 未出现在拓扑中: missing"},
        {"code": "RULE_TARGET_NOT_FOUND", "field": "approveRules[1].nodeKey",
         "message": "审批规则 nodeKey 未出现在拓扑中: gone"},
    ])
    assert fixed["approveRules"][0]["nodeKey"] == "manager"   # 命中的被修
    assert fixed["approveRules"][1]["nodeKey"] == "gone"      # 未动的保留
    assert len(remaining) == 1                                # 只剩未修那条
    assert remaining[0]["field"] == "approveRules[1].nodeKey"


def test_mechanical_repair_error_with_code_and_field():
    """M-2:显式 code+field 用例——修复定位靠 field 路径而非消息文本碰巧。"""
    draft = {
        "topology": {"nodes": [
            {"key": "hr", "type": "CC_TASK", "name": "抄送HR"}],
            "edges": []},
        "approveRules": [],
        "ccRules": [{"nodeKey": "cc_typo", "nodeName": "抄送HR"}],
    }
    fixed, remaining = mechanical_repair(draft, [
        {"code": "RULE_TARGET_NOT_FOUND", "field": "ccRules[0].nodeKey",
         "message": "抄送规则 nodeKey 未出现在拓扑中: cc_typo"}])
    assert fixed["ccRules"][0]["nodeKey"] == "hr"
    assert remaining == []


# ── 生成管线端到端(mock)────────────────────────────────────

def test_generate_pipeline_emits_artifact_only_on_pass():
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = {"formFieldConfigVos": [
        {"fieldTitleKey": "days", "fieldTitleText": "天数", "formFieldType": 1}]}
    api.get_template.return_value = {}
    api.render.return_value = VALID_RENDER

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT])
    ctx, events = make_ctx(llm)
    state = {"user_input": "做个请假审批", "source_artifact": dict(HOST_ART)}

    result = GenerateProcessTool().execute(state, ctx)

    assert result.artifact is not None
    assert result.artifact["bpmnXml"] == "<xml/>"
    assert result.valid is True
    # F1:render 收到的 draft 带宿主身份
    sent = api.render.call_args_list[0].args[0]
    assert sent["processKey"] == "leave_process"
    assert sent["processName"] == "请假流程"
    # prompt 不含 UEL/XML 指令由模板静态保证,这里锁 stage 事件序列
    stages = [e[1] for e in events if e[0] == "stage"]
    assert "render_pass" in stages


def test_generate_pipeline_fails_closed_when_validation_missing():
    """上游 200 但响应缺 validation(网关截断/契约演进)→ 视同失败,不产半成品。"""
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    api.render.return_value = {"bpmnXml": "<xml/>", "rules": {}}  # 无 validation

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT] * 4)
    ctx, events = make_ctx(llm)
    state = {"user_input": "做个请假审批", "source_artifact": dict(HOST_ART)}

    result = GenerateProcessTool().execute(state, ctx)
    assert result.artifact is None
    stages = [e[1] for e in events if e[0] == "stage"]
    assert "render_fail" in stages
    assert "render_pass" not in stages


def test_generate_pipeline_fails_closed_without_host_identity():
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT])
    ctx, events = make_ctx(llm)
    state = {"user_input": "做个请假审批", "source_artifact": {}}

    result = GenerateProcessTool().execute(state, ctx)
    assert result.artifact is None
    assert api.render.call_count == 0
    assert any("processKey" in str(e.get("message", "")) for e in
               (result.validation_errors or [])) or \
           "宿主上下文缺少流程标识" in (result.validation_errors or [{}])[0].get("message", "")


def test_generate_pipeline_retries_then_succeeds():
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    bad = {"bpmnXml": None, "rules": None,
           "validation": {"pass": False, "errors": [{"message": "RULE_TARGET_NOT_FOUND"}],
                          "warnings": []}}
    api.render.side_effect = [bad, VALID_RENDER]

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT, GOOD_DRAFT])
    ctx, events = make_ctx(llm)
    state = {"user_input": "做个请假审批", "source_artifact": dict(HOST_ART)}
    result = GenerateProcessTool().execute(state, ctx)
    assert result.artifact is not None


# ── 整体重造语义(画布已有流程,新描述全量替换回显)──────────────

def test_generate_reads_existing_topology_as_context():
    """fetch_existing:画布有流程时 parse 出旧拓扑进 state 供 LLM 参考。"""
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    api.parse.return_value = {"draft": json.loads(json.dumps(CURRENT_DRAFT)),
                              "warnings": []}
    api.render.return_value = VALID_RENDER

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT])
    ctx, events = make_ctx(llm)
    host_art = {"modelKey": "leave_process", "name": "请假流程",
                "formKey": "leave_form", "bpmnXml": "<old/>", "rules": {}}
    state = {"user_input": "重新做个请假审批", "source_artifact": host_art}

    GenerateProcessTool().execute(state, ctx)
    assert state.get("existing_topology") is not None
    # generate 的 user 消息包含画布当前流程参考段
    gen_call = llm.chat_json.call_args_list[-1]
    user_msg = gen_call.args[0][1]["content"]
    assert "画布当前流程" in user_msg


def test_generate_full_regen_preserves_host_rule_details():
    """I2:整体重造时,新 draft 未提及的节点规则经名称桥接从宿主明细继承。"""
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    api.parse.return_value = {"draft": CURRENT_DRAFT, "warnings": []}
    rendered = {
        "bpmnXml": "<xml2/>",
        "rules": {"formKey": "leave_form", "approveRules": [
            {"taskId": "Activity_manager"}, {"taskId": "Activity_new"}],
            "ccRules": [], "branchRules": []},
        "validation": {"pass": True, "errors": [], "warnings": []},
    }
    api.render.return_value = rendered

    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT])
    ctx, events = make_ctx(llm)
    # 宿主 rules 是落库形态(taskId=BPMN id);CURRENT_DRAFT 拓扑里
    # Activity_manager 名称与新 draft 语义 key manager 对齐 → 明细可继承
    host_art = {"modelKey": "leave_process", "name": "请假流程",
                "formKey": "leave_form", "bpmnXml": "<old/>",
                "rules": {"formKey": "leave_form",
                          "approveRules": [{"taskId": "manager",  # parse 产物拓扑键(即 BPMN id)
                                            "approveOptRange": 80,
                                            "approveOptRangeSettings":
                                                {"checkedRoleIds": ["role_1"]}}],
                          "ccRules": [], "branchRules": []}}
    state = {"user_input": "重新生成", "source_artifact": host_art}

    result = GenerateProcessTool().execute(state, ctx)

    assert result.artifact is not None
    sent = api.render.call_args_list[-1].args[0]
    m = next(r for r in sent["approveRules"] if r["nodeKey"] == "manager")
    assert m.get("approveOptRangeSettings", {}).get("checkedRoleIds") == ["role_1"]


def test_generate_empty_canvas_skips_existing():
    """画布为空:existing_topology 为 None,不调 parse。"""
    from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    api.render.return_value = VALID_RENDER
    llm = llm_returning([GOOD_INTENTS, GOOD_DRAFT])
    ctx, _ = make_ctx(llm)
    state = {"user_input": "做个审批流", "source_artifact": {}}
    GenerateProcessTool().execute(state, ctx)
    assert state.get("existing_topology") is None
    api.parse.assert_not_called()


# ── 路由铁律 ────────────────────────────────────────────────

def test_router_single_business_tool():
    from domains.njmind_bpm.router import NjmindBpmRouter
    from domains.njmind_bpm.pack import create_registry
    reg = create_registry()
    names = [t.name for t in reg.all()]
    assert "generate_process" in names and "update_process" not in names
    assert "bpm_chat" in names


# ── 修复回合1:C-1 apply_changes 拓扑 ────────────────────────

def _update_api(draft, rendered=None):
    api = service_locator.get_api()
    api.parse.return_value = {"draft": json.loads(json.dumps(draft)), "warnings": []}
    api.get_form.return_value = {"formFieldConfigVos": [
        {"fieldTitleKey": "days", "fieldTitleText": "天数", "formFieldType": 1}]}
    api.get_template.return_value = {}
    api.render.return_value = rendered or VALID_RENDER
    return api


# ── 修复回合1:I-1 确认死循环 ─────────────────────────────────

# ── 修复回合1:I-3 patch 元字段剥离 ───────────────────────────

# ── 修复回合1:M-4 update 管线 form_catalog ───────────────────

# ── 修复回合2:N-1 桥边 key 唯一 ──────────────────────────────

# ── 修复回合2:N-2 add_node 消费 nodeType ─────────────────────

# ── 修复回合2:N-3 CC 节点 patch 分流 ─────────────────────────

# ── 修复回合2:N-4 prompt 词表对齐 CC_TASK ────────────────────

class TestBpmPromptVocabulary:
    """N-4:prompt 节点类型词表必须与 Java 契约(CC_TASK)一致,
    LLM 按词表输出的值不能被 Java 拒。#2 修复后词表以 guide 动态渲染,
    兜底路径(无 guide)用列表行格式且不含枚举码。"""

    @pytest.fixture
    def loader(self):
        from engine.prompt_loader import PromptLoader
        packs_root = Path(__file__).resolve().parents[3] / "src" / "domains"
        return PromptLoader(packs_root)

    def test_node_types_section_uses_cc_task(self, loader):
        out = loader.render("njmind_bpm", "_sections/node_types", guide={})
        assert "- CC_TASK |" in out               # 兜底词表行
        assert not any(line.startswith("- CC |") for line in out.splitlines())

    def test_parse_prompt_example_uses_cc_task(self, loader):
        out = loader.render("njmind_bpm", "parse", guide={})
        assert '"nodeType": "CC_TASK"' in out
        assert '"nodeType": "CC"' not in out

    def test_generate_prompt_renders_with_cc_task(self, loader):
        out = loader.render("njmind_bpm", "generate",
                            guide={}, process_template={}, rule_templates={},
                            form_catalog=[])
        assert "- CC_TASK |" in out
        assert not any(line.startswith("- CC |") for line in out.splitlines())


# ── 结构化分支字段确认门 ───────────────────────────────────

CATALOG = [
    {"key": "wenben1", "title": "文本输入框", "type": 0, "typeName": "TEXT"},
    {"key": "wenben2", "title": "文本输入框2", "type": 0, "typeName": "TEXT"},
    {"key": "bumen", "title": "部门", "type": 6, "typeName": "DEPARTMENT"},
]


def test_collect_unknown_branch_fields_silent_title_fix():
    from domains.njmind_bpm.tools._pipeline_common import collect_unknown_branch_fields
    draft = {"branchRules": [{"edgeKey": "e1", "rules": [
        {"formFieldKey": "文本输入框2", "logicType": 70, "values": ["3"]}]}]}
    unknown = collect_unknown_branch_fields(draft, CATALOG)
    assert unknown == []                       # 名称精确命中:静默修正
    assert draft["branchRules"][0]["rules"][0]["formFieldKey"] == "wenben2"


def test_collect_unknown_branch_fields_reports_with_candidates():
    from domains.njmind_bpm.tools._pipeline_common import collect_unknown_branch_fields
    draft = {"branchRules": [{"edgeKey": "e1", "rules": [
        {"formFieldKey": "leave_days", "logicType": 70, "values": ["3"]}]}]}
    unknown = collect_unknown_branch_fields(draft, CATALOG)
    assert len(unknown) == 1
    assert unknown[0]["missing"] == "leave_days"
    labels = [c["label"] for c in unknown[0]["candidates"]]
    assert labels  # 候选非空


def test_apply_branch_field_answers_resolves_by_title_and_key():
    from domains.njmind_bpm.tools._pipeline_common import apply_branch_field_answers
    draft = {"branchRules": [
        {"edgeKey": "e1", "rules": [{"formFieldKey": "leave_days"}]},
        {"edgeKey": "e2", "rules": [{"formFieldKey": "leave_days"}]}]}
    n = apply_branch_field_answers(draft, {"leave_days": "文本输入框2"}, CATALOG)
    assert n == 2
    assert draft["branchRules"][0]["rules"][0]["formFieldKey"] == "wenben2"
    draft2 = {"branchRules": [
        {"edgeKey": "e1", "rules": [{"formFieldKey": "leave_days"}]}]}
    n2 = apply_branch_field_answers(draft2, {"leave_days": "bumen"}, CATALOG)
    assert n2 == 1   # 也可直接给 key
    assert draft2["branchRules"][0]["rules"][0]["formFieldKey"] == "bumen"


def test_mechanical_repair_downgrades_incompatible_operator():
    """操作符兼容降级:部门字段配"大于"(70)→按 allowedValues 首个兼容项替换。"""
    from domains.njmind_bpm.tools._pipeline_common import mechanical_repair
    draft = {
        "topology": {"nodes": [], "edges": []},
        "branchRules": [{"edgeKey": "e1", "rules": [
            {"formFieldKey": "bumen1", "logicType": 70, "values": ["研发部"]}]}],
    }
    errors = [{
        "code": "FIELD_OPERATOR_INCOMPATIBLE",
        "field": "branchRules[0].rules[0].logicType",
        "message": "字段类型 6 不支持操作符 70",
        "allowedValues": ["10", "20", "30", "40", "50", "60"],
    }]
    fixed, remaining = mechanical_repair(draft, errors)
    assert fixed["branchRules"][0]["rules"][0]["logicType"] == 10
    assert remaining == []
