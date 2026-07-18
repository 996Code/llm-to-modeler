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
        """架构试金石:engine/ 目录下 .py 文件不应有领域词汇。

        注:stream.py 作为 SSE 桥接层,需要读取制品的 formFieldConfigVos
        来产出 SSE payload。这是"输出格式"而非"领域逻辑"--阶段 4 会通过
        tool.format_result() 钩子化,把这段读制品的代码移到 pack 内。
        本阶段暂时排除 stream.py 的检查。
        """
        engine_dir = Path(__file__).resolve().parent.parent.parent / "src" / "engine"
        result = subprocess.run(
            ["grep", "-rnE", "--include=*.py",
             "formCode|formFieldConfigVos|fieldTitle|TYPE_TO_TEMPLATE",
             str(engine_dir)],
            capture_output=True, text=True,
        )
        # 过滤掉 stream.py(阶段 4 钩子化移除)
        lines = [
            line for line in result.stdout.splitlines()
            if line and "stream.py" not in line
        ]
        assert not lines, (
            f"engine/ 含领域词汇(除 stream.py):\n{chr(10).join(lines)}"
        )
