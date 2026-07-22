"""Engine 骨架测试 — 验证空实现可实例化 + 架构试金石。"""
import subprocess
from pathlib import Path

import pytest

from engine.dispatcher import ToolDispatcher
from engine.conversation import ConversationManager
from api.sse import StreamManager


def _langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False


class TestEngineSkeleton:
    def test_dispatcher_instantiable(self):
        d = ToolDispatcher.__new__(ToolDispatcher)  # 不触发 __init__(需要依赖)
        assert d is not None

    def test_conversation_manager_instantiable(self):
        cm = ConversationManager.__new__(ConversationManager)
        assert cm is not None

    def test_stream_manager_instantiable(self):
        sm = StreamManager.__new__(StreamManager)
        assert sm is not None

    @pytest.mark.skipif(
        not _langgraph_available(),
        reason="langgraph not installed",
    )
    def test_graph_module_importable(self):
        """LangGraph 迁移后 engine.graph 可导入。"""
        from engine.graph import build_graph
        assert callable(build_graph)

    def test_compression_module_importable(self):
        """engine.compression 共享模块可导入。"""
        from engine.compression import build_compressed_history
        assert callable(build_compressed_history)

    def test_engine_has_no_domain_words(self):
        """架构试金石:engine/ 目录下 .py 文件不应有领域词汇。

        阶段 3 修复:stream.py 已通过 tool.format_result() 钩子化,
        不再直接读 formFieldConfigVos/formCode/formName。
        """
        engine_dir = Path(__file__).resolve().parent.parent.parent / "src" / "engine"
        result = subprocess.run(
            ["grep", "-rnE", "--include=*.py",
             "formCode|formFieldConfigVos|fieldTitle|TYPE_TO_TEMPLATE",
             str(engine_dir)],
            capture_output=True, text=True,
        )
        assert result.stdout == "", (
            f"engine/ 含领域词汇:\n{result.stdout}"
        )
