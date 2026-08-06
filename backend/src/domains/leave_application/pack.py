"""leave_application pack - 请假申请提交(数据类插件示例)。

【模块定位】
这是 domains/ 自动发现机制约定的工具包入口文件。与 njmind_form pack 不同,
本 pack 演示"非配置类插件":工具产出的是业务数据(artifact_type="data"),
前端用数据卡片(data-card)渲染,而非表单配置。

【本 pack 提供的工具】
  - submit_leave:        提交请假申请(CompositeTool,内部 3 步管线)
  - query_leave_status:  查询请假审批状态(普通 Tool)

【插件化约定】
  - create_registry() 必须提供,返回 ToolRegistry —— 本 pack 注册上面 2 个工具。
  - create_prompt_loader() 返回 None —— 本 pack 不需要自定义 prompt,
    系统会使用第一个提供 prompt_loader 的 pack(如 njmind_form)。

【artifact_type 的区别】
对比 njmind_form(artifact_type="config",产出可应用的表单配置):
本 pack 的 artifact_type="data",Engine 不会把结果当配置存储/应用,
只把摘要写进对话历史,前端渲染为数据卡片。这是 ToolResult 协议的核心区分
(见 sdk/tool.py 的 ToolResult.artifact_type)。

【Java 类比】
类似一个 Spring @Configuration 配置类,声明本业务模块的服务集合。
与其他 @Configuration 共存于同一 ApplicationContext,通过契约函数
(create_registry / create_prompt_loader)被框架统一装配。
"""
from pathlib import Path

from sdk.registry import ToolRegistry
# 导入本 pack 的工具实现。SubmitLeaveTool 是 CompositeTool(多步管线),
# QueryLeaveStatusTool 是普通单步 Tool。两者都继承自 sdk.tool.Tool。
from domains.leave_application.tools.submit_leave import SubmitLeaveTool
from domains.leave_application.tools.query_status import QueryLeaveStatusTool


def create_registry() -> ToolRegistry:
    """创建并注册 leave_application pack 的工具。

    Returns:
        装好 2 个工具的 ToolRegistry:
        - submit_leave(CompositeTool):提交请假,内部分步骤执行
        - query_leave_status(Tool):查询审批状态

    【Java 类比】
    等价于:
        ToolRegistry r = new ToolRegistry();
        r.register(new SubmitLeaveTool());
        r.register(new QueryLeaveStatusTool());
        return r;
    """
    registry = ToolRegistry()
    registry.register(SubmitLeaveTool())        # 提交请假(复合工具,3 步管线)
    registry.register(QueryLeaveStatusTool())   # 查询请假审批状态
    return registry


def create_prompt_loader():
    """请假申请包不需要自定义 prompt,返回 None。

    返回 None 是 pack 契约里明确允许的"我不提供 prompt"信号。
    系统的回退逻辑(见 domains/__init__.py 的 load_all_packs):
      1. 优先使用第一个提供 prompt_loader 的 pack(通常 njmind_form);
      2. 如果所有 pack 都返回 None,dispatcher 会使用内置的动态 prompt 生成
         —— 从 registry.all() 动态构建意图识别 prompt,无需外部模板文件。

    Returns:
        None —— 显式表示本 pack 不提供 PromptLoader。

    Note:
        即使返回 None 也不影响本 pack 工具的可用性 —— 工具自身在
        ToolResult 里携带说明,LLM 仍能正确识别和调用。

    【Java 类比】
    类似实现一个可选 SPI 接口时直接返回 null/Optional.empty():
    框架检测到空值后走默认回退逻辑,不会抛异常。
    """
    return None
