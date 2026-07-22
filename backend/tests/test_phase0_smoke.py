"""阶段 0 冒烟测试 - 验证新架构模块可 import。

阶段 4:旧 LangGraph 代码已删除,只验证新架构模块。
"""
import pytest


def _module_available(import_path: str) -> bool:
    try:
        parts = import_path.rsplit(".", 1)
        if len(parts) == 2:
            mod = __import__(parts[0], fromlist=[parts[1]])
            getattr(mod, parts[1])
        else:
            __import__(import_path)
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def test_existing_store_imports():
    """ConversationStore 仍可 import(append-only 重建后)。"""
    from src.services.conversation_store import ConversationStore
    assert ConversationStore is not None


def test_existing_sse_imports():
    """SSEEvent/StreamManager 仍可 import。"""
    from src.api.sse import StreamManager as ExistingStreamManager
    assert ExistingStreamManager is not None


def test_existing_upstream_imports():
    """UpstreamClient 仍可 import。"""
    from src.services.upstream_client import UpstreamClient
    assert UpstreamClient is not None


@pytest.mark.skipif(
    not _module_available("openai"),
    reason="openai package not installed",
)
def test_existing_llm_client_imports():
    """LLMClient 仍可 import。"""
    from src.llm.client import LLMClient
    assert LLMClient is not None


def test_new_sdk_imports():
    """新 SDK 模块可 import。"""
    from sdk.tool import Tool, CompositeTool, ToolResult, AskSpec, ToolContext
    from sdk.asset_client import AssetClient
    from sdk.registry import ToolRegistry
    from sdk.sanitize import sanitize_text, sanitize_obj
    assert all([Tool, CompositeTool, ToolResult, AskSpec, ToolContext,
                AssetClient, ToolRegistry, sanitize_text, sanitize_obj])


@pytest.mark.skipif(
    not _module_available("langgraph"),
    reason="langgraph package not installed",
)
def test_new_engine_imports():
    """新 Engine 模块可 import(含 langgraph 依赖的 stream_graph)。"""
    from engine.dispatcher import ToolDispatcher
    from engine.conversation import ConversationManager
    from engine.stream import stream_graph
    from engine.prompt_loader import PromptLoader
    from engine.compression import CompressionSidechain
    from engine.logging_filter import RedactFilter
    assert all([ToolDispatcher, ConversationManager, stream_graph,
                PromptLoader, CompressionSidechain, RedactFilter])


def test_new_engine_imports_no_langgraph():
    """不依赖 langgraph 的 Engine 模块仍可 import。"""
    from engine.dispatcher import ToolDispatcher
    from engine.conversation import ConversationManager
    from engine.prompt_loader import PromptLoader
    from engine.compression import CompressionSidechain, build_compressed_history
    from engine.logging_filter import RedactFilter
    assert all([ToolDispatcher, ConversationManager,
                PromptLoader, CompressionSidechain, build_compressed_history,
                RedactFilter])


def test_new_adapters_imports():
    """新 Adapters 模块可 import。"""
    from adapters.http_asset_client import HttpAssetClient
    assert HttpAssetClient is not None


def test_new_pack_imports():
    """njmind_form pack 可 import。"""
    from domains.njmind_form.pack import create_registry, create_prompt_loader
    from domains.njmind_form.tools.create_form import CreateFormTool
    from domains.njmind_form.tools.modify_form import ModifyFormTool
    from domains.njmind_form.tools.chat import ChatTool
    from domains.njmind_form.tools.get_form import GetFormTool
    from domains.njmind_form.tools.clone_form import CloneFormTool
    from domains.njmind_form.tools.image_form import ImageFormTool
    assert all([create_registry, create_prompt_loader,
                CreateFormTool, ModifyFormTool, ChatTool,
                GetFormTool, CloneFormTool, ImageFormTool])
