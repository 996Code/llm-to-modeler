"""SubmitLeaveTool 集成测试——确认门槛三态 + 完整管线（此前该工具零测试覆盖）。

B3 加了提交确认门槛（不可逆操作须用户确认），本文件用 mock LLM/asset
走完整 execute() 管线验证三态：
  1. 字段齐全首跑 → 确认挂起（ClarificationRaised），不触上游；
  2. resume 答"确认" → 提交成功（artifact_type=data）；
  3. resume 答"取消" → 取消回复，上游 submit 零调用；
  4. 字段缺失 → 请假信息追问（既有行为不回归）。
"""
from unittest.mock import MagicMock

import pytest

from domains.leave_application.tools.submit_leave import SubmitLeaveTool
from sdk.tool import ToolContext, ClarificationRaised


FULL_FIELDS = {
    "applicant": "张三", "leaveType": "事假",
    "startDate": "2026-09-04", "endDate": "2026-09-06", "reason": "家事",
}


def _ctx(llm_parsed=None):
    llm = MagicMock()
    llm.chat_json.return_value = dict(llm_parsed or FULL_FIELDS)
    asset = MagicMock()
    # validate 通过 / submit 返回审批号
    asset.submit_data.return_value = {"success": True, "id": "PENDING-1"}
    ctx = ToolContext(llm_client=llm, asset_client=asset,
                      conversation=None, emit=lambda *a, **k: None)
    return ctx, llm, asset


def _assert_no_submit(asset):
    """断言 submit 路径零调用（validate_rules 也用 submit_data 发 POST，须区分）。"""
    for c in asset.submit_data.call_args_list:
        path = c.kwargs.get("path") or (c.args[0] if c.args else "")
        assert "submit" not in path, f"不可逆提交被触发: {path}"


class TestConfirmGate:
    def test_first_run_holds_before_submit(self):
        """字段齐全首跑：挂起确认，绝不直写上游。"""
        ctx, llm, asset = _ctx()
        with pytest.raises(ClarificationRaised) as ei:
            SubmitLeaveTool().execute({"user_input": "帮我请假"}, ctx)
        assert "确认提交" in ei.value.questions[0]
        _assert_no_submit(asset)  # submit 路径零调用是硬约束(validate 允许)

    def test_resume_confirm_submits(self):
        """resume 答"确认"（带挂起者标记，同真实 rerun 的 state）：完整跑通。"""
        ctx, llm, asset = _ctx()
        result = SubmitLeaveTool().execute(
            {"user_input": "帮我请假", "clarify_answers": {"text": "确认"},
             "_awaiting": "confirm"}, ctx)
        assert result.artifact_type == "data"
        assert result.artifact and result.artifact.get("approvalId") == "PENDING-1"
        # submit 路径恰好一次（validate 路径另计）
        submit_calls = [c for c in asset.submit_data.call_args_list
                        if "submit" in (c.kwargs.get("path") or "")]
        assert len(submit_calls) == 1

    def test_resume_cancel_never_submits(self):
        """resume 答"取消"（带挂起者标记）：取消回复，上游 submit 零调用。"""
        ctx, llm, asset = _ctx()
        result = SubmitLeaveTool().execute(
            {"user_input": "帮我请假", "clarify_answers": {"text": "取消"},
             "_awaiting": "confirm"}, ctx)
        assert "已取消" in result.reply
        _assert_no_submit(asset)

    def test_missing_fields_asks_for_info(self):
        """缺关键字段：请假信息追问（既有行为不因门槛回归）。"""
        ctx, _, asset = _ctx(llm_parsed={"applicant": "张三"})  # 缺类型/日期
        result = SubmitLeaveTool().execute({"user_input": "帮我请假"}, ctx)
        assert result.ask is not None  # 追问信息而非确认
        _assert_no_submit(asset)


class TestConfirmGateRerun:
    """端到端 rerun 路径（交叉终审 1a：隔离测试测不到的链路）。

    复刻真实 ask→resume→rerun：clarify_answers 由 nodes 注入 tool_state 且
    跨 run 残留——验证挂起者路由（_awaiting）防绕过。
    """

    def test_field_answer_does_not_bypass_confirm(self):
        """1a 主案：缺字段→追问→用户答字段→rerun 必须仍挂起确认（不直提）。

        旧缺陷：confirm 见到残留 answers 就当"已确认"放行——最常见路径
        100% 绕过门槛。_awaiting 路由后 answers 只被 parse_info 消费。
        """
        tool = SubmitLeaveTool()
        # run#1: 字段缺失 → parse_info 追问（设置 _awaiting=parse_info）
        ctx1, llm, asset = _ctx(llm_parsed={"applicant": "张三"})  # 缺类型/日期
        r1 = tool.execute({"user_input": "帮我请假"}, ctx1)
        assert r1.ask is not None
        _assert_no_submit(asset)

        # run#2(rerun): nodes 注入 answers={"text":"年假 9/4~9/6"}，
        # 模拟 checkpointer 持久化的同一 state dict 跨 run
        state2 = {"user_input": "帮我请假",
                  "clarify_answers": {"text": "年假 9月4日到6日"},
                  "_awaiting": "parse_info"}
        llm.chat_json.return_value = dict(FULL_FIELDS)  # 补充后字段齐全
        with pytest.raises(ClarificationRaised) as ei:
            tool.execute(state2, ctx1)
        assert "确认提交" in ei.value.questions[0]  # 必须挂起确认
        _assert_no_submit(asset)  # 未确认，绝不提交

        # run#3: 用户答"确认" → 提交
        state3 = {**state2, "clarify_answers": {"text": "确认"},
                  "_awaiting": "confirm"}
        result = tool.execute(state3, ctx1)
        assert result.artifact_type == "data"

    def test_confirm_answer_not_parsed_as_leave_info(self):
        """1b：确认回答不被 parse_info 当新信息重解析（_awaiting 隔离）。"""
        tool = SubmitLeaveTool()
        ctx, llm, asset = _ctx()
        # _awaiting=confirm: parse_info 不应把"确认"并进 user_input
        state = {"user_input": "帮我请假",
                 "clarify_answers": {"text": "确认"},
                 "_awaiting": "confirm"}
        tool.execute(state, ctx)
        sent = llm.chat_json.call_args_list[-1].args[0][-1]["content"]
        assert "确认" not in sent  # 确认词没混进解析输入

    def test_cancel_word_no_false_positive_on_shi_fou(self):
        """1e：「是否确认」类回复不再被子串"否"误判为取消。"""
        tool = SubmitLeaveTool()
        ctx, llm, asset = _ctx()
        state = {"user_input": "帮我请假",
                 "clarify_answers": {"text": "是否确认都行，提交吧"},
                 "_awaiting": "confirm"}
        result = tool.execute(state, ctx)
        # "是否"含"否"但不应触发取消 → 正常提交
        assert result.artifact_type == "data"
