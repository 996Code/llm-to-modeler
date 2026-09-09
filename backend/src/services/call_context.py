"""向后兼容转发:请求级调用上下文已下沉 sdk/call_context.py
(纯 thread-local 原语,零平台依赖,插件可直接使用)。"""
from sdk.call_context import (  # noqa: F401
    bind_conversation, clear_conversation, current_conversation_id)
