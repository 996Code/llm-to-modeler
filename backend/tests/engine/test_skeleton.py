"""Engine 骨架测试 — 验证空实现可实例化 + 架构试金石。"""
import subprocess
from pathlib import Path

from engine.dispatcher import ToolDispatcher
from engine.conversation import ConversationManager
from engine.stream import StreamManager


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

    def test_engine_has_no_domain_words(self):
        """架构试金石:engine/ 目录下不应有领域词汇。"""
        engine_dir = Path(__file__).resolve().parent.parent.parent / "src" / "engine"
        result = subprocess.run(
            ["grep", "-rE", "formCode|formFieldConfigVos|fieldTitle|TYPE_TO_TEMPLATE",
             str(engine_dir)],
            capture_output=True, text=True,
        )
        # 不应有任何输出(无领域词泄漏)
        assert result.stdout == "", (
            f"engine/ 含领域词汇:\n{result.stdout}"
        )
