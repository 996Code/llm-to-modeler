"""阶段 0 冒烟测试 — 验证现有流程无回归。

本阶段只搭骨架,不引入新行为。以下 import 都必须成功,
证明 engine/sdk/domains 目录创建没有破坏现有代码。
"""
import pytest


def test_existing_graph_imports():
    """现有 LangGraph workflow 仍可 import。"""
    from src.graph.graph import FormConfigWorkflow
    assert FormConfigWorkflow is not None


def test_existing_store_imports():
    """现有 ConversationStore 仍可 import。"""
    from src.services.conversation_store import ConversationStore
    assert ConversationStore is not None


def test_existing_sse_imports():
    """现有 StreamManager 仍可 import。"""
    from src.api.sse import StreamManager as ExistingStreamManager
    assert ExistingStreamManager is not None


def test_existing_upstream_imports():
    """现有 UpstreamClient 仍可 import。"""
    from src.services.upstream_client import UpstreamClient
    assert UpstreamClient is not None


def test_existing_llm_client_imports():
    """现有 LLMClient 仍可 import。"""
    from src.llm.client import LLMClient
    assert LLMClient is not None


def test_new_sdk_imports():
    """新 SDK 模块可 import。"""
    from sdk.tool import Tool, CompositeTool, ToolResult, AskSpec, ToolContext
    from sdk.asset_client import AssetClient
    from sdk.registry import ToolRegistry
    assert all([Tool, CompositeTool, ToolResult, AskSpec, ToolContext,
                AssetClient, ToolRegistry])


def test_new_engine_imports():
    """新 Engine 模块可 import。"""
    from engine.dispatcher import ToolDispatcher
    from engine.conversation import ConversationManager
    from engine.stream import StreamManager
    assert all([ToolDispatcher, ConversationManager, StreamManager])
