"""njmind_form pack - 工具注册入口。

pack 启动时注册 3 个工具到 ToolRegistry。
"""
from pathlib import Path

from sdk.registry import ToolRegistry
from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.tools.modify_form import ModifyFormTool
from domains.njmind_form.tools.chat import ChatTool


def create_registry() -> ToolRegistry:
    """创建并注册 njmind_form pack 的 3 个工具。"""
    registry = ToolRegistry()
    registry.register(CreateFormTool())
    registry.register(ModifyFormTool())
    registry.register(ChatTool())
    return registry


def create_prompt_loader():
    """创建 PromptLoader,指向 domains 目录。"""
    from engine.prompt_loader import PromptLoader
    # packs_root = backend/src/domains
    domains_dir = Path(__file__).resolve().parent.parent
    return PromptLoader(packs_root=domains_dir)
