"""StreamManager — SSE 流式桥接。

阶段 0:空壳。实际 SSE 仍由 api/sse.py 的 StreamManager 负责。
阶段 3-4 把 result payload 钩子化(调 tool.format_result)。
"""


class StreamManager:
    """阶段 0 占位。阶段 3 增强后委托给现有 api.sse.StreamManager。"""

    def __init__(self):
        pass
