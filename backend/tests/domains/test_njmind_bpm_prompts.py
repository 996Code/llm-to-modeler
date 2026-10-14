"""njmind_bpm prompts 模板渲染测试（parse/generate/_sections）。

契约对应 Plan2 Task 2：审批人与角色分支的 LLM 中间态契约进 prompt——
- 审批人中间态用中文名词（checkedRoleIds/checkedDepIds/checkedUserIds 数组），
  禁编 id/code（id 映射由解析阶段做，LLM 只产语义名词）
- roleRules 仅 formFieldKey=loginUserRole + logicType 30/40（包含/不包含）
- 未提及的身份维度省键，禁止输出空数组占位
- rules（表单字段值条件）与 roleRules（系统字段角色条件）在同一边上互斥
- formFieldKey 最终形态 Array<{formFieldType,formFieldKey}>，取值来自表单字段目录

对齐 test_njmind_list_prompts.py 的 loader 装配方式（packs_root 指向 src/domains）。
"""
import pytest
from pathlib import Path

from sdk.prompt_loader import PromptLoader


@pytest.fixture
def loader():
    """PromptLoader 指向 domains 根（pack.py 同款装配）。"""
    return PromptLoader(packs_root=Path(__file__).resolve().parents[2] / "src" / "domains")


GUIDE = {
    "nodeTypes": [
        {"code": "USER_TASK", "description": "人工审批", "rule": ""},
        {"code": "CC_TASK", "description": "抄送", "rule": ""},
    ],
    "enumMappings": [
        {"group": "branchLogic", "name": "AND", "code": 10, "description": "且",
         "keywords": []},
        {"group": "approveMethod", "name": "OR_ANY", "code": 1, "description": "或签",
         "keywords": []},
    ],
}

FORM_CATALOG = [
    {"key": "leave_days", "title": "请假天数", "type": 2, "typeName": "数字",
     "options": []},
    {"key": "approver", "title": "审批人", "type": 7, "typeName": "人员",
     "options": []},
]

PROCESS_TEMPLATE = {
    "nodes": [{"key": "start", "type": "START_EVENT", "name": "开始"}],
    "edges": [{"key": "flow_1", "from": "start", "to": "end"}],
}

RULE_TEMPLATES = {
    "bpm_approve_default": {"taskId": "t1", "approveOptRange": 80,
                            "approveOptRangeSettings": {}},
    "bpm_branch_field_value": {"sequenceFlowId": "f1", "logic": 10,
                               "rules": []},
}


class TestParsePromptApproverIntent:
    """parse.j2：审批人/分支意图的中间态契约。"""

    def test_parse_审批人意图含中文语义字段说明(self, loader):
        out = loader.render("njmind_bpm", "parse", guide=GUIDE)
        # 审批人描述字段仍走语义名词（角色/部门/人员），id/code 由后续阶段映射
        for token in ("approver", "nodeIntents", "branchIntents"):
            assert token in out, f"parse.j2 缺契约键 {token}"

    def test_parse_角色分支意图引导loginUserRole(self, loader):
        """分支意图需能表达"当前用户角色"语义（loginUserRole 系统字段）。"""
        out = loader.render("njmind_bpm", "parse", guide=GUIDE)
        assert "loginUserRole" in out, "parse.j2 缺 loginUserRole 系统字段说明"
        assert "当前用户角色" in out

    def test_parse_禁编id_code禁令在场(self, loader):
        out = loader.render("njmind_bpm", "parse", guide=GUIDE)
        assert "id" in out and "code" in out  # 禁令文案
        # 禁编令必须明确指向审批人/角色（不只是 BPMN id 铁律的既有表述）
        assert "审批人" in out


class TestGeneratePromptApproverContract:
    """generate.j2：审批人中间态契约骨架与禁令。"""

    def test_generate_审批人中间态骨架含中文名词数组(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        for key in ("checkedRoleIds", "checkedDepIds", "checkedUserIds"):
            assert key in out, f"generate.j2 缺审批人中间态键 {key}"
        # 中文语义名词示例（LLM 产出中文名，解析阶段映射 id）
        assert "财务角色" in out or "角色名" in out

    def test_generate_审批人禁编id_code(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        assert "禁止" in out
        # 禁编令点名 id/code（审批人名词必须是中文名/部门名/姓名，不是 id）
        assert "id" in out and "code" in out

    def test_generate_未提及维度省键禁空数组(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        assert "省略" in out or "不要输出空数组" in out, \
            "generate.j2 缺「未提及维度省键」禁令"

    def test_generate_角色分支roleRules仅loginUserRole且logicType30_40(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        assert "roleRules" in out, "generate.j2 缺 roleRules 说明"
        assert "loginUserRole" in out
        # logicType 30/40（包含/不包含）是角色分支唯一合法操作符
        assert "30" in out and "40" in out

    def test_generate_rules与roleRules互斥(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        assert "互斥" in out or "不可同时" in out or "二选一" in out, \
            "generate.j2 缺 rules/roleRules 互斥禁令"

    def test_generate_formFieldKey最终形态对象数组(self, loader):
        out = loader.render("njmind_bpm", "generate", guide=GUIDE,
                            process_template=PROCESS_TEMPLATE,
                            rule_templates=RULE_TEMPLATES,
                            form_catalog=FORM_CATALOG)
        assert "formFieldType" in out, \
            "generate.j2 缺 formFieldKey 最终形态 {formFieldType,formFieldKey} 说明"
        assert "approver" in out  # 字段目录注入后示例 key 可见

    def test_generate_守卫缺变量渲染不抛且契约段仍在(self, loader):
        """StrictUndefined 环境：新参考区用 is defined 守卫，旧调用点
        （只传 guide）不传新变量也不炸。"""
        out = loader.render("njmind_bpm", "generate", guide=GUIDE)
        assert "checkedRoleIds" in out  # 契约段是静态骨架，无变量依赖

    def test_parse_守卫缺变量渲染不抛(self, loader):
        out = loader.render("njmind_bpm", "parse", guide=GUIDE)
        assert "nodeIntents" in out
