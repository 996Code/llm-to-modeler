"""QueryLeaveStatusTool - 查询请假审批状态的工具(演示用,简单 Tool)。

【模块定位】
这是一个"领域工具"(Domain Tool),属于请假申请域(leave_application)。
被工作流引擎在识别到用户意图为"查询请假状态"时调用。

【Java 类比】
  - Tool ≈ Spring 里的 ``@Component`` 服务,实现某个 ``Tool<I, O>`` 接口
  - 整个工具系统 ≈ 策略模式 (Strategy Pattern):
      引擎根据用户意图,从一堆 Tool 里挑一个执行
  - 本 Tool 是简单工具(Tool),不是复合工具(CompositeTool)
    —— 类比普通 Service vs 编排型 Service(后者内部有多步流程)

【架构约定】
  - 所有上游调用走 ``ctx.asset_client``(AssetClient 抽象),不直接用 httpx
    → 类比 Java:不直接 new HttpClient,而是注入 ``@Autowired AssetClient``
    → 好处:可替换实现(真实接口 / Mock),便于单测
  - 上游地址经 service_name 寻址(宿主 services 表按请求下发,见 upstream.py)
"""
import logging
from typing import Any, Dict

from sdk.tool import Tool, ToolResult, ToolContext
from domains.leave_application.upstream import SERVICE_NAME, PATHS

logger = logging.getLogger(__name__)


class QueryLeaveStatusTool(Tool):
    """查询请假审批状态。

    【职责】
      接收用户的自然语言查询(如"我的请假批了吗"),调用上游接口,
      把结果格式化成人类可读的文本回复。

    【设计模式】
      策略模式的一个具体策略;由引擎的意图识别节点选中后实例化执行。

    【Java 类比】
      ``class QueryLeaveStatusTool implements Tool { ... }``
      等价于 Spring 里一个 ``@Component`` + ``@Qualifier("query_leave_status")`` 的 Bean。
    """

    # ── 工具元数据(供引擎做意图匹配 + 路由) ──
    # 类比 Java:相当于 @Component 的 bean name
    name = "query_leave_status"
    description = "查询请假审批状态"
    # when 字段是给 LLM 看的"何时该选这个工具"的自然语言描述
    # 引擎会把所有工具的 when 喂给 LLM,让它判断当前用户输入该走哪个工具
    when = "用户想查询请假审批状态,如'我的请假批了吗'、'查看审批进度'"

    # ── 安全声明(供引擎做风控决策) ──
    # 这些布尔标记类似 Java 注解 @ReadOnly / @Destructive 的运行时等价物

    def input_schema(self) -> dict:
        """声明工具的输入 schema(JSON Schema 格式)。

        【作用】
          1. 引擎据此校验 state 是否包含必需字段
          2. LLM 据此判断该工具能否处理当前输入

        【Java 类比】
          相当于方法签名的 ``Map<String, Object>`` 入参契约,
          或 Swagger/OpenAPI 的请求体定义。
        """
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息"},
            },
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行查询:通过 AssetClient 调上游 API,把结果格式化成回复。

        【容错策略】三级降级:
          1. NotImplementedError → AssetClient 未实现(可能是 Mock 没配)
          2. 其他 Exception      → 上游服务不可用
          3. 成功                → 格式化审批信息

        Args:
            state: 工作流状态字典,这里取 user_input
            ctx:   执行上下文,提供 asset_client 等依赖

        Returns:
            ToolResult,reply 和 summary 都填上格式化文本。
        """
        user_input = state.get("user_input", "")

        try:
            # 通过抽象的 AssetClient 调上游,不直接 httpx
            # forward_headers 透传鉴权头(嵌入模式下由宿主系统提供)
            result = ctx.asset_client.query_data(
                path=PATHS["status"],
                service_name=SERVICE_NAME,
                params={"query": user_input},
                headers=ctx.forward_headers,
            )
            logger.info(f"query_leave_status response: {result}")

            # 把上游返回的结构化数据格式化成人类可读的纯文本
            # reply 是给用户看的最终回复
            reply = (
                f"📋 请假审批状态查询结果:\n"
                f"   审批编号: {result.get('id', 'N/A')}\n"
                f"   状态: {result.get('status', 'N/A')}\n"
                f"   备注: {result.get('message', '')}"
            )
        except NotImplementedError:
            # AssetClient.query_data 没实现(抽象方法未被子类覆盖)
            # 类比 Java:抛 AbstractMethodError
            logger.warning("AssetClient.query_data not implemented")
            reply = "📋 查询功能暂不可用。（上游接口未配置）"
        except Exception as e:
            # 上游接口报错(网络 / 5xx / 超时等),降级返回友好提示
            # 不向上抛,保证用户体验(类比 Java:catch 后返回兜底值)
            logger.warning(f"query_leave_status API failed: {e}")
            reply = "📋 当前没有查询到请假审批记录。（上游服务不可用）"

        # reply 和 summary 都填同一个文本:
        #   - reply:   直接给用户看的回复
        #   - summary: 给对话历史 / 压缩器用的摘要(这里内容短,直接复用)
        return ToolResult(
            reply=reply,
            summary=reply,
        )
