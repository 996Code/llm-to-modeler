"""BPM 审批人与角色分支解析测试（Plan2 Task3）。

覆盖：
- resolve 纯逻辑层：角色→id 对象、部门→id、roleRules.values→code、
  人员搜索单结果直选/零结果追问/多结果追问（label 唯一化）、
  追问 header 名词指纹 + 忽略单条、resume label 反查、自由输入重搜索
- 工具层：管线 steps 三同步、render 输入无中文身份语义、
  目录降级零追问继续管线、resume 不烧 LLM、`__` 键不进 parse 输入
- upstream 层：get_org_catalog 端点、search_users 的 X-Org-Token 头、
  未配 token 不发请求
"""
import json
from unittest.mock import MagicMock

import pytest

from domains.njmind_bpm import service_locator
from domains.njmind_bpm.tools import _permission_catalog as pc
from sdk.tool import ToolContext, ClarificationRaised


@pytest.fixture(autouse=True)
def bpm_api(monkeypatch):
    api = MagicMock()
    service_locator.reset_bpm_transport()
    service_locator.set_api_for_testing(api)
    yield api
    service_locator.reset_bpm_transport()


ROLE_ROWS = [
    {"id": "r1", "code": "FIN", "name": "财务角色"},
    {"id": "r2", "code": "FIN_MGR", "name": "财务主管"},
    {"id": "r3", "code": "SALE_MGR", "name": "销售主管"},
]
DEPT_ROWS = [
    {"id": "d1", "name": "财务部"},
    {"id": "d2", "name": "研发部"},
]


class FakeOrgApi:
    """受控人员搜索 fake：keyword → 结果列表（None=搜索不可用）。"""

    def __init__(self, users=None):
        self.users = users or {}
        self.search_calls = []

    def search_users(self, keyword):
        self.search_calls.append(keyword)
        return self.users.get(keyword)


def _tok(semantic: str) -> str:
    return pc.semantic_token(semantic)


def _draft_cn():
    """带中文身份名词的 draft（中间态契约形态）。"""
    return {
        "formKey": "leave_form",
        "topology": {
            "nodes": [
                {"key": "start", "type": "START_EVENT", "name": "开始"},
                {"key": "manager", "type": "USER_TASK", "name": "主管审批"},
                {"key": "end", "type": "END_EVENT", "name": "结束"},
            ],
            "edges": [
                {"key": "e1", "from": "start", "to": "manager"},
                {"key": "e_fin", "from": "manager", "to": "end"},
            ],
        },
        "approveRules": [{
            "nodeKey": "manager", "approveOptRange": 80,
            "approveOptRangeSettings": {
                "checkedRoleIds": ["财务角色"],
                "checkedDepIds": ["财务部"],
                "checkedUserIds": ["张三"],
            },
        }],
        "ccRules": [],
        "branchRules": [{
            "edgeKey": "e_fin", "logic": 10,
            "roleRules": [{"formFieldKey": "loginUserRole", "logicType": 30,
                           "values": ["财务角色"]}],
        }],
    }


# ── resolve 纯逻辑层 ─────────────────────────────────────────

class TestResolveDirect:
    def test_role_to_id_object_dep_to_id_rolevalues_to_code(self):
        """角色→{roleId,depAndSupDep:0,condition:None}；部门→id；
        roleRules.values→code；人员单结果直选；全部落定零追问。"""
        api = FakeOrgApi({"张三": [{"id": "u1", "label": "张三｜财务部"}]})
        draft = _draft_cn()
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert questions == []
        s = draft["approveRules"][0]["approveOptRangeSettings"]
        assert s["checkedRoleIds"] == [
            {"roleId": "r1", "depAndSupDep": 0, "condition": None}]
        assert s["checkedDepIds"] == ["d1"]
        assert s["checkedUserIds"] == ["u1"]
        assert draft["branchRules"][0]["roleRules"][0]["values"] == ["FIN"]
        assert api.search_calls == ["张三"]

    def test_idempotent_rerun_passes_through(self):
        """幂等：解析后再跑一轮，对象/id/code 原样透传、零搜索零追问。"""
        api = FakeOrgApi({"张三": [{"id": "u1", "label": "张三｜财务部"}]})
        draft = _draft_cn()
        pc.resolve_bpm_permissions(draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        n_calls = len(api.search_calls)
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert questions == []
        assert len(api.search_calls) == n_calls      # 不重查
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedUserIds"] == ["u1"]

    def test_ascii_and_object_passthrough(self):
        """存量 ASCII id/code 与已解析对象透传（不清已有）。"""
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"] = {
            "checkedRoleIds": [{"roleId": "r9", "depAndSupDep": 0,
                                "condition": None}],
            "checkedDepIds": ["d9"],
            "checkedUserIds": ["u9"],
        }
        draft["branchRules"][0]["roleRules"][0]["values"] = ["FIN"]
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, FakeOrgApi())
        assert questions == []
        s = draft["approveRules"][0]["approveOptRangeSettings"]
        assert s["checkedRoleIds"] == [{"roleId": "r9", "depAndSupDep": 0,
                                        "condition": None}]
        assert s["checkedDepIds"] == ["d9"]
        assert s["checkedUserIds"] == ["u9"]


class TestUserSearchAsk:
    def test_multi_result_question_labels_and_header(self):
        """多结果 → 追问（label「姓名｜部门」+忽略项）；header 名词指纹。"""
        api = FakeOrgApi({"张三": [
            {"id": "u1", "label": "张三｜财务部"},
            {"id": "u2", "label": "张三｜研发部"},
        ]})
        draft = _draft_cn()
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == \
            f"__bpm_user:manager:checkedUserIds:{_tok('张三')}"
        labels = [o["label"] for o in q["options"]]
        assert "张三｜财务部" in labels and "张三｜研发部" in labels
        assert pc.IGNORE_LABEL in labels

    def test_zero_result_asks_for_more_accurate_name(self):
        """零结果 → 追问「更准确的姓名或工号」（仅忽略项可点）。"""
        api = FakeOrgApi({"李四": []})
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"]["checkedUserIds"] = ["李四"]
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert "更准确的姓名或工号" in q["question"]
        assert [o["label"] for o in q["options"]] == [pc.IGNORE_LABEL]

    def test_two_rounds_answer_label_resolves_id(self):
        """两轮：多结果追问 → 答 label → id 落定、无第二次 LLM 语义。"""
        api = FakeOrgApi({"张三": [
            {"id": "u1", "label": "张三｜财务部"},
            {"id": "u2", "label": "张三｜研发部"},
        ]})
        draft = _draft_cn()
        header = f"__bpm_user:manager:checkedUserIds:{_tok('张三')}"
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert len(questions) == 1
        # 第二轮：带答案重放
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {header: "张三｜研发部"}, api)
        assert questions == []
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedUserIds"] == ["u2"]

    def test_free_text_answer_researches(self):
        """自由输入答案（前端"其他"）：按答案文本重搜索，单结果直选。"""
        api = FakeOrgApi({"李四": [], "李四四": [{"id": "u9", "label": "李四四｜财务部"}]})
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"]["checkedUserIds"] = ["李四"]
        header = f"__bpm_user:manager:checkedUserIds:{_tok('李四')}"
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {header: "李四四"}, api)
        assert questions == []
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedUserIds"] == ["u9"]
        assert "李四四" in api.search_calls

    def test_search_unavailable_strips_without_asking(self):
        """搜索不可用（None=未配 token/失败）→ 剥中文名词降级，不追问。"""
        api = FakeOrgApi({"张三": None})
        draft = _draft_cn()
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, api)
        assert questions == []
        s = draft["approveRules"][0]["approveOptRangeSettings"]
        assert "checkedUserIds" not in s


class TestRoleAskAndIgnore:
    def test_ambiguous_role_question_and_answer(self):
        """角色歧义（「主管」包含命中两个）→ 追问 → label 反查 → id 对象。"""
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedRoleIds"] = ["主管"]
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, FakeOrgApi())
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == \
            f"__bpm_role:manager:checkedRoleIds:{_tok('主管')}"
        labels = [o["label"] for o in q["options"]]
        assert "财务主管（FIN_MGR）" in labels
        assert "销售主管（SALE_MGR）" in labels
        assert pc.IGNORE_LABEL in labels
        # resume：label 反查
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS,
            {q["header"]: "销售主管（SALE_MGR）"}, FakeOrgApi())
        assert questions == []
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedRoleIds"] == [{"roleId": "r3", "depAndSupDep": 0,
                                   "condition": None}]

    def test_ignore_drops_single_noun_only(self):
        """忽略仅丢弃单条名词：同组已解析值不连坐。"""
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedRoleIds"] = ["财务角色", "主管"]
        header = f"__bpm_role:manager:checkedRoleIds:{_tok('主管')}"
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS,
            {header: pc.IGNORE_LABEL}, FakeOrgApi())
        assert questions == []
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedRoleIds"] == [{"roleId": "r1", "depAndSupDep": 0,
                                   "condition": None}]

    def test_rolevalues_ambiguous_asks_with_role_header(self):
        """roleRules.values 歧义 → __bpm_role 前缀追问（anchor=edgeKey）。"""
        draft = _draft_cn()
        draft["branchRules"][0]["roleRules"][0]["values"] = ["主管"]
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS, {}, FakeOrgApi())
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == f"__bpm_role:e_fin:roleRules:{_tok('主管')}"
        # resume 反查 → code
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, DEPT_ROWS,
            {q["header"]: "财务主管（FIN_MGR）"}, FakeOrgApi())
        assert questions == []
        assert draft["branchRules"][0]["roleRules"][0]["values"] == ["FIN_MGR"]

    def test_dep_ambiguous_asks_with_dep_header(self):
        """部门歧义（包含命中多个）→ __bpm_dep 前缀追问。"""
        dep_rows = [{"id": "d1", "name": "财务一部"},
                    {"id": "d3", "name": "财务二部"}]
        draft = _draft_cn()
        draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedDepIds"] = ["财务"]
        questions = pc.resolve_bpm_permissions(
            draft, ROLE_ROWS, dep_rows, {}, FakeOrgApi())
        assert len(questions) == 1
        q = json.loads(questions[0])
        assert q["header"] == \
            f"__bpm_dep:manager:checkedDepIds:{_tok('财务')}"
        labels = [o["label"] for o in q["options"]]
        assert "财务一部（d1）" in labels and "财务二部（d3）" in labels


class TestCatalogDegradation:
    def test_empty_catalogs_strip_chinese_no_questions(self):
        """目录空 → 中文名词剥除（settings 组空整键省略）、零追问。"""
        draft = _draft_cn()
        questions = pc.resolve_bpm_permissions(
            draft, [], [], {}, FakeOrgApi({"张三": None}))
        assert questions == []
        # 中文身份语义剥除后 settings 组空 → 整键省略
        assert "approveOptRangeSettings" not in draft["approveRules"][0]
        # roleRules values 全中文剥除后组空 → roleRules 键省略
        assert "roleRules" not in draft["branchRules"][0]


# ── 工具层（generate 管线）────────────────────────────────────

def make_ctx(llm, emit=None):
    from sdk.tool import ToolContext
    events = []

    def _emit(*args):
        events.append(args)

    return ToolContext(
        llm_client=llm, asset_client=None, conversation=None,
        emit=emit or _emit, conv_id="t"), events


def llm_returning(seq):
    m = MagicMock()
    m.chat_json.side_effect = list(seq)
    return m


VALID_RENDER = {
    "bpmnXml": "<xml/>",
    "rules": {"formKey": "leave_form",
              "approveRules": [{"taskId": "Activity_1"}],
              "ccRules": [], "branchRules": []},
    "validation": {"pass": True, "errors": [], "warnings": []},
}

HOST_ART = {
    "modelKey": "leave_process", "name": "请假流程",
    "formKey": "leave_form", "bpmnXml": None, "rules": {},
}

GOOD_INTENTS = {
    "needsClarification": False,
    "processSummary": "请假审批",
    "nodeIntents": [
        {"name": "主管审批", "nodeType": "USER_TASK", "approver": "财务角色"},
    ],
    "branchIntents": [],
}


def _pipeline_api(users=None):
    """组织目录可用 + 受控人员搜索的 fake BpmModelerAPI。"""
    api = service_locator.get_api()
    api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
    api.get_form.return_value = None
    api.get_template.return_value = {}
    api.render.return_value = VALID_RENDER
    api.get_org_catalog.side_effect = lambda t: (
        ROLE_ROWS if t == "role" else DEPT_ROWS)
    api.search_users.side_effect = (
        lambda kw: (users or {}).get(kw))
    return api


class TestStepsSync:
    def test_steps_and_pipeline_steps_sync(self):
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        tool = GenerateProcessTool()
        assert tool.steps == [
            "fetch_guide", "fetch_existing", "fetch_form_catalog",
            "fetch_org_catalogs", "parse_intents", "fetch_templates",
            "generate", "resolve_permissions", "render"]
        assert [s["key"] for s in tool.pipeline_steps] == tool.steps


class TestPipelineResolvesBeforeRender:
    def test_render_input_has_no_chinese_identity(self):
        """render 收到的 draft：身份语义全 id/code，无中文。"""
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        api = _pipeline_api({"张三": [{"id": "u1", "label": "张三｜财务部"}]})
        llm = llm_returning([GOOD_INTENTS, _draft_cn()])
        ctx, events = make_ctx(llm)
        state = {"user_input": "财务角色审批，张三审批",
                 "source_artifact": dict(HOST_ART)}

        result = GenerateProcessTool().execute(state, ctx)

        assert result.artifact is not None
        sent = api.render.call_args_list[0].args[0]
        s = sent["approveRules"][0]["approveOptRangeSettings"]
        assert s["checkedRoleIds"] == [
            {"roleId": "r1", "depAndSupDep": 0, "condition": None}]
        assert s["checkedDepIds"] == ["d1"]
        assert s["checkedUserIds"] == ["u1"]
        assert sent["branchRules"][0]["roleRules"][0]["values"] == ["FIN"]
        # 身份语义无中文（节点名等业务语义不在断言范围）
        identity = json.dumps(
            [s["checkedRoleIds"], s["checkedDepIds"], s["checkedUserIds"],
             sent["branchRules"][0]["roleRules"][0]["values"]],
            ensure_ascii=False)
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in identity)
        stages = [e[1] for e in events if e[0] == "stage"]
        assert "resolve_permissions_done" in stages
        assert "render_pass" in stages


class TestPipelineDegradation:
    def test_catalogs_unavailable_continues_without_asking(self):
        """目录拉取失败/空 → 空列表继续管线、零追问、中文剥除不进 render。"""
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        api = service_locator.get_api()
        api.get_guide.return_value = {"nodeTypes": [{"type": "USER_TASK"}]}
        api.get_form.return_value = None
        api.get_template.return_value = {}
        api.render.return_value = VALID_RENDER
        api.get_org_catalog.return_value = []          # modeler 未升级
        api.search_users.return_value = None           # 未配 token

        llm = llm_returning([GOOD_INTENTS, _draft_cn()])
        ctx, events = make_ctx(llm)
        state = {"user_input": "做个请假审批", "source_artifact": dict(HOST_ART)}

        result = GenerateProcessTool().execute(state, ctx)

        assert result.artifact is not None             # 管线继续，不追问
        assert "_permission_pending_draft" not in state
        sent = api.render.call_args_list[0].args[0]
        # 中文身份语义剥除后 settings 组空 → 整键省略
        assert "approveOptRangeSettings" not in sent["approveRules"][0]
        assert "roleRules" not in sent["branchRules"][0] or \
            not sent["branchRules"][0]["roleRules"]
        stages = [e[1] for e in events if e[0] == "stage"]
        assert "render_pass" in stages


class TestResumeSkipsLLM:
    def test_resume_answers_without_burning_llm(self):
        """挂起 → resume：LLM call_count 不增，answers 回填落定。"""
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        api = _pipeline_api({"张三": [
            {"id": "u1", "label": "张三｜财务部"},
            {"id": "u2", "label": "张三｜研发部"},
        ]})
        llm = llm_returning([GOOD_INTENTS, _draft_cn()])
        ctx, _ = make_ctx(llm)
        state = {"user_input": "张三审批", "source_artifact": dict(HOST_ART)}

        with pytest.raises(ClarificationRaised) as ei:
            GenerateProcessTool().execute(state, ctx)
        assert llm.chat_json.call_count == 2            # parse + generate
        q = json.loads(ei.value.questions[0])
        assert q["header"] == f"__bpm_user:manager:checkedUserIds:{_tok('张三')}"

        state["clarify_answers"] = {q["header"]: "张三｜研发部"}
        result = GenerateProcessTool().execute(state, ctx)
        assert llm.chat_json.call_count == 2            # resume 未烧 LLM
        assert result.artifact is not None
        sent = api.render.call_args_list[-1].args[0]
        assert sent["approveRules"][0]["approveOptRangeSettings"][
            "checkedUserIds"] == ["u2"]
        assert "_permission_pending_draft" not in state


class TestParseSkipsUnderscoreKeys:
    def test_parse_input_excludes_double_underscore_keys(self):
        """`__` 前缀追问回答不拼入 parse 的 user_input。"""
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        llm = MagicMock()
        llm.chat_json.return_value = GOOD_INTENTS
        ctx, _ = make_ctx(llm)
        state = {
            "user_input": "做个审批流",
            "guide": {},
            "clarify_answers": {
                "__bpm_user:manager:checkedUserIds:x": "张三｜研发部"},
        }
        GenerateProcessTool()._step_parse_intents(state, ctx)
        user_msg = llm.chat_json.call_args[0][0][1]["content"]
        assert "__bpm_user" not in user_msg
        assert "张三" not in user_msg


# ── upstream 层 ──────────────────────────────────────────────

class FakeTransport:
    def __init__(self, data=None):
        self.data = data if data is not None else []
        self.calls = []

    def get(self, service, path, *, auth=False, cache=False,
            params=None, extra_headers=None):
        self.calls.append({"path": path, "params": params,
                           "headers": extra_headers})
        return self.data


class TestUpstreamOrgEndpoints:
    def _api(self, transport):
        from domains.njmind_bpm.upstream import BpmModelerAPI
        return BpmModelerAPI(transport)

    def test_get_org_catalog_hits_declared_path(self):
        t = FakeTransport([{"id": "r1", "code": "FIN", "name": "财务角色"}])
        rows = self._api(t).get_org_catalog("role")
        assert rows[0]["code"] == "FIN"
        assert t.calls[0]["path"] == "/api/mcp/org/catalog"
        assert t.calls[0]["params"] == {"type": "role"}

    def test_search_users_sends_org_token_header(self, monkeypatch):
        monkeypatch.setenv("ORG_MCP_TOKEN", "secret-token")
        t = FakeTransport([{"id": "u1", "label": "张三｜财务部"}])
        rows = self._api(t).search_users("张三")
        assert rows[0]["id"] == "u1"
        assert t.calls[0]["path"] == "/api/mcp/org/users"
        assert t.calls[0]["params"] == {"keyword": "张三"}
        assert t.calls[0]["headers"]["X-Org-Token"] == "secret-token"

    def test_search_users_without_token_sends_nothing(self, monkeypatch):
        monkeypatch.delenv("ORG_MCP_TOKEN", raising=False)
        t = FakeTransport([{"id": "u1", "label": "张三｜财务部"}])
        rows = self._api(t).search_users("张三")
        assert rows is None                       # 降级：不发请求
        assert t.calls == []

    def test_get_org_catalog_failure_returns_empty(self):
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("network down")

        assert self._api(Boom()).get_org_catalog("role") == []


class TestRenderRetryPreservesResolvedPermissions:
    def test_render_retry_restores_resolved_snapshot_not_regenerate(self):
        """render 校验失败重试时：权限已解析成功的 draft 必须冻结恢复，
        不得重跑 LLM 重生成（重生成的名词漂移会让已答追问失效——
        真实事故：用户答「部门经理」后 render 重试产「部门经理角色」死循环）。"""
        import types
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        tool = GenerateProcessTool()
        resolved_draft = {
            "approveRules": [{"nodeKey": "manager", "approveOptRangeSettings": {
                "checkedRoleIds": [{"roleId": "101", "depAndSupDep": 0, "condition": None}]}}],
            "branchRules": [], "topology": {"nodes": [], "edges": []},
        }
        llm_calls = []
        class _LLM:
            def chat_json(self, *a, **k):
                llm_calls.append(1)
                return {"approveRules": [{"nodeKey": "manager", "approveOptRangeSettings": {
                    "checkedRoleIds": ["部门经理角色"]}}], "branchRules": [],
                    "topology": {"nodes": [], "edges": []}}
        render_count = []
        class _Api:
            def render(self, d):
                render_count.append(1)
                n = len(render_count)
                if n == 1:
                    return {"validation": {"pass": False, "errors": [
                        {"message": "拓扑缺开始节点", "code": "TOPOLOGY_INVALID"}]}}
                return {"validation": {"pass": True, "errors": [], "warnings": []},
                        "bpmnXml": "<xml/>", "rules": {}}
        class _Ctx:
            emit = staticmethod(lambda *a, **k: None)
            llm_client = _LLM()
        state = {
            "_permission_resolved_draft": resolved_draft,
            "raw_draft": resolved_draft,
            "retry_count": 0,
            "source_artifact": {"modelKey": "test_flow", "name": "测试流程",
                                "rules": {}},
        }
        import domains.njmind_bpm.service_locator as sl
        old = sl.get_api
        sl.get_api = lambda: _Api()
        try:
            tool._step_render(state, types.SimpleNamespace(emit=lambda *a, **k: None,
                                                           llm_client=_LLM()))
        finally:
            sl.get_api = old
        assert state["artifact"], "重试后应产出 artifact"
        rules = state["artifact"]["rules"]
        assert llm_calls == [], f"render 重试不得烧 LLM（实烧 {len(llm_calls)} 次）"
        ar = state["artifact"]
        # resolved 快照里的真实 roleId 保留（未被名词漂移覆盖）
        raw_after = state.get("raw_draft") or {}
        kept = any(
            (r.get("approveOptRangeSettings") or {}).get("checkedRoleIds") == [
                {"roleId": "101", "depAndSupDep": 0, "condition": None}]
            for r in (raw_after.get("approveRules") or []))
        assert kept, "恢复后 draft 应保留已解析的真实 roleId"


class TestFinalReviewFixes:
    def test_bank_replay_survives_nested_clarify_round(self):
        """状态机-高：权限问完→分支字段门中断→resume 只带字段答案时，
        bank 里已消费的 __bpm_* 答案必须参与解析（不丢、不重问）。"""
        from domains.njmind_bpm.tools.generate_process import GenerateProcessTool
        tool = GenerateProcessTool()
        draft = {"approveRules": [{"nodeKey": "m", "approveOptRangeSettings": {
            "checkedRoleIds": ["独角兽骑士"]}}],
            "branchRules": [], "topology": {"nodes": [], "edges": []}}
        roles = [{"id": "101", "code": "BM", "name": "部门总管实际名"}, {"id": "102", "code": "XG", "name": "相关方"}]
        # 第一轮：零命中 → 必问
        qs = pc.resolve_bpm_permissions(draft, roles, [], {}, None)
        assert len(qs) == 1
        # 模拟引擎侧 bank 已累积该答案（resolve 成功路径写入）
        bank = {"__bpm_role:m:checkedRoleIds:<fp>": "部门总管实际名（BM）"}
        # 第二轮：分支门 resume（answers 只有字段答案，无 __bpm_*）
        merged = {**bank, "some_field": "文本输入框"}
        qs2 = pc.resolve_bpm_permissions(draft, roles, [], merged, None)
        # bank 的指纹 <fp> 与 draft 名词指纹一致才能消费——真实指纹由
        # pc 内部生成，此处用 pc 的指纹函数对齐
        from sdk.catalog import semantic_token
        fp = semantic_token("独角兽骑士")
        bank = {f"__bpm_role:m:checkedRoleIds:{fp}": "部门总管实际名（BM）"}
        merged = {**bank, "some_field": "文本输入框"}  # 模拟分支门 resume 只带新答案
        qs3 = pc.resolve_bpm_permissions(draft, roles, [], merged, None)
        assert qs3 == [], f"bank 答案应被消费，仍重问: {qs3[:1]}"
        assert draft["approveRules"][0]["approveOptRangeSettings"][
            "checkedRoleIds"] == [{"roleId": "101", "depAndSupDep": 0, "condition": None}]
