"""SDK 工具协议 — Tool/CompositeTool/数据类。

对标 Claude Code src/Tool.ts。设计原则:
- Fail-Closed 默认值:安全相关属性默认保守
- 安全声明与执行分离:validate_input 先于 execute
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional
from pydantic import BaseModel, Field, ConfigDict


class ToolContext(BaseModel):
    """工具执行时拿到的依赖(由 Engine 注入)。"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_client: Any              # LLMClient(chat / chat_json)
    asset_client: Any            # AssetClient
    conversation: Any            # ConversationStore
    emit: Callable[..., None]    # emit(event_type, message, **extra)
    forward_headers: dict = Field(default_factory=dict)


class AskOption(BaseModel):
    """追问选项。"""
    label: str
    description: str


class AskQuestion(BaseModel):
    """单个追问问题。"""
    question: str
    header: str                       # ≤12 字符,前端显示为 chip
    options: list[AskOption]          # 2-4 个(前端自动加"其他")
    multi_select: bool = False


class AskSpec(BaseModel):
    """追问规格。对标 CC AskUserQuestionTool。
    工具产出 ToolResult.ask → SSE 推前端 → 用户带 answers 重发 → dispatcher 重跑工具。"""
    questions: list[AskQuestion]


class ToolResult(BaseModel):
    """工具执行结果。三层设计:
    - artifact: 不透明制品,Engine 不读内部结构
    - summary: 标准化摘要,进 ConversationManager 历史
    - extra: 领域自由扩展,不进历史
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    artifact: Optional[dict] = None
    reply: Optional[str] = None
    ask: Optional[AskSpec] = None     # 非空 = 需要追问(C.2-A)
    summary: str = ""
    extra: dict = Field(default_factory=dict)
    error_for_llm: Optional[str] = None


class ClarificationRaised(Exception):
    """[向后兼容] 工具中途需要追问时抛出。
    v4 起统一改用 ToolResult.ask。新代码不应再抛此异常。"""
    def __init__(self, questions: list[str]):
        self.questions = questions


# 以下在本 Task 暂不实现(下个 Task),先占位避免 import 错
class Tool(ABC):
    """工具基类。下个 Task 实现。"""
    pass


class CompositeTool(Tool):
    """复合工具基类。下个 Task 实现。"""
    pass
