"""njmind_list pack - 列表页配置领域的工具注册入口。

domains/__init__.py 的 load_pack("njmind_list") import 本模块后调用
create_registry()/create_prompt_loader()。
"""
from pathlib import Path

from sdk.registry import ToolRegistry
from domains.njmind_list.router import NjmindListRouter
from domains.njmind_list.tools.chat import ChatTool
from domains.njmind_list.tools.generate_list import GenerateListTool


def create_registry() -> ToolRegistry:
    # ChatTool 排最前:无 LLM 路由降级时 DefaultPackRouter 回退到首个
    # 注册工具(SDK 契约)——兜底回闲聊工具,不误触发生成管线。
    registry = ToolRegistry()
    registry.register(ChatTool())
    registry.register(GenerateListTool())
    return registry


def create_prompt_loader():
    from sdk.prompt_loader import PromptLoader
    domains_dir = Path(__file__).resolve().parent.parent
    return PromptLoader(packs_root=domains_dir)


def create_router(registry: ToolRegistry = None):
    if registry is None:
        registry = create_registry()
    return NjmindListRouter(registry)


def enhance_asset_client(asset_client, upstream):
    """既有装配钩子(pack_manager 冷启动/热切换都会调):注入通用传输。

    列表配置的 guide/templates/validate 是 pack 专有端点,不进通用
    adapter(保持 HttpAssetClient 零领域知识);此处只取 upstream 参数做
    transport 注入,asset_client 参数忽略。main.py 因此零改动。
    """
    from domains.njmind_list.service_locator import wire_transport as _wire
    _wire(upstream)
