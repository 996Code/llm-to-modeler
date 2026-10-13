"""表单角色权限生成与追问回填测试（Plan1 Task4）。

覆盖：
- 纯 resolve 层：直选置位、code 透传不翻开关、空数组剥除（modify 保旧值/
  create 剥键提示）、歧义追问卡（label+忽略项）、resume label 反查、
  忽略仅丢弃单条名词（修复轮语义锁）、modify 三来源保真
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


def _tok(semantic: str) -> str:
    """角色追问 header 的名词身份 token（与实现同源）。"""
    return pc._semantic_token(semantic)


def _role_header(key: str, prop: str, semantic: str) -> str:
    """组装角色追问 header（与实现同格式）。"""
    return f"__role:{key}:{prop}:{_tok(semantic)}"


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
        """歧义追问：候选 label '名称（code）' + 固定'忽略本条权限'；
        header 末段是名词身份 token（跨轮稳定，非列表序号）。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == _role_header("jine", "canEditFieldRoles", "主管")
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
        answers = {_role_header("jine", "canEditFieldRoles", "主管"):
                   "销售主管（SALE_MGR）"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["SALE_MGR"]
        assert f["isEditField"] == 1

    def test_create_ignore_emptied_group_strips_key_only(self):
        """create 选'忽略本条权限'后组内空：剥键即可，开关回未涉及态（不动）。

        isEditField=1 是模板默认（可编辑），忽略≠禁用字段——开关不因忽略置 0。
        """
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"], "isEditField": 1},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "主管"):
                   "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert "canEditFieldRoles" not in f
        assert f["isEditField"] == 1            # 开关不动（默认态即未涉及态）


class TestIgnoreAnswerSemantics:
    """「忽略本条权限」的"条"=单个 pending 名词（评审修复轮锁定语义）。

    - 同组部分忽略：已解析值绝不连坐丢弃，开关按既有规则置 1
    - 忽略后组内空 → 回"本次指令未涉及该权限"状态：
      create（新字段）剥键即可；modify 存量开关与旧 roles 一律不动
      （忽略=本次不写，不是清空旧权限——与空数组剥键保旧值分支同构）
    """

    def test_partial_ignore_keeps_resolved_sibling(self):
        """"财务角色"直选命中 + "主管"歧义被忽略 → 财务写入，主管丢弃。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["财务角色", "主管"]},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "主管"):
                   "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]   # 已解析值保留，绝不全丢
        assert f["isEditField"] == 1               # 组内仍有已解析值 → 置 1

    def test_modify_full_ignore_keeps_old_roles_and_switch(self):
        """modify 全忽略：忽略=本次不写——旧 roles 与存量开关原样保留。"""
        source = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["FIN"], "isEditField": 1},
        ]}
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"], "isEditField": 1},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "主管"): "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, answers, source_artifact=source, mode="modify")
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]   # 旧 roles 不动
        assert f["isEditField"] == 1               # 存量开关不动

    def test_modify_full_ignore_restores_old_switch_value(self):
        """modify 全忽略（存量开关 0）：本轮产物开关被翻成 1 也要还原回 0。"""
        source = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["FIN"], "isEditField": 0},
        ]}
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"], "isEditField": 1},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "主管"): "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, answers, source_artifact=source, mode="modify")
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN"]
        assert f["isEditField"] == 0               # 回存量开关值（非清零）

    def test_modify_ignore_new_restriction_writes_nothing(self):
        """modify 新增限制被忽略且源无旧值：剥键即"本次不写"。"""
        source = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额"},
        ]}
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"], "isEditField": 1},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "主管"): "忽略本条权限"}
        questions = pc.resolve_form_role_permissions(
            artifact, ROWS, answers, source_artifact=source, mode="modify")
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert "canEditFieldRoles" not in f        # 本次不写（源本就无此键）


class TestMultiPendingStableHeaders:
    """终审 Important-1：多 pending 同组挂起 + header 身份化防答案错配。

    旧 header 末段是 pending 列表序号：部分作答/目录漂移/制品重生成都会让
    同一序号指向不同名词，旧答案被静默写到别的名词（零命中候选=全目录，
    任何合法 label 都反查得中）。新 header 末段是名词身份 token：错位答案
    的 key 必不撞键 → 重新追问，绝不静默写错 code。
    """

    def test_two_pending_nouns_both_answered_no_mismatch(self):
        """同字段同属性两个歧义名词（主管/归档）→ 分别作答 → 两个正确
        code 按序写入、开关置 1、无追问无错配。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管", "归档"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert len(questions) == 2
        headers = {json.loads(q)["header"] for q in questions}
        assert headers == {
            _role_header("jine", "canEditFieldRoles", "主管"),
            _role_header("jine", "canEditFieldRoles", "归档"),
        }
        answers = {
            _role_header("jine", "canEditFieldRoles", "主管"):
                "销售主管（SALE_MGR）",
            _role_header("jine", "canEditFieldRoles", "归档"):
                "财务角色（FIN）",
        }
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管", "归档"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["SALE_MGR", "FIN"]  # 各归各位
        assert f["isEditField"] == 1

    def test_partial_answer_remaining_header_identity_stable(self):
        """只答归档一轮后，主管的追问 header 仍是自己的 token（与首轮
        一致），不因列表压缩换序号；下一轮按 token 作答正确落定。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管", "归档"]},
        ]}
        # 第一轮：只答归档
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {
            _role_header("jine", "canEditFieldRoles", "归档"):
                "财务角色（FIN）"})
        assert len(questions) == 1
        assert json.loads(questions[0])["header"] == \
            _role_header("jine", "canEditFieldRoles", "主管")
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["FIN", "主管"]  # 已定 code 在前
        # 第二轮（resume 重放同一制品）：主管按自身 token 作答 → 落定
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {
            _role_header("jine", "canEditFieldRoles", "主管"):
                "销售主管（SALE_MGR）"})
        assert questions == []
        assert artifact["formFieldConfigVos"][0]["canEditFieldRoles"] == \
            ["FIN", "SALE_MGR"]

    def test_duplicate_semantics_single_question(self):
        """同语义重复名词只发一张卡；一次作答对同语义全部生效（写码去重）。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管", "主管"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert len(questions) == 1
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {
            _role_header("jine", "canEditFieldRoles", "主管"):
                "销售主管（SALE_MGR）"})
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["SALE_MGR"]  # 去重写入
        assert f["isEditField"] == 1


class TestAnswerMismatchProtection:
    """答案错配防护：伪造/过期 header 的 answers 不得静默写入错误 code。"""

    def test_catalog_drift_compacts_pending_but_token_answers_stay_bound(self):
        """目录漂移压缩 pending（旧缺陷触发器）：首轮目录下 归档/主管 两问
        都作答；次轮目录新增"归档"直选 → pending 压缩成只剩主管。旧序号
        header 下 :0 位的归档答案会打到主管头上（全目录反查必中）静默写
        FIN、真答案 :1 被丢弃；身份 token 下答案各归各位，零错配。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["归档", "主管"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, {})
        assert len(questions) == 2                     # 首轮：两问
        answers = {
            _role_header("jine", "canEditFieldRoles", "归档"):
                "财务角色（FIN）",
            _role_header("jine", "canEditFieldRoles", "主管"):
                "销售主管（SALE_MGR）",
        }
        # 次轮目录漂移：归档 可直选（resolve_names 直选归 DA，不经 answers）
        drifted = ROWS + [{"id": "4", "code": "DA", "name": "归档"}]
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["归档", "主管"]},
        ]}
        questions = pc.resolve_form_role_permissions(artifact, drifted, answers)
        assert questions == []
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["DA", "SALE_MGR"]  # 各归各位
        assert "FIN" not in f["canEditFieldRoles"]           # 归档答案未串到主管

    def test_legacy_positional_key_safely_ignored_and_reasked(self):
        """在途旧格式（序号版）header：修复部署前发出的追问卡，用户按旧
        key 作答 → 新代码不消费（防错位）→ 该名词重新追问一次（安全忽略
        的取舍：多问一次，绝不静默写错）。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"]},
        ]}
        answers = {"__role:jine:canEditFieldRoles:0": "销售主管（SALE_MGR）"}
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert len(questions) == 1                       # 未消费 → 重新追问
        assert json.loads(questions[0])["header"] == \
            _role_header("jine", "canEditFieldRoles", "主管")
        assert artifact["formFieldConfigVos"][0]["canEditFieldRoles"] == ["主管"]

    def test_foreign_token_key_ignored(self):
        """answers 携带"别的名词的 token"作 key（过期/串答）：当前名词
        不消费该键 → 重新追问，不落任何 code。"""
        artifact = {"formFieldConfigVos": [
            {"fieldTitleKey": "jine", "fieldTitleText": "金额",
             "canEditFieldRoles": ["主管"]},
        ]}
        answers = {_role_header("jine", "canEditFieldRoles", "归档"):
                   "财务主管（FIN_MGR）"}   # 归档的 key，答的却是主管的 label
        questions = pc.resolve_form_role_permissions(artifact, ROWS, answers)
        assert len(questions) == 1
        f = artifact["formFieldConfigVos"][0]
        assert f["canEditFieldRoles"] == ["主管"]   # 语义原样保留
        assert f.get("isEditField") is None          # 无 code 写入即不置开关


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
            _role_header("jine", "canEditFieldRoles", "主管"): "财务主管（FIN_MGR）"}
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
            "clarify_answers": {_role_header("jine", "canEditFieldRoles", "主管"): "财务角色（FIN）"},
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
