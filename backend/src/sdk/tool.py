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
    """工具执行时拿到的依赖(由 Engine 注入)。

    属性说明:
    - llm_client: LLM 调用(chat / chat_json)
    - asset_client: 上游资产/数据操作抽象
    - conversation: ConversationStore
    - emit: SSE 事件回调
    - forward_headers: 嵌入模式透传的请求头
    - conv_id: 会话 ID
    - registry: 工具注册表(只读),供工具查询其他工具的能力描述
      例如 ChatTool 用它动态生成"我能做什么"的能力列表
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_client: Any              # LLMClient(chat / chat_json)
    asset_client: Any            # AssetClient
    conversation: Any            # ConversationStore
    emit: Callable[..., None]    # emit(event_type, message, **extra)
    forward_headers: dict = Field(default_factory=dict)
    conv_id: Optional[str] = None  # 会话 ID，用于日志记录
    registry: Any = None           # ToolRegistry(只读),供工具查询能力

    def trace(
        self,
        stage: str,
        title: str = "",
        status: str = "info",
        duration_ms: Optional[int] = None,
        detail: Optional[dict] = None,
    ) -> None:
        """写入一条链路追踪事件(events 表 kind=trace),供管理端链路视图展示。

        这是 pack 向会话链路贡献业务打点的官方 API——工具内部的关键步骤
        (调了哪个上游、校验结论、内部阶段切换)都可以入链,与引擎自动
        打点(意图路由/工具执行耗时/历史压缩)汇成同一条时间线。

        Args:
            stage:      环节标识,建议 "工具名.动作" 命名空间(如
                        "create_form.fetch_template"),避免跨工具撞名。
            title:      人类可读标题(管理端时间线展示),缺省用 stage。
            status:     "info" / "ok" / "error"(时间线着色用)。
            duration_ms: 该环节耗时(毫秒,可选)。
            detail:     结构化明细 dict(可选)。注意保持小体积——
                        trace 是高频审计流,别把完整制品塞进来。

        设计约束(Fail-Open):无会话上下文(MCP 单轮等)静默跳过;
        写入异常只记日志不上抛——追踪永远不能影响工具主流程。
        """
        import logging
        if not self.conversation or not self.conv_id:
            return
        try:
            # conversation 是 ConversationManager(见 nodes.py 注入),
            # append 即 store.append_event——append-only,不污染消息重放
            # (conversation.load 的 kind 分流会忽略 trace)
            self.conversation.append(self.conv_id, "trace", {
                "stage": stage,
                "title": title or stage,
                "status": status,
                "duration_ms": duration_ms,
                "detail": detail,
            })
        except Exception as e:
            logging.getLogger(__name__).warning(f"trace write failed ({stage}): {e}")


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
    工具产出 ToolResult.ask → SSE 推前端 → 用户带 answers 重发 → 引擎从 interrupt 断点重跑工具。"""
    questions: list[AskQuestion]


class ToolResult(BaseModel):
    """工具执行结果。三层设计:
    - artifact: 不透明制品,Engine 不读内部结构
    - artifact_type: 制品类型,决定 SSE 桥接和前端渲染方式
      - "config": 表单配置(存 config_snapshot,显示应用按钮)
      - "data": 数据结果(只存消息,不存 config,显示摘要卡片)
    - summary: 标准化摘要,进 ConversationManager 历史
    - extra: 领域自由扩展,不进历史
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    artifact: Optional[dict] = None
    artifact_type: str = "config"    # "config" | "data" — 向后兼容,默认 config
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

    def preflight(self, state: dict, ctx: ToolContext) -> Optional[ToolResult]:
        """执行前提校验——流程链路的标准环节(Engine 在 execute 前调用)。

        与 validate_input 的分工:
          - validate_input 校验「输入语义」(只看 state,如画布是否有内容);
          - preflight 校验「执行前提」(需要 ctx,如依赖的上游服务地址是否
            可解析、外部资源是否就绪)——这些条件不在会话状态里,在请求上下文里。

        返回 None = 通过,继续 execute;返回 ToolResult(error_for_llm) =
        拦截执行(fail-fast:错误直达用户,不烧 LLM/上游调用)。
        默认通过;业务工具按需覆写(引擎不感知校验内容,零领域知识)。"""
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
    
    # ── 插件化:Pipeline 步骤定义(用于前端动态渲染) ──
    # 格式: [{"key": "step_name", "label": "步骤描述"}, ...]
    pipeline_steps: list[dict] = []

    def run_pipeline(self, state: dict, ctx: ToolContext) -> None:
        """顺序执行 steps,支持中途中断(追问/错误)。

        step 可通过设置 state["_need_clarify"]=True 中断后续步骤,
        execute 方法检查此标记后返回 ToolResult.ask 而非继续执行。
        每个 step 内部自行 emit stage 事件(含详细描述)。
        """
        # 发送 pipeline 定义给前端
        if self.pipeline_steps:
            ctx.emit("pipeline_definition", {
                "tool": self.name,
                "steps": self.pipeline_steps
            })
        
        for step_name in self.steps:
            # 检查前序 step 是否请求中断
            if state.get("_need_clarify"):
                break
            method = getattr(self, f"_step_{step_name}")
            method(state, ctx)
