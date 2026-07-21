"""QueryLeaveStatusTool - 查询请假审批状态。

演示简单 Tool（非 CompositeTool），返回 reply 通道（纯文本）。

架构约定:
  - 所有上游调用走 ctx.asset_client (AssetClient 抽象),不直接用 httpx
  - 上游 base_url 通过环境变量 ASSET_BASE_URL 配置,默认 mock API
"""
import logging
from typing import Any, Dict

from sdk.tool import Tool, ToolResult, ToolContext

logger = logging.getLogger(__name__)


class QueryLeaveStatusTool(Tool):
    """查询请假审批状态。"""

    name = "query_leave_status"
    description = "查询请假审批状态"
    when = "用户想查询请假审批状态,如'我的请假批了吗'、'查看审批进度'"

    # 安全声明
    is_destructive = False
    is_read_only = True
    is_concurrency_safe = True
    requires_existing_artifact = False

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息"},
            },
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """查询审批状态（通过 AssetClient 调上游 API）。"""
        user_input = state.get("user_input", "")

        try:
            result = ctx.asset_client.query_data(
                path="/api/leave/status",
                params={"query": user_input},
                headers=ctx.forward_headers,
            )
            logger.info(f"query_leave_status response: {result}")

            reply = (
                f"📋 请假审批状态查询结果:\n"
                f"   审批编号: {result.get('id', 'N/A')}\n"
                f"   状态: {result.get('status', 'N/A')}\n"
                f"   备注: {result.get('message', '')}"
            )
        except NotImplementedError:
            logger.warning("AssetClient.query_data not implemented")
            reply = "📋 查询功能暂不可用。（上游接口未配置）"
        except Exception as e:
            logger.warning(f"query_leave_status API failed: {e}")
            reply = "📋 当前没有查询到请假审批记录。（上游服务不可用）"

        return ToolResult(
            reply=reply,
            summary=reply,
        )
