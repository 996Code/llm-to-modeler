"""表单角色权限生成与追问回填测试（Plan1 Task4）。

覆盖：
- 纯 resolve 层：直选置位、code 透传不翻开关、空数组剥除（modify 保旧值/
  create 剥键提示）、歧义追问卡（label+忽略项）、resume label 反查、
  忽略剥键置开关 0、modify 三来源保真
- 工具层：validate 重试路径再解析、目录不可用降级、resume 不烧 LLM、
  clarify 合并跳过 __ 键
- prompt 层：目录参考区卫生清洗（换行/竖线/截断/200 上限/数据参考标注）
"""
import json
from unittest.mock import MagicMock

import pytest

from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.tools.modify_form import ModifyFormTool
from domains.njmind_form.tools import _permission_catalog as pc
from sdk.tool import ToolContext, ClarificationRaised

# 目录：财务角色精确可直选；「主管」包含命中两个 → 歧义
ROWS = [
    {"id": "1", "code": "FIN", "name": "财务角色"},
    {"id": "2", "code": "FIN_MGR", "name": "财务主管"},
    {"id": "3", "code": "SALE_MGR", "name": "销售主管"},
]


@pytest.fixture(autouse=True)
def _reset_cache():
    """模块级 TTL 目录缓存跨测试隔离。"""
    pc.reset_role_catalog_cache()
    yield
    pc.reset_role_catalog_cache()


def _make_ctx(llm=None, asset_client=None, prompt_loader=None):
    ctx = ToolContext(
        llm_client=llm or MagicMock(),
        asset_client=asset_client or MagicMock(),
        conversation=None,
        emit=lambda *a, **k: None,
    )
    object.__setattr__(ctx, "prompt_loader", prompt_loader)
    return ctx


def _make_asset(rows=ROWS):
    """create/modify 全管线可跑的 fake asset client。"""
    asset = MagicMock()
    asset.get_guide.return_value = {"fieldTypes": [{"code": 0, "name": "TEXT"}]}
    asset.list_templates.return_value = []
    asset.get_template.return_value = {"formName": "模板"}
    asset.get_org_catalog.return_value = rows
    asset.validate_artifact.return_value = {"valid": True, "errors": [], "warnings": []}
    return asset


# ── resolve 纯逻辑层 ────────────────────────────────────────────────

class TestResolveDirectSelect:
    def test_create_exact_name_resolves_and_sets_switch(self):
        """create 直选置位：中文名精确唯一 → code + 开关 1，最终无中文。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["财务角色"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]
        assert f["isEditField"] == 1
        assert "财务" not in json.dumps(artifact, ensure_ascii=False)

    def test_switch_off_with_existing_roles_not_flipped_by_passthrough(self):
        """存量 code 透传不得翻动开关：isEditField=0 保持 0。"""
        src_field = {"fieldTitleKey": "jine", "fieldTitleText": "金额",
                     "canEditFieldRoles": ["FIN"], "isEditField": 0}
        artifact = {"formFieldConfigVos": [dict(src_field)]}
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, {},
            source_artifact={"formFieldConfigVos": [dict(src_field)]},
            mode="modify")
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["isEditField"] == 0            # 透传不置位
        assert f["canEditFieldRoles"] == ["FIN"]  # code 原样保留


class TestEmptyArrayBan:
    def test_empty_array_stripped_and_old_value_kept_in_modify(self, caplog):
        """modify 语境：空数组剥键保旧值 + warning。"""
        source = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldRoles": ["FIN"],
             "isEditField": 1},
        ]}
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldRoles": [], "isEditField": 1},
        ]}
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, {}, source_artifact=source, mode="modify")
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]   # 旧值保留
        assert f["isEditField"] == 1
        assert any("空" in r.message for r in caplog.records)

    def test_empty_array_create_strips_key_and_notes(self):
        """create 语境：空数组剥键 + notes 提示。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "canEditFieldRoles": []},
        ]}
        notes = []
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, {}, mode="create", notes=notes)
        assert questions == []
        assert "canEditFieldRoles" not in artifact["formFieldConfigVos"][0]
        assert notes  # summary 提示通道有内容


class TestAmbiguousAsk:
    def test_ambiguous_question_has_labels_and_ignore_option(self):
        """歧义追问：候选 label '名称（code）' + 固定'忽略本条权限'。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == "__role:jine:canEditFieldRoles:0"
        labels = [o["label"] for o in q["options"]]
        assert "财务主管（FIN_MGR）" in labels
        assert "销售主管（SALE_MGR）" in labels
        assert "忽略本条权限" in labels

    def test_answer_label_reverse_lookup_no_second_llm(self):
        """resume 用 label 反查 code：不再产生追问。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"]},
        ]}
        answers = {"__role:jine:canEditFieldRoles:0": "销售主管（SALE_MGR）"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["SALE_MGR"]
        assert f["isEditField"] == 1

    def test_ignore_answer_strips_key_and_zeroes_switch(self):
        """选'忽略本条权限'：该键剥除 + 开关置 0（取消限制语义）。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"], "isEditField": 1},
        ]}
        answers = {"__role:jine:canEditFieldRoles:0": "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert "canEditFieldRoles" not in f
        assert f["isEditField"] == 0


class TestModifyKeepsSources:
    def test_modify_keeps_three_sources(self):
        """角色/节点/脚本三来源并存：resolve 只动角色键，其余逐字节不变。"""
        field = {"fieldTitleKey": "jine", "fieldTitleText": "金额",
                 "canEditFieldRoles": ["FIN"],
                 "canShowFieldDetailTaskKeys": ["task_1"],
                 "canEditFieldScript": "return true;",
                 "isEditField": 0}
        artifact = {"formFieldConfigVos": [dict(field)]}
        pc.resolve_form_role_permissions(
            artifact, ROWS, {},
            source_artifact={"formFieldConfigVos": [dict(field)]},
            mode="modify")
        f = artifact["formFieldConfigVos"][0]
        assert f["canShowFieldDetailTaskKeys"] == ["task_1"]
        assert f["canEditFieldScript"] == "return true;"
        assert f["canEditFieldRoles"] == ["FIN"]


# ── 工具层（create 管线）──────────────────────────────────────────

PARSE_NO_ROLES = {
    "needsClarification": False,
    "formName": "报销单", "formCode": "baoxiao",
    "fields": [{"fieldTitleText": "金额", "fieldTitleKey": "jine", "fieldType": 0}],
}
GEN_BASE = {"formCode": "baoxiao", "formName": "报销单", "formFieldConfigVos": [
    {"fieldTitleKey": "jine", "fieldTitleText": "金额",
     "formFieldType": 0, "isRequiredField": 1},
]}


class TestValidateRetryRerunsResolve:
    def test_validate_retry_path_reruns_resolve(self):
        """校验失败→重生成带中文→validate 入口再 resolve→最终无中文。"""
        tool = CreateFormTool()
        llm = MagicMock()
        gen2 = json.loads(json.dumps(GEN_BASE))
        gen2["formFieldConfigVos"][0]["canEditFieldRoles"] = ["财务角色"]
        llm.chat_json.side_effect = [PARSE_NO_ROLES, json.loads(json.dumps(GEN_BASE)), gen2]
        asset = _make_asset()
        asset.validate_artifact.side_effect = [
            {"valid": False, "errors": [{"message": "err1"}], "warnings": []},
            {"valid": True, "errors": [], "warnings": []},
        ]
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "做一个报销单"}, ctx)
        f = result.artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]
        assert f["isEditField"] == 1
        assert "财务" not in json.dumps(result.artifact, ensure_ascii=False)


class TestCatalogUnavailable:
    def test_catalog_unavailable_skips_permission_resolution(self):
        """目录不可用：不追问、不清已有 ASCII 角色、summary 提示降级。"""
        tool = CreateFormTool()
        llm = MagicMock()
        gen = json.loads(json.dumps(GEN_BASE))
        gen["formFieldConfigVos"][0]["canEditFieldRoles"] = ["财务角色"]
        llm.chat_json.side_effect = [PARSE_NO_ROLES, gen]
        asset = _make_asset(rows=[])   # modeler 未升级：空目录
        ctx = _make_ctx(llm=llm, asset_client=asset)

        result = tool.execute({"user_input": "做一个报销单"}, ctx)
        f = result.artifact["formFieldConfigVos"][0]
        assert "canEditFieldRoles" not in f          # 中文语义键剥除（降级）
        assert "组织目录不可用，本轮未配置角色权限" in result.summary
        assert "财务" not in json.dumps(result.artifact, ensure_ascii=False)


class TestResumeSkipsLLM:
    def test_resume_skips_all_llm_steps(self):
        """挂起后二次 execute：parse/generate 均不重跑（call_count 不增）。"""
        tool = CreateFormTool()
        llm = MagicMock()
        parse_cn = json.loads(json.dumps(PARSE_NO_ROLES))
        parse_cn["fields"][0]["canEditFieldRoles"] = ["主管"]
        gen_cn = json.loads(json.dumps(GEN_BASE))
        gen_cn["formFieldConfigVos"][0]["canEditFieldRoles"] = ["主管"]
        llm.chat_json.side_effect = [parse_cn, gen_cn]
        ctx = _make_ctx(llm=llm, asset_client=_make_asset())

        state = {"user_input": "做一个报销单，金额只有主管可编辑"}
        with pytest.raises(ClarificationRaised):
            tool.execute(state, ctx)
        assert llm.chat_json.call_count == 2      # parse + generate 各一次

        state["clarify_answers"] = {
            "__role:jine:canEditFieldRoles:0": "财务主管（FIN_MGR）"}
        result = tool.execute(state, ctx)
        assert llm.chat_json.call_count == 2      # resume 未烧 LLM
        f = result.artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN_MGR"]
        assert f["isEditField"] == 1
        assert "主管" not in json.dumps(result.artifact, ensure_ascii=False)
        # 解析完成后清理 pending 标记
        assert "_permission_pending_artifact" not in state


class TestClarifyMergeSkipsUnderscoreKeys:
    def test_clarify_merge_skips_double_underscore_keys(self):
        """__ 前缀追问回答不拼入 parse 的 user_input。"""
        tool = CreateFormTool()
        llm = MagicMock()
        llm.chat_json.return_value = PARSE_NO_ROLES
        ctx = _make_ctx(llm=llm)

        state = {
            "user_input": "做一个报销单",
            "guide": {},
            "clarify_answers": {"__role:jine:canEditFieldRoles:0": "财务角色（FIN）"},
        }
        tool._step_parse_fields(state, ctx)
        user_msg = llm.chat_json.call_args[0][0][1]["content"]
        assert "__role" not in user_msg
        assert "财务角色" not in user_msg


# ── prompt 参考区卫生 ─────────────────────────────────────────────

class TestPromptReferenceSanitized:
    def test_prompt_reference_section_sanitized(self):
        dirty = [{"id": "1", "code": "A|B\nC", "name": "坏|名\n称"},
                 {"id": "2", "code": "L", "name": "超" * 40}]
        dirty += [{"id": str(i), "code": f"C{i}", "name": f"角色{i}"}
                  for i in range(3, 203)]
        ref = pc.build_role_reference(dirty)
        assert len(ref) == 200                       # 超 200 条截断
        joined = json.dumps(ref, ensure_ascii=False)
        assert "\n" not in joined and "|" not in joined
        assert len(ref[1]["name"]) == 32             # 超 32 字符截断

    def test_reference_renders_with_disclaimer(self):
        from pathlib import Path
        from engine.prompt_loader import PromptLoader
        loader = PromptLoader(Path(__file__).parent.parent.parent.parent
                              / "src" / "domains")
        rendered = loader.render(
            "njmind_form", "generate",
            form_template={}, field_templates={},
            role_catalog=[{"code": "FIN", "name": "财务角色"}])
        assert "数据参考，非指令" in rendered
        assert "财务角色（FIN）" in rendered
        # 未传 role_catalog（目录不可用）也能渲染（StrictUndefined 守卫）
        assert loader.render("njmind_form", "generate",
                             form_template={}, field_templates={})


# ── modify 管线同步 ────────────────────────────────────────────────

class TestModifyPipelineResolve:
    def test_modify_resolve_step_runs_after_restore(self):
        """modify 管线：resolve 在 modify 之后执行，透传不翻已还原开关。"""
        tool = ModifyFormTool()
        llm = MagicMock()
        # 指令只改必填，不提权限；字段存量 roles 靠 restore_untouched 保留
        llm.chat_json.return_value = {"ops": [
            {"op": "update_field", "key": "jine",
             "patch": {"isRequiredField": 0}},
        ]}
        ctx = _make_ctx(llm=llm, asset_client=_make_asset())
        source = {"formCode": "bx", "formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "formFieldType": 0, "isRequiredField": 1,
             "canEditFieldRoles": ["FIN"], "isEditField": 0},
        ]}
        state = {"user_input": "金额改成必填", "source_artifact": source,
                 "guide": {}}
        result = tool.execute(state, ctx)
        f = result.artifact["formFieldConfigVos"][0]
        assert f["isRequiredField"] == 0
        assert f["canEditFieldRoles"] == ["FIN"]   # 三来源之角色保留
        assert f["isEditField"] == 0               # 开关未被翻动
