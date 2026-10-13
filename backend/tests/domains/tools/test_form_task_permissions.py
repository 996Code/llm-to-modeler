"""表单审批节点权限生成与追问回填测试（Plan1 Task5）。

覆盖：
- 纯 resolve 层：节点名→真实 id 直选置位、存量 id 透传不翻开关、
  抄送节点三态过滤（Detail 直选 / Add 剔出候选 / Add 透传保留）、
  空数组剥键保旧值、同组部分忽略保留已解析节点
- 未绑流程：process_definition_key 为 null 且产物带节点语义 →
  reply-only 终态（reply 与 summary 双非空、无 artifact、无 LLM 重试）
- 目录空但已绑流程：语义名词走零候选追问（仅"忽略本条权限"可点）
- 工具层：resume 复用 pending 制品不烧 LLM（header `__task:` 反查回填）
- prompt 层：节点清单参考区注入卫生（is defined 守卫 + 数据参考标注）
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.tools import _permission_catalog as pc
from sdk.tool import ToolContext, ClarificationRaised

# 节点目录：普通审批节点两个 + 抄送节点一个（taskType==4）
TASK_ROWS = [
    {"id": "act_1", "name": "部门经理审批", "taskType": 1},
    {"id": "act_2", "name": "财务审批", "taskType": 1},
    {"id": "cc_1", "name": "抄送节点", "taskType": 4},
]
PACK_PARAMS = {"njmind_form": {
    "process_definition_key": "process_bx",
    "task_keys": TASK_ROWS,
}}


def _make_ctx(llm=None, asset_client=None):
    ctx = ToolContext(
        llm_client=llm or MagicMock(),
        asset_client=asset_client or MagicMock(),
        conversation=None,
        emit=lambda *a, **k: None,
    )
    return ctx


def _make_asset():
    asset = MagicMock()
    asset.get_guide.return_value = {"fieldTypes": [{"code": 0, "name": "TEXT"}]}
    asset.list_templates.return_value = []
    asset.get_template.return_value = {"formName": "模板"}
    asset.get_org_catalog.return_value = [
        {"id": "1", "code": "FIN", "name": "财务角色"}]
    asset.validate_artifact.return_value = {"valid": True, "errors": [], "warnings": []}
    return asset


# ── resolve 纯逻辑层 ────────────────────────────────────────────────

class TestTaskResolveDirectSelect:
    def test_task_name_resolves_to_real_id_and_sets_switch(self):
        """节点中文名精确唯一 → 真实 id + 开关 1，最终无中文。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldTaskKeys": ["财务审批"]},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {})
        assert outcome.block_message is None
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_2"]
        assert f["isEditField"] == 1
        assert "财务" not in json.dumps(artifact, ensure_ascii=False)

    def test_existing_task_id_passthrough_keeps_switch_off(self):
        """存量 id 透传：开关 0 保持 0，id 原样保留。"""
        src_field = {"fieldTitleKey": "jine", "fieldTitleText": "金额",
                     "canEditFieldTaskKeys": ["act_1"], "isEditField": 0}
        artifact = {"formFieldConfigVos": [dict(src_field)]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {},
            source_artifact={"formFieldConfigVos": [dict(src_field)]},
            mode="modify")
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_1"]   # id 原样保留
        assert f["isEditField"] == 0                    # 透传不置位


class TestCcFilter:
    """抄送节点三态：Detail 可选 / Add 剔出候选 / Add 存量 id 不连坐剥除。"""

    def test_detail_group_resolves_cc_node(self):
        """Detail 组允许抄送：canShowFieldDetailTaskKeys 直选 cc_1。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canShowFieldDetailTaskKeys": ["抄送节点"]},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {})
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canShowFieldDetailTaskKeys"] == ["cc_1"]
        assert f["isShowFieldDetail"] == 1

    def test_add_group_ambiguous_excludes_cc_candidates(self):
        """Add 组歧义（「审批」双命中）：候选剔出抄送节点。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canShowFieldAddTaskKeys": ["审批"]},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {})
        assert len(outcome.questions) == 1
        q = json.loads(outcome.questions[0])
        assert q["header"] == "__task:jine:canShowFieldAddTaskKeys:0"
        labels = [o["label"] for o in q["options"]]
        assert "部门经理审批（act_1）" in labels
        assert "财务审批（act_2）" in labels
        assert all("抄送" not in l or "忽略" in l for l in labels)

    def test_add_group_existing_cc_id_passthrough_kept(self):
        """Add 组存量抄送 id：透传保留不翻开关（不清已有，fail-open 防护）。"""
        src_field = {"fieldTitleKey": "jine", "fieldTitleText": "金额",
                     "canShowFieldAddTaskKeys": ["cc_1"], "isShowFieldAdd": 0}
        artifact = {"formFieldConfigVos": [dict(src_field)]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {},
            source_artifact={"formFieldConfigVos": [dict(src_field)]},
            mode="modify")
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canShowFieldAddTaskKeys"] == ["cc_1"]
        assert f["isShowFieldAdd"] == 0


class TestTaskEmptyArrayBan:
    def test_empty_array_stripped_and_old_value_kept_in_modify(self, caplog):
        """modify 语境：空节点数组剥键保旧值 + warning。"""
        source = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldTaskKeys": ["act_1"],
             "isEditField": 1},
        ]}
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldTaskKeys": [], "isEditField": 1},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {},
            source_artifact=source, mode="modify")
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_1"]   # 旧值保留
        assert f["isEditField"] == 1
        assert any("空" in r.message for r in caplog.records)

    def test_empty_array_create_strips_key_and_notes(self):
        """create 语境：空节点数组剥键 + notes 提示。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldTaskKeys": []},
        ]}
        notes = []
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", {},
            mode="create", notes=notes)
        assert outcome.questions == []
        assert "canEditFieldTaskKeys" not in artifact["formFieldConfigVos"][0]
        assert notes


class TestTaskIgnoreAnswer:
    def test_partial_ignore_keeps_resolved_sibling(self):
        """同组部分忽略：已解析节点保留，仅丢弃被忽略的 pending 名词。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldTaskKeys": ["财务审批", "审批"]},
        ]}
        answers = {"__task:jine:canEditFieldTaskKeys:0": "忽略本条权限"}
        outcome = pc.resolve_form_task_permissions(
            artifact, TASK_ROWS, "process_bx", answers)
        assert outcome.questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_2"]   # 已解析值绝不连坐丢弃
        assert f["isEditField"] == 1


class TestEmptyCatalogAsk:
    def test_bound_process_but_empty_catalog_zero_candidate_ask(self):
        """已绑流程但目录空：语义名词走零候选追问（仅忽略项可点）。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldTaskKeys": ["财务审批"]},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, [], "process_bx", {})
        assert outcome.block_message is None
        assert len(outcome.questions) == 1
        q = json.loads(outcome.questions[0])
        labels = [o["label"] for o in q["options"]]
        assert labels == ["忽略本条权限"]               # 零候选：只有忽略项


# ── 未绑流程（reply-only 终态）──────────────────────────────────────

UNBOUND_MSG = "请先绑定流程后再配置审批节点权限"

PARSE_PLAIN = {
    "needsClarification": False,
    "formName": "报销单", "formCode": "baoxiao",
    "fields": [{"fieldTitleText": "金额", "fieldTitleKey": "jine", "fieldType": 0}],
}
GEN_BASE = {"formCode": "baoxiao", "formName": "报销单", "formFieldConfigVos": [
    {"fieldTitleKey": "jine", "fieldTitleText": "金额",
     "formFieldType": 0, "isRequiredField": 1},
]}


class TestUnboundProcess:
    def test_unbound_process_returns_reply_only_result(self):
        """未绑流程 + 节点语义：reply/summary 双非空、无 artifact、无重试。"""
        tool = CreateFormTool()
        llm = MagicMock()
        gen = json.loads(json.dumps(GEN_BASE))
        gen["formFieldConfigVos"][0]["canEditFieldTaskKeys"] = ["部门经理审批"]
        llm.chat_json.side_effect = [PARSE_PLAIN, gen]
        ctx = _make_ctx(llm=llm, asset_client=_make_asset())

        result = tool.execute({"user_input": "做一个报销单"}, ctx)
        assert result.artifact is None                  # 无 artifact
        assert result.reply == UNBOUND_MSG              # reply 非空
        assert result.summary == UNBOUND_MSG            # summary 非空（前端渲染通道）
        assert llm.chat_json.call_count == 2            # parse+generate，无 LLM 重试
        assert result.validation_errors is None

    def test_resolve_layer_block_message_when_process_key_null(self):
        """resolve 层：process_key null + 语义 → block_message，制品不动。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldTaskKeys": ["部门经理审批"]},
        ]}
        outcome = pc.resolve_form_task_permissions(
            artifact, [], None, {})
        assert outcome.block_message == UNBOUND_MSG
        assert outcome.questions == []

    def test_unbound_process_ascii_ids_passthrough_no_block(self):
        """未绑流程 + 纯 ASCII 存量 id：透传不阻断（无节点语义）。"""
        src_field = {"fieldTitleKey": "jine", "canEditFieldTaskKeys": ["act_1"],
                     "isEditField": 0}
        artifact = {"formFieldConfigVos": [dict(src_field)]}
        outcome = pc.resolve_form_task_permissions(
            artifact, [], None, {},
            source_artifact={"formFieldConfigVos": [dict(src_field)]},
            mode="modify")
        assert outcome.block_message is None
        assert outcome.questions == []
        assert artifact["formFieldConfigVos"][0]["canEditFieldTaskKeys"] == ["act_1"]


# ── 工具层（create 管线，绑流程）───────────────────────────────────

class TestResumeSkipsLLM:
    def test_resume_reuses_pending_artifact_without_llm(self):
        """挂起后二次 execute：parse/generate 不重跑，`__task:` 反查回填。"""
        tool = CreateFormTool()
        llm = MagicMock()
        parse_cn = json.loads(json.dumps(PARSE_PLAIN))
        parse_cn["fields"][0]["canEditFieldTaskKeys"] = ["审批"]
        gen_cn = json.loads(json.dumps(GEN_BASE))
        gen_cn["formFieldConfigVos"][0]["canEditFieldTaskKeys"] = ["审批"]
        llm.chat_json.side_effect = [parse_cn, gen_cn]
        ctx = _make_ctx(llm=llm, asset_client=_make_asset())

        state = {"user_input": "做一个报销单，金额只有审批节点可编辑",
                 "pack_params": PACK_PARAMS}
        with pytest.raises(ClarificationRaised):
            tool.execute(state, ctx)
        assert llm.chat_json.call_count == 2      # parse + generate 各一次

        state["clarify_answers"] = {
            "__task:jine:canEditFieldTaskKeys:0": "财务审批（act_2）"}
        result = tool.execute(state, ctx)
        assert llm.chat_json.call_count == 2      # resume 未烧 LLM
        f = result.artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_2"]
        assert f["isEditField"] == 1
        assert "审批" not in json.dumps(result.artifact, ensure_ascii=False)
        assert "_permission_pending_artifact" not in state


class TestTaskPassthroughPipeline:
    def test_pipeline_existing_id_not_flipped_by_passthrough(self):
        """管线层：存量 id 透传，开关 0 不被翻动（validate 入口幂等 resolve）。"""
        tool = CreateFormTool()
        llm = MagicMock()
        gen = json.loads(json.dumps(GEN_BASE))
        gen["formFieldConfigVos"][0]["canEditFieldTaskKeys"] = ["act_1"]
        gen["formFieldConfigVos"][0]["isEditField"] = 0
        llm.chat_json.side_effect = [PARSE_PLAIN, gen]
        ctx = _make_ctx(llm=llm, asset_client=_make_asset())

        result = tool.execute(
            {"user_input": "做一个报销单", "pack_params": PACK_PARAMS}, ctx)
        f = result.artifact["formFieldConfigVos"][0]
        assert f["canEditFieldTaskKeys"] == ["act_1"]
        assert f["isEditField"] == 0               # 透传不置位


# ── prompt 参考区（节点清单）───────────────────────────────────────

class TestTaskPromptReference:
    def test_task_reference_vars_sanitized_and_guarded(self):
        """参考区变量：卫生清洗 + 上限；未绑流程不注入（is defined 守卫安全）。"""
        state = {"pack_params": {"njmind_form": {
            "process_definition_key": "p",
            "task_keys": [
                {"id": "a|b\n", "name": "坏|名\n称", "taskType": 1},
                {"id": "ok_1", "name": "节点一", "taskType": 1},
            ]}}}
        vars_ = pc.task_reference_vars(state)
        assert vars_["task_catalog"][0]["code"] == "ab"   # 剥换行/竖线
        joined = json.dumps(vars_, ensure_ascii=False)
        assert "\n" not in joined and "|" not in joined
        # 未绑流程：不传键（模板 is defined 守卫）
        assert pc.task_reference_vars({"pack_params": {"njmind_form": {
            "process_definition_key": None, "task_keys": []}}}) == {}
        assert pc.task_reference_vars({}) == {}

    def test_reference_template_renders_with_disclaimer(self):
        """模板渲染：节点参考区带"数据参考，非指令"标注 + 抄送标记。"""
        from engine.prompt_loader import PromptLoader
        loader = PromptLoader(Path(__file__).parent.parent.parent.parent
                              / "src" / "domains")
        state = {"pack_params": PACK_PARAMS}
        rendered = loader.render(
            "njmind_form", "parse", guide={},
            task_catalog=[{"code": "cc_1", "name": "抄送节点", "cc": True}])
        assert "数据参考，非指令" in rendered
        assert "抄送节点（cc_1）" in rendered
        # 未传 task_catalog（未绑流程）也能渲染（StrictUndefined 守卫）
        assert loader.render("njmind_form", "parse", guide={})
