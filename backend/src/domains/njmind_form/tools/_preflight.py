"""njmind_form 工具的执行前提校验（SDK preflight 钩子的领域实现）。

抽象层（sdk.tool.Tool.preflight）定义钩子与调用时机，本模块提供本 pack
共享的校验逻辑——业务自己声明"我执行的前提是什么"，引擎零领域知识。

当前前提只有一条：njmind-modeler 上游地址可解析（宿主 services 表按请求
下发）。缺地址时 fail-fast 拦截，不进管线、不烧 LLM/上游调用；真正的地址
解析仍由 resolve_base fail-closed 兜底（preflight 挡早，resolve 挡漏）。
"""
from typing import Optional

from sdk.tool import ToolContext, ToolResult

from domains.njmind_form.upstream import SERVICE_NAME as MODELER_SERVICE


def require_modeler_service(ctx: ToolContext) -> Optional[ToolResult]:
    """校验 njmind-modeler 地址可用。None=通过；ToolResult=拦截。

    has_service 不在 AssetClient 抽象契约内（通用 ABC 无此方法），
    用 getattr 探测：非 HTTP 实现（测试桩等）视为通过，运行期由
    resolve_base 的 fail-closed 兜底。
    """
    has = getattr(ctx.asset_client, "has_service", None)
    if callable(has) and not has(MODELER_SERVICE):
        return ToolResult(
            error_for_llm=(
                f"上游服务 {MODELER_SERVICE} 无可用地址：本请求的宿主 services 表"
                f"未下发该服务。请检查宿主 INIT 的 services 字段。"
            ),
            summary=f"缺少上游服务地址：{MODELER_SERVICE}（宿主未下发）",
        )
    return None
