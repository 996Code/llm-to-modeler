"""leave_application pack - 请假申请提交。

演示非配置类插件：artifact_type="data"，前端渲染 data-card。

工具:
  - submit_leave: 提交请假申请(CompositeTool, 3步管线)
  - query_leave_status: 查询请假审批状态(Tool)

插件化约定:
  - create_registry() 必须提供,返回 ToolRegistry
  - create_prompt_loader() 返回 None — 本 pack 不需要自定义 prompt,
    系统会使用第一个提供 prompt_loader 的 pack(如 njmind_form)
"""
from pathlib import Path

from sdk.registry import ToolRegistry
from domains.leave_application.tools.submit_leave import SubmitLeaveTool
from domains.leave_application.tools.query_status import QueryLeaveStatusTool


def create_registry() -> ToolRegistry:
    """创建并注册 leave_application pack 的工具。"""
    registry = ToolRegistry()
    registry.register(SubmitLeaveTool())
    registry.register(QueryLeaveStatusTool())
    return registry


def create_prompt_loader():
    """请假申请包不需要自定义 prompt，返回 None。

    系统会使用第一个提供 prompt_loader 的 pack（njmind_form）。
    如果没有任何 pack 提供 prompt_loader，dispatcher 会使用
    内置的动态 prompt 生成（从 registry.all() 动态构建意图识别 prompt）。
    """
    return None
