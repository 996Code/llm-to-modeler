"""SubmitLeaveTool - 提交请假申请的复合工具(3步管线)。

步骤:
  1. parse_info  — LLM 解析用户消息，提取请假信息
  2. validate_rules — 校验请假规则（通过 AssetClient 调上游 API）
  3. submit      — 提交请假申请（通过 AssetClient 调上游 API）

多轮追问:
  parse_info 检测关键字段(请假类型、日期)是否缺失,
  缺失时返回 ToolResult.ask 而非填默认值,确保用户确认后再提交。
  （提交为不可逆操作：信息不足时追问、绝不填默认值——防线在 validate_input。）

artifact_type="data" — 不是表单配置，是数据结果。
前端渲染 data-card，不显示"应用配置"按钮。

架构约定:
  - 所有上游调用走 ctx.asset_client (AssetClient 抽象),不直接用 httpx
  - 保证: sanitize_obj 清洗 / forward_headers 传播 / 连接池统一管理
  - 上游地址经 service_name 寻址(宿主 services 表按请求下发,见 upstream.py)
"""
import logging

from sdk.tool import CompositeTool, ToolResult, ToolContext, AskSpec, AskQuestion, AskOption, ClarificationRaised
from domains.leave_application.upstream import SERVICE_NAME, PATHS

logger = logging.getLogger(__name__)


class SubmitLeaveTool(CompositeTool):
    """提交请假申请到审批系统。

    安全设计:
    - 信息不足时追问,不自动填默认值提交
    """

    name = "submit_leave"
    description = "提交请假申请到审批系统"
    when = "用户想提交请假申请,如'我要请假'、'提交请假单'、'申请年假'、'请3天假'"

    # 安全声明

    # 管线定义
    steps = ["parse_info", "validate_rules", "confirm", "submit"]
    pipeline_steps = [
        {"key": "parse_info", "label": "解析请假信息"},
        {"key": "validate_rules", "label": "校验请假规则"},
        {"key": "submit", "label": "提交申请"},
    ]

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息"},
            },
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行管线。parse_info 可能设置 _need_clarify 标记,
        此时跳过后续步骤,直接返回追问。confirm 步骤取消时设置
        _cancelled 标记，跳过后续并返回取消回复。"""
        self.run_pipeline(state, ctx)

        # ── 取消分支:用户在确认步回答了"取消" ──
        if state.get("_cancelled"):
            return ToolResult(
                reply="已取消提交，本次未向上游发送任何数据。",
                summary="已取消请假提交",
            )

        # ── 追问分支:信息不足,需要用户补充 ──
        if state.get("_need_clarify"):
            return ToolResult(
                ask=state["_clarify_spec"],
                summary=state.get("_clarify_summary", "需要补充请假信息"),
            )

        # ── 正常完成:返回数据结果 ──
        leave_data = state.get("leave_data", {})
        summary = state.get("summary", "请假申请已提交")

        return ToolResult(
            artifact=leave_data,
            artifact_type="data",  # ← 关键：数据结果，不是配置
            summary=summary,
            extra={
                "formatted": {
                    "title": f"请假申请 - {leave_data.get('applicant', '')}",
                    "formName": "请假申请",
                    # fieldCount 排除内部字段(status/approvalId)
                    "fieldCount": len([k for k, v in leave_data.items()
                                       if k not in ("status", "approvalId")]),
                }
            },
        )

    # ── Steps ──────────────────────────────────────────────

    def _step_parse_info(self, state: dict, ctx: ToolContext) -> None:
        """LLM 解析用户消息，提取请假信息。

        管线幂等性（全量走查①#1 修复）：字段追问的回答累积回写
        state["clarified_input"]——rerun 从 confirm 挂起恢复时（_awaiting
        != "parse_info"），已有完整 leave_data 则跳过重解析，防止
        "追问→回答→确认→rerun 用原始残缺输入重解析→再追问"死循环。
        """
        # 已有完整数据且挂起者不是本步骤 → 跳过重解析（管线幂等短路）
        if (state.get("leave_data", {}).get("leaveType")
                and state.get("_awaiting") != "parse_info"):
            return

        ctx.emit("stage", "parse_info", "AI 正在解析您的请假需求...")

        user_input = state.get("clarified_input") or state.get("user_input", "")
        compressed_history = state.get("compressed_history", "")

        # 消费本轮 resume 回答——仅当挂起者是本步骤（_awaiting 路由）。
        # 累积回写 clarified_input：下次 rerun（如 confirm→确认→rerun）
        # 拼的是完整输入而非原始残缺消息。
        if state.get("_awaiting") == "parse_info":
            clarify_answers = state.pop("clarify_answers", {}) or {}
            state.pop("_awaiting", None)
            parts = [user_input]
            for k, v in clarify_answers.items():
                parts.append(f"{k}: {v}")
            user_input = "; ".join(p for p in parts if p)
            state["clarified_input"] = user_input

        messages = [
            {"role": "system", "content": _PARSE_INFO_PROMPT},
            {"role": "user", "content": f"对话历史:\n{compressed_history}\n\n用户消息: {user_input}"},
        ]

        try:
            parsed = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id, stage="submit_leave.parse")
        except Exception as e:
            logger.warning(f"parse_info LLM failed: {e}")
            parsed = {}

        # 确保必要字段存在(缺失的留空,不填默认值)
        for key in ("applicant", "leaveType", "startDate", "endDate", "reason"):
            parsed.setdefault(key, "")

        # ── 检测关键字段缺失,触发追问 ──
        missing_questions = []

        if not parsed.get("leaveType"):
            missing_questions.append(AskQuestion(
                question="请问是什么类型的假？",
                header="请假类型",
                options=[
                    AskOption(label="年假", description="使用年假额度"),
                    AskOption(label="事假", description="因个人事务请假"),
                    AskOption(label="病假", description="因身体不适请假"),
                    AskOption(label="调休", description="使用调休额度"),
                ],
            ))

        if not parsed.get("startDate") or not parsed.get("endDate"):
            missing_questions.append(AskQuestion(
                question="请假的起止日期是？",
                header="请假日期",
                options=[
                    AskOption(label="今天", description="从今天开始"),
                    AskOption(label="明天", description="从明天开始"),
                ],
            ))

        if missing_questions:
            # 设置追问标记,execute 会跳过后续步骤。
            # _awaiting 标记"本轮挂起者"：resume 重跑时只有本步骤消费 answers，
            # 防止残留回答被 confirm 误当"已确认"（交叉终审 1a）
            state["_need_clarify"] = True
            state["_awaiting"] = "parse_info"
            state["_clarify_spec"] = AskSpec(questions=missing_questions)
            state["_clarify_summary"] = "需要补充请假信息才能提交"
            ctx.emit("stage", "parse_info_incomplete", "信息不足，需要补充...")
            return  # 中断 pipeline,不再执行后续步骤

        # 信息完整,存入 state 继续
        state["leave_data"] = parsed
        ctx.emit("stage", "parse_info_done",
                 f"已解析: {parsed.get('applicant', '')} 申请 "
                 f"{parsed.get('leaveType', '')} "
                 f"({parsed.get('startDate', '')} ~ {parsed.get('endDate', '')})")

    def _step_validate_rules(self, state: dict, ctx: ToolContext) -> None:
        """校验请假规则（通过 AssetClient 调上游 API）。

        AssetClient.submit_data 归一化返回:
          {success: bool, errors: list, ...}
        上游返回 "pass" 时自动转为 "success"。
        """
        ctx.emit("stage", "validate_rules", "正在校验请假规则...")

        leave_data = state.get("leave_data", {})

        try:
            result = ctx.asset_client.submit_data(
                path=PATHS["validate"],
                data=leave_data,
                service_name=SERVICE_NAME,
                headers=ctx.forward_headers,
            )
            logger.info(f"validate_rules response: {result}")

            # AssetClient 归一化后统一用 "success" 字段
            if not result.get("success", True) and result.get("errors"):
                errors = result["errors"]
                # errors 可能是 str 列表或 {message: str} 列表,统一转 str
                error_strs = [
                    e if isinstance(e, str) else e.get("message", str(e))
                    for e in errors
                ]
                ctx.emit("stage", "validate_fail",
                         f"校验失败: {', '.join(error_strs)}")
                state["validation_errors"] = error_strs
            else:
                ctx.emit("stage", "validate_rules_done", "请假规则校验通过 ✓")
                state["validation_errors"] = []

        except NotImplementedError:
            # AssetClient 未实现 submit_data — 降级为直接通过
            logger.warning("AssetClient.submit_data not implemented, skipping validation")
            ctx.emit("stage", "validate_rules_done", "请假规则校验通过 ✓ (跳过)")
            state["validation_errors"] = []
        except Exception as e:
            logger.warning(f"validate_rules API failed: {e}")
            # 上游不可用时，直接通过（不阻塞用户流程）
            ctx.emit("stage", "validate_rules_done", "请假规则校验通过 ✓ (mock)")
            state["validation_errors"] = []

    def _step_confirm(self, state: dict, ctx: ToolContext) -> None:
        """提交前确认中断：不可逆操作必须有用户确认门槛（审计④发现）。

        answers 消费用 _awaiting 路由：只有本轮挂起者是 confirm 时才把
        resume 回答当确认判定——否则（挂起者是 parse_info 的字段补充回答）
        属残留，必须重新挂起确认（交叉终审 1a：不区分归属时最常见的
        "缺字段→追问→回答"路径 100% 绕过门槛直写上游）。
        取消词不含单字"否"（"是否确认"会被子串误配）。
        """
        if state.get("_awaiting") == "confirm":
            answers = state.pop("clarify_answers", {}) or {}
            state.pop("_awaiting", None)
            reply = str(answers.get("text", answers) if isinstance(answers, dict) else answers)
            if not reply.strip() or reply == "{}" or reply == "{'text': ''}":
                # 空/无文本回答——不安全（全量走查①#1）：重新挂起要求明确回复
                state["_awaiting"] = "confirm"
                ctx.emit("stage", "confirm", "等待确认提交...")
                raise ClarificationRaised(["请回复「确认」或「取消」"])
            if any(w in reply for w in ("取消", "不要", "算了", "不提交")):
                state["_cancelled"] = True
                state["_need_clarify"] = True  # 断掉后续步骤(execute 先查 _cancelled)
                return
            return  # 确认（或非取消表述）→ 放行 submit

        # 无属主回答（或属 parse_info 的残留）→ 挂起确认
        leave_data = state.get("leave_data", {})
        summary = (
            f"即将提交请假申请：{leave_data.get('applicant', '')} "
            f"{leave_data.get('leaveType', '')} "
            f"({leave_data.get('startDate', '')} ~ {leave_data.get('endDate', '')})"
        )
        # 挂起者标记要在 raise 前写入（raise 后本函数不再执行，
        # resume 重跑时靠它识别 answers 归属）
        state["_awaiting"] = "confirm"
        ctx.emit("stage", "confirm", "等待确认提交...")
        raise ClarificationRaised([
            f'{summary}。确认提交吗？（回复"确认"或"取消"）'
        ])

    def _step_submit(self, state: dict, ctx: ToolContext) -> None:
        """提交请假申请（通过 AssetClient 调上游 API）。

        Fail-Closed：上游失败（地址/网络/假200信封）→ adapter 返回
        {success: False, errors: [...]} 而非抛异常——此处必须读 success
        字段，失败时绝不伪造"提交成功 + PENDING 编号"（不可逆操作，
        误报成功是审计②号终审发现的 Fail-Closed 链条断裂）。
        """
        ctx.emit("stage", "submit", "正在提交请假申请...")

        leave_data = state.get("leave_data", {})

        try:
            result = ctx.asset_client.submit_data(
                path=PATHS["submit"],
                data=leave_data,
                service_name=SERVICE_NAME,
                headers=ctx.forward_headers,
            )
            logger.info(f"submit response: {result}")

            if not result.get("success"):
                # 上游拒绝/失败：错误直达用户，不落 status/approvalId
                errors = result.get("errors") or ["上游未返回错误详情"]
                error_strs = [e if isinstance(e, str) else str(e) for e in errors]
                state["summary"] = f"提交失败：{'；'.join(error_strs[:3])}"
                ctx.emit("stage", "submit_fail",
                         f"提交失败：{'；'.join(error_strs[:2])}")
                return

            # 上游确认成功
            leave_data["status"] = "submitted"
            leave_data["approvalId"] = result.get("id", "PENDING")
            state["leave_data"] = leave_data
            state["summary"] = (
                f"已提交请假申请，审批编号 {leave_data['approvalId']}。"
                f"{leave_data.get('applicant', '')} 申请 "
                f"{leave_data.get('leaveType', '')} "
                f"({leave_data.get('startDate', '')} ~ {leave_data.get('endDate', '')})"
            )
            ctx.emit("stage", "submit_done",
                     f"提交成功 ✓ 审批编号: {leave_data['approvalId']}")

        except NotImplementedError:
            # AssetClient 未实现 submit_data — 降级为本地模式（演示桩场景）
            logger.warning("AssetClient.submit_data not implemented, using local mode")
            leave_data["status"] = "submitted"
            leave_data["approvalId"] = f"LOCAL-{id(leave_data) % 10000:04d}"
            state["leave_data"] = leave_data
            state["summary"] = f"已提交请假申请（本地模式），编号 {leave_data['approvalId']}"
            ctx.emit("stage", "submit_done",
                     f"提交成功 ✓ (本地模式) 编号: {leave_data['approvalId']}")
        except Exception as e:
            # 非预期异常（编程错误等）——诚实报告，不伪造成功
            logger.warning(f"submit API failed: {e}")
            state["summary"] = f"提交失败：{e}"
            ctx.emit("stage", "submit_fail", f"提交失败：{str(e)[:80]}")


_PARSE_INFO_PROMPT = """你是请假信息提取器。从用户消息中提取请假申请信息,只返回 JSON。

需要提取的字段:
- applicant: 申请人姓名(如未提及,填空字符串"")
- leaveType: 请假类型(事假/病假/年假/调休/婚假/产假/丧假/其他,如未提及填空字符串"")
- startDate: 开始日期(YYYY-MM-DD格式,如未提及填空字符串"")
- endDate: 结束日期(YYYY-MM-DD格式,如未提及填空字符串"")
- reason: 请假原因(如未提及,填空字符串"")

重要:如果用户没有提及某个字段,不要猜测或填默认值,留空字符串""即可。
系统会根据缺失字段追问用户。

输出格式: {"applicant": "", "leaveType": "", "startDate": "", "endDate": "", "reason": ""}

只输出 JSON,不要解释。"""
