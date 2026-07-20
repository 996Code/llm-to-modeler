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
    conv_id: Optional[str] = None  # 会话 ID，用于日志记录


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


class Tool(ABC):
    """工具基类。对标 Claude Code 的 Tool 协议(src/Tool.ts)。

    设计原则(借鉴 CC):
    - Fail-Closed 默认值:安全相关属性默认保守,避免误用
    - 安全声明与执行分离:check_permissions / validate_input 先于 execute
    """
    name: str                     # 工具名,LLM 选择时看到
    description: str              # 工具说明
    when: str                     # "何时用"短描述(填进选择 prompt)

    # ── 安全声明(Fail-Closed,借鉴 CC Tool.ts:757)──
    is_destructive: bool = True   # 默认破坏性,需 pack 显式声明 False 才认为安全
    is_read_only: bool = False
    # ── 并发安全声明(C.2-B:借鉴 CC isConcurrencySafe)──
    is_concurrency_safe: bool = False

    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema,描述这个工具需要的参数(从 state 抽取)。"""

    @abstractmethod
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具。可中途 emit progress,可抛 ClarificationRaised(兼容)。"""

    def validate_input(self, state: dict) -> Optional[str]:
        """语义校验(比 JSON Schema 更严格)。返回错误文本或 None。
        默认 None=通过。Engine 在 execute 前调用,失败则跳过 execute、
        把错误写进 ToolResult.error_for_llm 回流给下一轮选择。"""
        return None

    def requires_follow_up(self, result: ToolResult) -> bool:
        """工具执行后是否需要 Engine 再做一轮选择。默认 False。
        未来引入 Agent Loop 时,工具可声明"我做完但还需要继续判断"。"""
        return False

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿文本。默认空。"""
        return ""

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从制品生成标题。默认空。"""
        return ""

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从制品提取前端需要的字段(如字段数、名称等)。
        Engine 调用此方法,避免直接读制品内部结构(架构试金石)。
        默认返回空 dict,pack 按需覆写。"""
        return {}


class CompositeTool(Tool):
    """复合工具基类:内部有多步 pipeline。对标 CC 的 Skill——
    "封装一个工作流 + 声明触发条件"。

    run_pipeline 顺序执行 steps:
    - 每个 step 对应 _step_<name>(state, ctx) 方法
    - step 内可抛 ClarificationRaised → 立即上抛,Engine 转成 SSE
    - step 内可重跑前序 step 实现 retry(如 validate 失败重跑 generate)
    - 每个 step 自行 emit stage 事件(含详细描述)
    """
    steps: list[str] = []

    def run_pipeline(self, state: dict, ctx: ToolContext) -> None:
        for step_name in self.steps:
            method = getattr(self, f"_step_{step_name}")
            method(state, ctx)
