"""向后兼容转发:PromptLoader 已下沉 sdk/prompt_loader.py(插件与引擎
共用 pack 契约类型,物理归宿 SDK——engine → sdk 是合法依赖方向)。"""
from sdk.prompt_loader import PromptLoader, PromptOverrides  # noqa: F401
