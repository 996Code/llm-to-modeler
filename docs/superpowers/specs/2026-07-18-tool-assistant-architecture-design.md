# 工具助手架构改造设计

**状态**:草案待评审(v3 — 两轮多视角重读 claude-code-analysis,见附录 C/D)
**日期**:2026-07-18
**作者**:架构梳理

## 一、定位重述

### 1.1 本质

这个项目是一个 **LLM 驱动的工具助手**:它听懂用户想做什么,调用上层系统的能力来完成,把结果流式返回给用户。

```
用户 ──► [工具助手: 听懂 + 调用] ──► [上层系统的能力]
              ↑                          ↑
          通用层(本项目)            领域层(njmind modeler)
```

工具助手的本质原则:**助手不该知道工具内部怎么工作**。

### 1.2 现状的问题

当前代码把"上层系统的能力"**内化**了。表单怎么生成、字段怎么解析、模板怎么取,全写死在 engine 里。`formFieldConfigVos` 这个字段名散落在 6 个文件——这是"领域知识没有唯一所有者"的症状。

具体定制化热点(盘点结果):

| # | 位置 | 内容 | 性质 |
|---|------|------|------|
| 1 | `nodes.py` `TYPE_TO_TEMPLATE` / `TYPE_NAMES` | 字段类型→模板映射 | njmind 专属 |
| 2 | `prompt_builder.py` 4 套 prompt + `FIELD_TYPE_TABLE` | njmind 文案 | njmind 专属 |
| 3 | `state.py` `ParsedField` + `form_*` 字段 | njmind schema 进 state | njmind 专属 |
| 4 | `upstream_client.py` URL 路径表 | REST 契约写死 | njmind 专属 |
| 5 | `graph.py` 节点名 + CREATE/MODIFY 管线语义 | 管线拓扑绑定表单 | njmind 专属 |
| 6 | `compressor.py` `_COMPACT_PROMPT` + 状态提取 | 压缩内容绑定表单 | 半通用半专属 |
| 7 | `sse.py` 结果 payload 字段 + 摘要文案 | SSE 内容绑定表单 | 半通用半专属 |
| 8 | `config.py` 路由/请求体/forward 白名单 | 入口契约写死 | 半通用半专属 |
| 9 | `general_reply_node` system prompt + 意图标签 | 意图语义绑定表单 | njmind 专属 |
| 10 | `mcp_server.py` 工具集 + `njmind://` URI | 对外品牌 | njmind 专属 |

**通用骨架**(管线拓扑、SSE 桥接、压缩机制、LLM client、对话存储、header 透传)被这些 njmind 代码污染。

### 1.3 关键技术事实

上游 njmind-modeler 暴露的是 **REST API**(路径风格 `/api/mcp/...` 只是命名,不是 MCP 协议)。本项目对外提供 MCP 接口,内部通过 HTTP 桥接到上游 REST。

这意味着:任何绑 MCP 的方案都不可行(要么改上游,要么自己造 MCP 适配层)。

## 二、设计目标

### 2.1 核心目标

把当前"管线框架"重构为"工具助手 + 可插拔工具包",让 **njmind 业务知识完全收口到一个工具包内**,Engine 零领域知识。

### 2.2 验证标准(架构试金石)

> `engine/` 目录下不应出现 `form`、`formCode`、`template`、`field` 这些领域词汇。

任何 njmind 词汇出现在 engine 里,就是抽象泄漏。

### 2.3 非目标

- ❌ 追求"任何领域零代码接入"(那是纯配置方案,我们已经否决)
- ❌ 多步 Agent Loop(成本高、不可控)
- ❌ 接 MCP 生态(上游不是 MCP)
- ❌ 大爆炸式重写(用绞杀者模式渐进迁移)

## 三、架构总览

### 3.1 三层 + 依赖反转(六边形架构)

```
┌──────────────────────────────────────────────────┐
│  API Layer(FastAPI,通用路由)                   │
│  POST /api/chat → Engine                         │
└──────────────────────┬───────────────────────────┘
                       │ depends on
                       ▼
┌──────────────────────────────────────────────────┐
│  Engine(工具助手内核)                           │
│  - ConversationManager:多轮 / 压缩 / 存储        │
│  - ToolDispatcher:LLM 选工具 + 执行              │
│  - StreamBridge:SSE 流式                         │
│  - HeaderForwarder:thread-local 透传             │
│  ─── 零领域知识 ───                              │
└──────────────────────┬───────────────────────────┘
                       │ depends on(抽象)
                       ▼
┌──────────────────────────────────────────────────┐
│  SDK(契约层 / 端口)                             │
│  Tool / CompositeTool / SimpleTool (ABC)         │
│  ToolContext / ToolResult / ClarificationRaised  │
│  AssetClient (ABC)                               │
│  Artifact(BaseModel,不透明)                     │
└──────────────────────▲───────────────────────────┘
                       │ implements
┌──────────────────────┴───────────────────────────┐
│  Tool Packs(领域工具包)                         │
│  ┌─────────────────────────────┐                 │
│  │ njmind_form/                │                 │
│  │  - CreateFormTool(复合)    │                 │
│  │  - ModifyFormTool(复合)    │                 │
│  │  - ChatTool(简单)          │                 │
│  │  - prompts/*.j2             │                 │
│  │  - adapters/upstream.py     │                 │
│  │  - models.py(FormConfig)    │                 │
│  └─────────────────────────────┘                 │
└──────────────────────────────────────────────────┘
```

**依赖方向永远向内**(依赖反转原则)。Engine 依赖 SDK 的抽象,**绝不 import 任何具体 pack**。换上层系统 = 换 pack,Engine 一行不改。

### 3.2 与现状的对应关系

| 当前 | 改造后 | 说明 |
|------|--------|------|
| 6 步 CREATE 管线 | `CreateFormTool.execute()` 内部 | 管线活在工具内部 |
| 3 步 MODIFY 管线 | `ModifyFormTool.execute()` 内部 | 同上 |
| `classify_intent_node` | `ToolDispatcher._select_tools()` | LLM 从注册表选 1..N 个工具 |
| `general_reply_node` | `ChatTool.execute()` | 闲聊也是工具 |
| `upstream_client.py` | `njmind_form/adapters/upstream.py` | 收口进 pack |
| `prompt_builder.py` | `njmind_form/prompts/*.j2` | 文案进 pack |
| `compressor.py` 机制 | `engine/compress/` | 机制留下 |
| `compressor.py` 内容 | pack 提供 `summarize_artifact()` | 内容出去 |
| `sse.py` StreamBridge | `engine/stream/` | 全部留下 |
| `sse.py` 结果 payload | pack 提供 `format_result()` | 内容出去 |

## 四、核心契约:SDK 层

### 4.1 工具协议

```python
# sdk/tool.py
from abc import ABC, abstractmethod
from typing import Any, Optional
from pydantic import BaseModel

class ToolContext(BaseModel):
    """工具执行时拿到的依赖与上下文(由 Engine 注入)。"""
    llm_client: Any              # LLMClient(chat / chat_json)
    asset_client: Any            # AssetClient(取模板/schema/guide)
    conversation: Any            # ConversationStore(读写历史)
    emit: Any                    # Callable[[stage, message, **extra], None]
    forward_headers: dict        # 转发到上游的请求头

class ToolResult(BaseModel):
    """工具执行结果。

    三层设计(对标 CC task-notification,附录 D.1 #4):
    - artifact: 不透明制品,Engine 从不读内部结构,只做传递/存储/让 pack 格式化
    - summary: 标准化摘要,进 ConversationManager 历史,是压缩器处理的唯一对象
    - extra: 领域自由扩展,不进历史(运行时噪声)

    固化规则:CompositeTool 中间 step 产出(parse_fields、fetched_templates 等)
    只活在 state 内,绝不进 ConversationManager——只有最终 summary 入历史。
    这保证压缩器处理的永远是标准化 summary,不是工具内部噪声。
    """
    artifact: Optional[dict] = None     # 不透明制品(Engine 不读内部)
    reply: Optional[str] = None         # 给用户的文本回复
    # ── 追问(C.2-A:Clarification 建模为内置 AskTool,而非异常短路)──
    # 工具执行中若发现信息不足,产出 ask 而非抛异常。Engine 把 ask 转成 SSE,
    # 用户回答后 Engine 带着答案重跑同一工具(answers 进 state["clarify_answers"])。
    # 比异常短路更强:追问的问答都进对话历史,下一轮 LLM 选择也能感知。
    ask: Optional["AskSpec"] = None     # 非空 = 需要追问用户
    summary: str = ""                   # 用于对话历史与压缩(标准化、无运行时噪声)
    extra: dict = {}                    # 领域自由扩展(不进对话历史)
    error_for_llm: Optional[str] = None # 失败时给下一轮 LLM 的标准化错误文本(对标 CC 的错误回流)

class AskSpec(BaseModel):
    """追问规格(C.2-A)。对标 CC 的 AskUserQuestionTool。
    Engine 把它转成 SSE result 事件(type=ask),前端展示追问 UI。
    用户回答后,Engine 把 answers 写进 state["clarify_answers"],重跑工具。"""
    questions: list["AskQuestion"]      # 1-4 个问题(前端限制)

class AskQuestion(BaseModel):
    question: str                       # 完整问题文本
    header: str                         # 短标签(≤12 字符,前端显示为 chip)
    options: list["AskOption"]          # 2-4 个选项(前端会自动加"其他")
    multi_select: bool = False          # 是否允许多选

class AskOption(BaseModel):
    label: str                          # 选项显示文本(1-5 字)
    description: str                    # 选项说明

# 前向引用解析(Pydantic v2 需要 model_rebuild)
AskSpec.model_rebuild()

class ClarificationRaised(Exception):
    """[已废弃,保留向后兼容] 工具中途需要追问时抛出。
    v4 起(C.2-A)统一改用 ToolResult.ask。新代码不应再抛此异常。
    保留定义是为了让迁移期间未改造的工具仍能工作(Engine 兼容捕获)。"""
    def __init__(self, questions: list[str]):
        self.questions = questions

class Tool(ABC):
    """工具基类。对标 Claude Code 的 Tool 协议(src/Tool.ts)。

    设计原则(借鉴 CC):
    - Fail-Closed 默认值:安全相关属性默认保守,避免误用
    - 安全声明与执行分离:check_permissions / validate_input 先于 execute
    """
    name: str                     # 工具名,LLM 选择时看到
    description: str              # 工具说明,LLM 选择时看到
    when: str                     # 简短描述"何时用"(填进选择 prompt)

    # ── 安全声明(Fail-Closed,借鉴 CC Tool.ts:757)──
    is_destructive: bool = True   # 默认破坏性,需 pack 显式声明 False 才认为安全
    is_read_only: bool = False    # 默认非只读
    # ── 并发安全声明(C.2-B:Tool 并行执行,借鉴 CC isConcurrencySafe)──
    # Fail-Closed:默认不可并发(保守)。Engine 的 partition_tool_calls 按此分批:
    # 连续的 concurrency_safe=True 工具并发执行,遇到 False 则串行。
    # 只读 + 幂等的工具(如 fetch_guide、list_assets)应声明 True 以提速。
    is_concurrency_safe: bool = False  # 默认串行,pack 显式声明才并发

    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema,描述这个工具需要的参数(从 state 抽取)。"""

    @abstractmethod
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具。可中途 emit progress,可抛 ClarificationRaised。"""

    # ── 执行前钩子(借鉴 CC 六段 pipeline 的前半部分)──
    def validate_input(self, state: dict) -> Optional[str]:
        """语义校验(比 JSON Schema 更严格)。返回错误文本或 None。
        默认 None=通过。Engine 在 execute 前调用,失败则跳过 execute、
        把错误写进 ToolResult.error_for_llm 回流给下一轮选择。"""
        return None

    # ── 多步预留(借鉴 CC requires_follow_up,当前单步,不焊死)──
    def requires_follow_up(self, result: ToolResult) -> bool:
        """工具执行后是否需要 Engine 再做一轮选择。默认 False。
        未来引入 Agent Loop 时,工具可声明"我做完但还需要继续判断"。"""
        return False

    # ── 可选 hooks(默认实现,pack 按需覆写)──
    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿文本。默认空。"""
        return ""

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从制品生成标题。默认空。"""
        return ""

class CompositeTool(Tool):
    """复合工具基类:内部有多步 pipeline 的工具。

    概念对标(借鉴 CC):这等价于 Claude Code 的 Skill——
    "封装一个工作流 + 声明触发条件"。CC 区分 Tool/Skill/Command,
    我们用 SimpleTool / CompositeTool 二分表达同样的分层。
    CompositeTool.when 字段可升级为结构化 trigger(关键词/状态谓词)
    以提升 _select_tools 的准确率。
    """
    steps: list[str] = []

    def run_pipeline(self, state, ctx):
        """顺序执行 steps。约定:
        - 每个 step 对应 _step_<name>(state, ctx) 方法
        - step 内可抛 ClarificationRaised → run_pipeline 立即上抛,Engine 转成 SSE
        - step 内可重跑前序 step 实现 retry(如 validate 失败重跑 generate)
        - 每个 step 自动 emit 一个 stage 事件,工具内部再细化 emit 子进度
        """
        for step_name in self.steps:
            ctx.emit("stage", step_name)
            method = getattr(self, f"_step_{step_name}")
            method(state, ctx)
```

**retry 的位置约定**:retry 发生在**工具内部**,Engine 不感知。例如 `CreateFormTool._step_validate` 校验失败时,自己重跑 `_step_generate`,达到 max_retries 仍失败则把 `validation_errors` 写进 ToolResult.extra,Engine 照常 emit result。这样 Engine 的 `run` 逻辑保持简单——只走"选→执行→保存"三步。

### 4.2 资产客户端协议

```python
# sdk/asset_client.py
from abc import ABC, abstractmethod

class AssetClient(ABC):
    """资产来源的抽象。pack 用它取模板/schema/guide,不关心是 HTTP 还是本地。

    安全约定(附录 D.2):所有 get_* 方法返回的内容在进入 prompt 前
    必须经过 Unicode 清洗(sdk.sanitize.sanitize_obj),防止上游数据
    携带零宽字符/方向反转字符等隐写指令。HttpAssetClient 实现应在
    返回前自动调用 sanitize_obj。
    """

    @abstractmethod
    def get_template(self, name: str) -> dict: ...

    @abstractmethod
    def list_templates(self) -> list[str]: ...

    @abstractmethod
    def get_schema(self, name: str) -> dict: ...

    @abstractmethod
    def get_guide(self) -> dict: ...

    @abstractmethod
    def validate_artifact(self, artifact: dict, mode: str) -> dict: ...
    # mode = "create" | "update",返回 {valid, errors, warnings}

    @abstractmethod
    def persist_artifact(self, artifact: dict, mode: str) -> dict: ...
    # create/update 到上游
```

**通用实现**(`adapters/http_asset_client.py`):配置一个 `base_url + path_map`,发 HTTP。

```python
# njmind_form 的路径表(进 pack 的 config)
NJMIND_PATHS = {
    "templates_list": "/api/mcp/templates/list-templates",
    "template":       "/api/mcp/templates/{name}",
    "schemas_list":   "/api/mcp/schemas/list-schemas",
    "schema":         "/api/mcp/schemas/{name}",
    "guide":          "/api/mcp/guides/guide.json",
    "validate":       "/api/mcp/forms/validate",
    "create":         "/api/mcp/forms/create",
    "update":         "/api/mcp/forms/{code}/update",
    "get":            "/api/mcp/forms/{code}",
}
```

未来接其他上层系统:写新的 `AssetClient` 实现(REST / gRPC / 本地文件)。

### 4.3 工具注册表

```python
# sdk/registry.py
class ToolRegistry:
    def register(self, tool: Tool): ...
    def all(self) -> list[Tool]: ...
    def get(self, name: str) -> Tool: ...

    def describe_for_llm(self, state: dict) -> str:
        """生成给 LLM 看的工具清单:
        - tool.name / description / when
        - 当前 state 下哪些工具可用(如无 artifact 时禁用 modify)
        """
```

## 五、Engine 核心

### 5.1 ToolDispatcher:单轮多工具选择(v4)

```python
# engine/dispatcher.py
class ToolDispatcher:
    """单轮多工具选择与执行(v4)。
    一次 LLM 调用选 1..N 个工具,按并发安全性分批执行。不循环。
    """

    def run(self, user_input: str, conv_id: str, ctx_extra: dict, answers: dict = None):
        # 1. 加载对话状态(append-only 读取,见 5.2)
        state = self._load_state(conv_id)
        state["user_input"] = user_input

        # 1b. C.2-A:追问恢复——如果 state 里有 pending_ask 且本次请求带了 answers,
        #     直接重跑上次挂起的工具,不再走选工具流程
        if state.get("pending_ask") and answers is not None:
            yield from self._resume_ask(state, conv_id, ctx_extra, answers)
            return

        # 2. 压缩检查(机制归 Engine,状态补偿内容归 tool)
        state = self._maybe_compress(state)

        # 3. LLM 选工具(单步选择,但可返回 1..N 个工具——C.2-B 支持并行)
        #    LLM 只返回工具名列表(参数从 state 抽取,不让 LLM 填)。
        tools = self._select_tools(user_input, state)
        if not tools:
            tools = [self._fallback_tool()]  # 兜底:走 ChatTool

        # 4. 分批调度(C.2-B:借鉴 CC partitionToolCalls)
        #    连续的 is_concurrency_safe=True 工具并发,遇到 False 则串行。
        #    单工具时退化为普通执行。
        for batch in self._partition_tool_calls(tools):
            if len(batch) == 1:
                yield from self._run_single(batch[0], state, conv_id, ctx_extra)
            else:
                yield from self._run_concurrent(batch, state, conv_id, ctx_extra)

    def _run_single(self, tool, state, conv_id, ctx_extra):
        """执行单个工具,含追问重跑(C.2-A)。

        追问的 SSE 语义:SSE 是单向推送,不能在同一个连接里等用户回答。
        所以追问作为本次响应的结束;用户答案通过【新的 /api/chat 请求】送回,
        Engine 在新请求 load_state 时检测到 state["pending_ask"],直接重跑同一工具。
        """
        # 追问重跑上限:防止工具反复追问死循环(默认 3 轮)
        clarify_round = state.get("clarify_round", 0)

        # 4a. 执行拦截层(借鉴 CC 六段 pipeline 前段):语义校验先于 execute
        err = tool.validate_input(state)
        if err is not None:
            result = ToolResult(error_for_llm=err, summary=f"输入校验失败: {err}")
            yield from self._emit_result(tool, result)
            self._save_state(conv_id, state, result)
            return

        # 4b. 执行工具
        try:
            result = tool.execute(state, self._build_ctx(state, ctx_extra))
        except ClarificationRaised as e:
            # 向后兼容:旧式异常 → 转成 ToolResult.ask
            result = ToolResult(ask=AskSpec(questions=[
                AskQuestion(question=q, header="追问", options=[]) for q in e.questions]))
        except Exception as e:
            # 失败回流(借鉴 CC 错误回流):异常包装成 error_for_llm
            result = ToolResult(error_for_llm=str(e), summary=f"工具执行失败: {e}")

        # 4c. 按 ToolResult 状态分流
        if result.ask is not None:
            clarify_round += 1
            if clarify_round > self._max_clarify_rounds:
                yield from self._emit_error("追问轮数超限,请重新描述需求")
                self._clear_pending_ask(state)
                return
            # C.2-A:保存追问现场到 state,emit ask 后结束本次响应
            state["pending_ask"] = {
                "tool": tool.name,
                "ask": result.ask.model_dump(),
                "round": clarify_round,
            }
            self._save_state(conv_id, state, result=None)
            yield from self._emit_ask(result.ask)
        else:
            self._clear_pending_ask(state)
            yield from self._emit_result(tool, result)
            self._save_state(conv_id, state, result)

    def _resume_ask(self, state, conv_id, ctx_extra, answers):
        """当 load_state 检测到 pending_ask 时,ToolDispatcher.run 直接走这里。
        answers 是用户对上次追问的回答,写进 state["clarify_answers"]后重跑工具。"""
        tool = self.registry.get(state["pending_ask"]["tool"])
        state["clarify_answers"] = answers
        yield from self._run_single(tool, state, conv_id, ctx_extra)

    def _run_concurrent(self, tools, state, conv_id, ctx_extra):
        """并发执行一批 concurrency_safe 工具(C.2-B)。
        各工具的 emit 用工具名前缀区分(stage 事件带 tool 字段)。
        context 修改延迟到批次全部完成后统一应用(借鉴 CC 延迟 contextModifier)。
        """
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tools)) as pool:
            futures = {pool.submit(self._exec_silent, t, state, ctx_extra): t for t in tools}
            for fut in concurrent.futures.as_completed(futures):
                tool = futures[fut]
                result = fut.result()
                yield from self._emit_result(tool, result)
        # 批次完成后统一 save(借鉴 CC 批量 apply,避免并发写覆盖)
        self._save_state(conv_id, state, None)

    def _select_tools(self, user_input, state) -> list[Tool]:
        """调一次 LLM,从 registry 选 1..N 个工具。
        Prompt = 工具清单 + 对话历史 + 用户输入。
        LLM 返回工具名列表(可单个可多个,如"加字段A 和 删字段B"→ 2 个 modify)。
        参数从 state 抽取,不让 LLM 填(这是"单步选择"与 Function Calling 的区别)。"""
        tools_desc = self.registry.describe_for_llm(state)
        # 具体实现细节见后续实现计划文档。
        ...

    def _partition_tool_calls(self, tools) -> list[list[Tool]]:
        """借鉴 CC toolOrchestration.ts 的 partitionToolCalls:
        连续的 is_concurrency_safe=True 工具归一批并发,False 的单独成批串行。
        例:[A(safe), B(safe), C(unsafe), D(safe)] → [[A,B], [C], [D]]
        """
        batches, current = [], []
        for tool in tools:
            if tool.is_concurrency_safe:
                current.append(tool)
            else:
                if current:
                    batches.append(current); current = []
                batches.append([tool])
        if current:
            batches.append(current)
        return batches

    def _maybe_compress(self, state):
        if self._conversation.should_compress(state):
            state["compressed_history"] = self._conversation.compress(state)
        return state
```

**关键**:`_select_tools` 内部只问一次 LLM,返回 1..N 个 Tool 实例(单轮多工具,不进入多步 loop)。

**retry / clarify / pipeline 都在工具内部,Engine 不感知**:
- retry:工具的 `_step_*` 方法自己决定要不要重跑(见 6.2 的 `_step_validate`)
- clarify:工具抛 `ClarificationRaised`,Engine 转成 SSE,工具间不共享这个状态
- pipeline:`CompositeTool.run_pipeline` 编排步骤,工具自治

### 5.2 ConversationManager

封装多轮 / 压缩 / 存储。**借鉴 Claude Code**(附录 C):

```python
# engine/conversation.py
class ConversationManager:
    # ── 存储:append-only 事件流(对标 CC sessionStorage.ts 的 JSONL 模型)──
    def append(self, conv_id, kind: str, payload: dict):
        """只追加,不覆盖。kind ∈ {user, assistant, tool_result, compacted,
        compact_trace, checkpoint, ask}。
        崩溃恢复只需重放尾部,压缩也不删旧行而是写一条 compacted 条目。"""

    def load(self, conv_id) -> dict:
        """读取并重建状态。按 kind 分流:
        - user/assistant/tool_result 按序重建 messages
        - compacted 标记压缩点,其后为 keep-recent,其前为已压缩
        - compact_trace 压缩轨迹(审计用,不进 messages)
        - checkpoint 用于持久化 artifact 快照、active_tool 等
        - ask 持久化 pending_ask 现场(C.2-A 追问恢复)
        """

    def save(self, conv_id, state, result):
        """append 用户输入 + 工具产出(summary 标准化后)。
        ToolResult.summary 入历史,ToolResult.extra 不入(借鉴 CC normalizeMessagesForAPI 的噪声剔除)。
        artifact 写 checkpoint 条目(不进 messages,避免膨胀)。"""

    def list_meta(self) -> list[SessionMeta]:
        """列表页只读 SessionMeta(title/summary/updated_at),
        不 JOIN messages(对标 CC lite reader 只读头尾 64KB)。"""

    # ── 压缩:三级保护 + sidechain 隔离(对标 CC compact.ts + autoCompact.ts)──
    def should_compress(self, state) -> bool:
        """token > 有效窗口的 70%。有效窗口 = 总窗口 - 预留 Summary Token。"""

    def compress(self, state, tool: Tool) -> str:
        """C.2-D:压缩在 forked sidechain 执行,不阻塞主对话流(对标 CC Forked Agent)。

        sidechain 的含义:
        1. 压缩是重操作(调 LLM 摘要旧历史),放独立线程执行
        2. 主对话流不等待压缩完成——先返回 keep-recent 历史,压缩结果异步写回
        3. 压缩失败/超时由三级保护兜底,不影响用户当前请求
        4. 压缩过程独立日志记录,便于审计

        三级保护(对标 CC):
        1) 熔断器:连续 3 次失败 → 120s 内不再尝试
        2) PTL 防御:摘要本身超限 → 剥掉 20% 旧分组重试(最多 N 次)
        3) 降级:全失败 → 简单截断(有损但不锁死)

        sidechain 存储压缩轨迹(对标 CC 的 sidechain transcript):
        - compacted 事件进 append-only 历史(已有)
        - 新增 compact_trace 条目:记录压缩前 token 数、压缩后 token 数、
          摘要内容、是否触发降级。用于调优阈值和审计。
        """
        # 1. 取状态补偿(调 tool.summarize_artifact)
        # 2. 在 forked context 调 LLM 摘要(独立超时、独立重试)
        # 3. 写 compacted + compact_trace 条目
        ...

    # ── 动态上下文注入(对标 CC 的 session_guidance/scratchpad dynamic section)──
    def dynamic_context(self, state) -> dict:
        """返回当前会话的动态态(当前 artifact 摘要、压缩历史等),
        供 prompt 装配时作为独立 section 注入,与静态 prompt 解耦。"""
```

**机制归 Engine,内容归 pack**:
- Engine 拥有:append-only 存储、阈值判断、keep-recent、熔断器、PTL 防御、调 LLM 摘要、动态上下文注入
- pack 提供:`tool.summarize_artifact(artifact)` 返回状态补偿文本
- 压缩 prompt 模板由 pack 的 prompts 提供(因为要提"表单"还是"报表")

**与现有 SQLite 的关系(老数据不迁移)**:当前 `conversation_store.py` 用 conversations + messages 两表 + 覆盖式 save。v4 存储改造**直接重建新表**——开发期无生产数据需要保护,不做数据迁移:
- 新建 `events` 表(append-only,含 kind 列)、`session_meta` 表(列表查询)
- 删除旧的 conversations/messages 表(或重命名为 `_legacy_` 留档备查)
- 旧表数据不导入;新会话从空表开始

这样做的好处:不用写迁移脚本、不用考虑 schema 兼容、不用灰度切换。详见阶段 4。

### 5.3 StreamBridge

```python
# engine/stream.py
class StreamManager:
    """asyncio.Queue 桥接同步工具执行到 SSE。"""
    def emit(self, event_type: str, data: dict): ...  # stage/result/error/done
    def run_tool_streaming(self, dispatcher, ...) -> AsyncGenerator: ...
```

完全复用现有 `sse.py` 的实现,只改一处:result 事件的 payload 通过 `tool.format_result(state)` 拿,不再硬读 `formFieldConfigVos`。

### 5.4 PromptLoader:C.2-C section 缓存 + C.2-E override/append

prompt 的加载、缓存、多来源合并归 Engine,模板内容归 pack。

```python
# engine/prompt_loader.py
class PromptLoader:
    """Prompt section 装配器(对标 CC systemPrompt.ts + systemPromptSections.ts)。

    两项增强(v4):
    - C.2-C:section 级缓存(以 pack+name 为 key,可标记 cacheable=False 强制重算)
    - C.2-E:区分 override(替换默认)/ append(挂尾)/ custom(替换 pack 默认)
    """

    def render(self, pack_name: str, name: str, **vars) -> str:
        """渲染单个 prompt 模板。pack 用 Jinja2,带 section 级缓存。
        缓存 key = (pack_name, name, vars 的可哈希部分)。
        模板文件可声明 frontmatter cacheable: false 强制每次重算(如含时间戳)。"""
        cache_key = self._cache_key(pack_name, name, vars)
        if cached := self._cache.get(cache_key):
            return cached
        template = self._load_template(pack_name, name)  # 从 pack 的 prompts/ 读
        rendered = self._jinja_env.from_string(template).render(**vars)
        if self._is_cacheable(pack_name, name):
            self._cache[cache_key] = rendered
        return rendered

    def assemble(self, pack_name: str, name: str, sections: list[str],
                 dynamic: dict, overrides: PromptOverrides = None) -> str:
        """组装完整 prompt(C.2-E 优先级,对标 CC buildEffectiveSystemPrompt):
            0. override → 完全替换(最高优先级,通常不用)
            1. 静态主干 = {% include %} 拼接 sections
            2. 动态段(dynamic dict)作为独立 section 追加,与静态解耦
            3. append → 挂到最末(无论前面来源)
        """
        if overrides and overrides.override:
            return overrides.override
        parts = [self.render(pack_name, f"_sections/{s}", **dynamic) for s in sections]
        parts.append(self.render(pack_name, name, **dynamic))  # 主模板
        if overrides and overrides.append:
            parts.append(overrides.append)
        return "\n\n".join(p for p in parts if p.strip())

@dataclass
class PromptOverrides:
    """多来源 prompt 合并(C.2-E)。当前只有 pack 一种来源,但留口子。
    未来:embed 宿主可传 override 覆盖 pack 的身份定位;append 追加企业规则。"""
    override: Optional[str] = None   # 完全替换(慎用)
    append: Optional[str] = None     # 追加到末尾
```

**设计说明**:
- **缓存粒度是 section,不是整份 prompt**。静态片段(intro/field_types/output_rules/safety)一旦渲染就缓存,动态段(当前 artifact、压缩历史)每次重算。这避免了对"整份 prompt"缓存的失效难题。
- **override vs append 的语义区分**(借鉴 CC):override 是"完全替换",append 是"挂尾"。当前只有 pack 一种来源,但 embed 宿主未来可能注入"你是 XX 公司的助手"这类 override,或"遵循 XX 合规规则"这类 append。
- **注入防护不变**:pack 渲染的领域 prompt 视为 trusted;override/append 内容来自外部,渲染时**不走 Jinja2**(纯文本拼接),避免宿主侧的模板注入。

## 六、njmind_form Tool Pack

这是第一个 pack,也是迁移的载体。

### 6.1 目录结构

```
domains/njmind_form/
├── __init__.py                # NjmindFormPack:注册 3 个工具
├── tools/
│   ├── create_form.py         # CreateFormTool(CompositeTool)
│   ├── modify_form.py         # ModifyFormTool(CompositeTool)
│   └── chat.py                # ChatTool(Tool)
├── prompts/
│   ├── _sections/             # 可复用 prompt 片段(对标 CC systemPromptSections)
│   │   ├── intro.j2           #   角色定位("你是 njmind 表单助手")
│   │   ├── field_types.j2     #   FIELD_TYPE_TABLE
│   │   ├── output_rules.j2    #   JSON 输出格式约定
│   │   └── safety.j2          #   注入防护提示
│   ├── select.j2              # 工具选择 prompt(可选,Engine 有默认)
│   ├── parse.j2               # 字段解析(引用 _sections)
│   ├── generate.j2            # 配置组装
│   ├── modify.j2              # 配置修改
│   ├── chat.j2                # 闲聊
│   └── compact.j2             # 压缩
├── adapters/
│   └── upstream.py            # HttpAssetClient 配 njmind 路径表
├── models.py                  # FormConfig / ParsedField Pydantic
└── config.yaml                # TYPE_TO_TEMPLATE / TYPE_NAMES / URL
```

### 6.2 CreateFormTool(复合工具)

把当前 6 步管线几乎原样搬进来:

```python
class CreateFormTool(CompositeTool):
    name = "create_form"
    description = "根据自然语言需求生成 njmind 表单配置"
    when = "用户想新建表单时"
    steps = ["fetch_guide", "list_assets", "parse_fields",
             "fetch_templates", "generate", "validate"]

    def input_schema(self):
        return {"type": "object", "properties": {
            "user_input": {"type": "string"}
        }}

    def execute(self, state, ctx):
        self.run_pipeline(state, ctx)        # 自动 emit 每个 step
        return ToolResult(
            artifact=state["artifact"],
            summary=f"已生成「{state['artifact']['formName']}」",
        )

    def _step_parse_fields(self, state, ctx):
        prompt = self._render("parse", **state)
        parsed = ctx.llm_client.chat_json(...)
        if parsed.get("needsClarification"):
            raise ClarificationRaised(parsed["questions"])
        state["parsed_fields"] = parsed["fields"]

    def _step_generate(self, state, ctx):
        prompt = self._render("generate", **state)
        state["artifact"] = ctx.llm_client.chat_json(...)

    def _step_validate(self, state, ctx):
        """校验 + 工具内部 retry。retry 完全在工具内,Engine 不感知。"""
        result = ctx.asset_client.validate_artifact(state["artifact"], "create")
        if result["valid"]:
            return  # 通过
        # 未通过 → 工具内部 retry(重跑 generate)
        state["retry_count"] = state.get("retry_count", 0) + 1
        if state["retry_count"] < MAX_RETRIES:
            ctx.emit("stage", "validate_retry", message=f"校验失败,第 {state['retry_count']} 次重试")
            self._step_generate(state, ctx)  # 重跑前序 step
            return self._step_validate(state, ctx)  # 递归再校验
        # 超过 max_retries → 把错误塞进 state,execute 返回时 Engine 照常 emit result
        state["validation_errors"] = result["errors"]

    def summarize_artifact(self, artifact):
        return f"当前表单: {artifact['formName']} ({artifact['formCode']}), " \
               f"字段: {', '.join(f['fieldTitleText'] for f in artifact['formFieldConfigVos'])}"
```

**注意**:这些 `formName`/`formCode`/`formFieldConfigVos` 字段名**只出现在 pack 内部**。Engine 从不读它们。

### 6.3 ModifyFormTool(复合工具)

```python
class ModifyFormTool(CompositeTool):
    name = "modify_form"
    when = "用户想修改已有表单(加/删/改字段)"
    steps = ["fetch_guide", "modify", "validate"]

    def execute(self, state, ctx):
        # state["source_artifact"] 是已有配置
        self.run_pipeline(state, ctx)
        return ToolResult(artifact=state["artifact"], ...)
```

### 6.4 ChatTool(简单工具)

```python
class ChatTool(Tool):
    name = "chat"
    when = "闲聊、打招呼、与表单无关的问题"

    def execute(self, state, ctx):
        prompt = self._render("chat", **state)
        reply = ctx.llm_client.chat(...)
        return ToolResult(reply=reply, summary=reply)
```

## 七、迁移策略:绞杀者模式

**不做大爆炸重写**。分 5 个阶段,每个阶段应用都能跑、能测。

### 阶段 0:骨架搭建(无功能改动)

1. 创建 `engine/`、`sdk/`、`domains/njmind_form/` 目录
2. 写出所有 ABC 和 dataclass(`Tool`、`CompositeTool`、`ToolContext`、`ToolResult`、`AssetClient`、`ToolRegistry`)
3. 搭出 `ToolDispatcher`、`ConversationManager`、`StreamBridge` 骨架(空实现,委托给旧代码)
4. 写 1 个冒烟测试:确认现有流程仍工作

**交付**:目录结构 + ABC,无行为变化。

### 阶段 1:抽 AssetClient + 安全清洗

1. 把 `upstream_client.py` 的路径表挪到 `njmind_form/config.yaml`
2. 实现 `HttpAssetClient`,委托给现有 `UpstreamClient`
3. **安全清洗(附录 D.1 #1)**:实现 `sdk/sanitize.py`(`sanitize_text` + `sanitize_obj`),HttpAssetClient 每个 get_* 方法返回前调 `sanitize_obj`,清除零宽字符/方向反转字符/PUA
4. **连接复用(附录 D.5)**:HttpAssetClient 加连接 memoize + 超时控制
5. 让 pack 提供 `HttpAssetClient` 实例,Engine 通过 `ToolContext.asset_client` 注入
6. 删除 `upstream_client.py` 里的路径常量

**交付**:上游路径全部进配置,AssetClient 抽象可用,Unicode 隐写注入面已封堵。

### 阶段 2:抽 Prompt + C.2-C section 缓存 + C.2-E override/append

1. 把 `prompt_builder.py` 的 4 套 prompt 拆成 `njmind_form/prompts/*.j2`
2. **按 CC 的 section 模式组织**(附录 C.1 #4):静态片段放 `prompts/_sections/`(intro/field_types/output_rules/safety),工具 prompt 用 Jinja2 `{% include %}` 引用,动态内容(当前 artifact、压缩历史)作为独立 context 注入而非塞进模板变量
3. **实现 PromptLoader(§5.4,C.2-C)**:`engine/prompt_loader.py`,section 级缓存(以 pack+name 为 key,frontmatter 可声明 `cacheable: false` 强制重算)
4. **override/append 优先级(§5.4,C.2-E)**:`assemble()` 按 override→静态主干→动态段→append 顺序拼装;`PromptOverrides` 数据类承载外部来源(当前为空,留口子)
5. **注入防护**(附录 C.1):pack 渲染的领域 prompt 视为 trusted;AssetClient 返回的模板/数据作为 user-role 或独立 section 注入,**绝不进 Jinja2 变量渲染**;override/append 不走 Jinja2(纯文本拼接)
6. 删除 `prompt_builder.py`(逻辑搬进工具的 `_step_*` 方法)

**交付**:prompt 全部进 pack,`prompt_builder.py` 消失,section 装配 + 缓存 + override/append 可用。

### 阶段 3:把管线搬进工具 + 落库确认 + C.2-A 追问 + C.2-B 并行

1. 实现 `CreateFormTool` / `ModifyFormTool` / `ChatTool`,内部 `execute` 调用当前 `nodes.py` 的节点函数(先不重写,只是搬位置)
2. 把 `nodes.py` 的 `TYPE_TO_TEMPLATE`/`TYPE_NAMES` 挪到 `config.yaml`
3. `graph.py` 的拓扑挪到 `CompositeTool.steps` + `run_pipeline`
4. **实现 ToolDispatcher(§5.1)**:`_select_tools`(LLM 可返回 1..N 工具)+ `_partition_tool_calls`(按 is_concurrency_safe 分批)+ `_run_single`/`_run_concurrent`
5. **C.2-A 追问机制**:ToolResult.ask + AskSpec/AskQuestion/AskOption;dispatcher 检测 pending_ask 走 `_resume_ask`;`/api/chat` 新增 `answers` 参数接收用户回答;append-only 存 ask 条目
6. **C.2-B 并行执行**:只读工具(fetch_guide/list_assets)声明 `is_concurrency_safe=True`;`_run_concurrent` 用 ThreadPoolExecutor,context 修改延迟到批次完成
7. 加入执行拦截层(附录 C.1 #2):`validate_input` 在 `execute` 前调用,失败走 `error_for_llm` 回流;persist 类工具的 validate_input 必须显式实现(附录 D.5)
8. **落库前确认(附录 D.1 #5)**:persist 步骤前 emit `confirm` SSE 事件,用户确认才继续;ToolContext 支持 `dry_run` 跳过 persist 返回预览
9. 切换 `/api/chat` 走 `ToolDispatcher` 而非旧 graph

**交付**:三意图变成三工具,Engine 通过 ToolDispatcher 调度,njmind 知识全部在 pack 内,落库操作有确认环节。

### 阶段 4:存储改造 + 日志安全 + 压缩 sidechain + 清理 + 验证

1. **存储重建 append-only(附录 C.1 #5,老数据不迁移)**:新建 `events` 表(kind ∈ user/assistant/tool_result/compacted/compact_trace/checkpoint/ask)+ `session_meta` 表(列表查询)。旧的 conversations/messages 表重命名为 `_legacy_` 留档,不导入数据。新会话从空表开始
2. **日志 redact filter(附录 D.1 #2)**:实现 `engine/logging_filter.py`(RedactFilter,正则 redact Bearer/sk-/cookie),Engine 启动时挂载
3. 删除 `graph.py`、`nodes.py`(已迁完)
4. **C.2-D 压缩 sidechain 隔离**:压缩在 forked 线程执行(独立超时、独立重试),主对话流不等压缩完成;失败由三级保护兜底(熔断器/PTL/降级)
5. 压缩器升级(附录 C.1 #7/#8/#9 + D.1 #3):加 Summary Token 预留、PTL 防御、`dynamic_context` 状态重启补偿、**压缩后工具能力复灌**(重建 tool schema 注入)
6. **压缩轨迹记录(C.2-D)**:每次压缩写 `compact_trace` 条目(压缩前 token 数、压缩后 token 数、摘要内容、是否触发降级),供调优阈值和审计
7. 压缩器内容钩子化:Engine 调 `tool.summarize_artifact()`
8. SSE result payload 钩子化:Engine 调 `tool.format_result()`
9. 验证架构试金石:`grep -rE "form|formCode|template" engine/` 应无结果
10. 端到端回归:三意图 + 追问(C.2-A 重跑)+ 并发(C.2-B)+ 压缩(sidechain + PTL + 能力复灌)+ header 透传(日志不泄漏)+ 落库确认 + SSE + 会话恢复

**交付**:Engine 零领域知识,存储工业级,压缩隔离运行,安全防护完整,迁移完成。

### 各阶段验收

| 阶段 | 可运行 | 可测试 | 回滚成本 |
|------|--------|--------|---------|
| 0 | ✓ | 冒烟 | 极低 |
| 1 | ✓ | AssetClient 单测 | 删目录即可 |
| 2 | ✓ | prompt 渲染测试 | 删目录即可 |
| 3 | ✓ | 端到端三意图 | 切回旧 graph |
| 4 | ✓ | 试金石 grep + 全回归 | — |

## 八、硬问题的处理

### 8.1 意图识别:通用还是领域?

**机制归 Engine,标签归 pack**。

- Engine 拥有意图识别的**编排**:调 LLM、从注册表选工具、执行
- pack 通过 `tool.when` 字段告诉 Engine "这个工具何时用"
- 三种意图的**语义**(create/modify/general)由 pack 的工具集合自然表达
- 不同领域注册不同工具集,意图的"含义"自然不同

**好处**:Engine 不需要"意图"这个概念,只有"工具选择"。

### 8.2 压缩器:半通用半专属

**机制与内容分离**。

- Engine 拥有机制:阈值判断、keep-recent、熔断器、调 LLM 摘要、把摘要注入下一轮
- pack 提供内容:`tool.summarize_artifact(artifact)` 返回状态补偿
- 压缩 prompt 模板由 pack 提供(因为要提"表单"还是"报表")

### 8.3 SSE 结果:通用协议 + 领域 payload + 接口约束

**接口约束(v4 明确)**:后端 SSE/请求体可重构,前端配套同步改(前后端都是我们控制,一个版本内同步发)。不写适配层技术债。

**请求侧**:`POST /api/chat` 请求体新增 `answers` 字段(可选,C.2-A 追问恢复时携带):
```json
{ "message": "创建请假表", "conversation_id": "xxx",
  "answers": { "请假单需要哪些字段": ["申请人","请假类型","..."] } }  // 可选
```

**响应侧**:SSE 事件统一为 4 种(stage / result / error / done),其中 result 的 data schema:
```typescript
{
  event: "result",
  data: {
    type: "config" | "ask" | "reply" | "error",   // 通用四态(C.2-A:clarification→ask)
    tool: "create_form",                          // 哪个工具产出
    payload: { ... },                             // pack 自定义(不透明,Engine 不读)
    summary: "已生成「请假申请表」"                // pack 提供,进对话历史
  }
}
```

各 type 的 payload 约定:
- `config`:pack 自定义的制品(如 formConfig JSON),前端按 pack 知识渲染
- `ask`:追问规格(AskSpec,见 §4.1),前端渲染追问 UI,用户回答后带 `answers` 发新请求
- `reply`:普通文本回复(闲聊/解释),`payload = { text: "..." }`
- `error`:`payload = { message, retryable }`,前端提示并允许重试

前端按 `type` 路由展示。后端 SSE 事件类型(stage/result/error/done)不变,只是 result 的 data 字段从旧的零散结构统一成 `{type, tool, payload, summary}`。

## 九、风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| 迁移期间双套代码混乱 | 高 | 绞杀者模式:每阶段旧代码先委托、后删除,从不同时存在两套实现 |
| **上游内容 Unicode 隐写注入**(附录 D.1 #1) | 高 | HttpAssetClient 返回前调 `sanitize_obj`(NFKC + 删零宽/方向反转字符) |
| **日志泄漏 Authorization/cookie**(附录 D.1 #2) | 高 | Engine 启动挂 RedactFilter,正则 redact Bearer/sk-/cookie 模式 |
| ToolDispatcher 的 LLM 选工具准确率 | 中 | 单步选择 + when 字段约束 + 兜底(选错时 `validate_input` 或 execute 失败,走 `error_for_llm` 回流让下轮自纠,对标 CC) |
| 复合工具内部 retry 逻辑复杂 | 中 | `CompositeTool.run_pipeline` 提供 step 编排;retry 完全在工具内(§6.2 _step_validate 递归重跑);clarify 走 ToolResult.ask + dispatcher 重跑(§5.1) |
| 追问重跑死循环(C.2-A) | 中 | `_max_clarify_rounds` 上限(默认 3 轮);超限 emit error 并清除 pending_ask |
| pack 内 prompt 的 Jinja2 变量命名不统一 | 中 | 阶段 2 制定变量命名规范(user_input/history/artifact/guide 统一) |
| **压缩后工具能力声明丢失**(附录 D.1 #3) | 中 | 压缩后 `buildPostCompactMessages` 等价物:重建 tool schema 注入下一轮 |
| **落库操作无确认,误改上游数据**(附录 D.1 #5) | 中 | persist 前 emit `confirm` SSE,用户确认才继续;可选 dry_run 预览 |
| 老数据不迁移,现有会话丢失 | 低 | 开发期无生产数据,旧表重命名为 `_legacy_` 留档备查;新表从零开始,无迁移风险 |
| 前后端接口同步改的协调成本 | 中 | 后端重构 SSE/请求体,前端配套同步改;约束在"一个版本内同步发",避免长期双轨 |
| 压缩 PTL 防御误伤 | 低 | 剥洋葱有损,加日志告警;`MAX_PTL_RETRIES` 上限 3 次,超限停止并保留原始未压缩版本 |
| 注入风险:上游返回的模板含恶意内容 | 低 | 注入防护原则(附录 C.1 #4):上游数据绝不进 Jinja2 变量渲染,只作为 user-role 独立 section |
| 抽象过度:未来接的新领域不是"制品生成"型 | 低 | 先不为这个优化;Tool ABC 足够通用,SimpleTool 已覆盖非制品场景 |

## 十、不在本期范围

- 多步 Agent Loop(LLM 主动连续调用多个工具;当前是单步选择,虽支持返回多工具一次执行,但不支持"看完结果再决定下一步")
- MCP 客户端能力(需上游支持,目前上游是 REST)
- 多 pack 共存的路由(目前只一个 pack;ToolRegistry 已支持注册多个,但无跨 pack 选择策略)
- 动态工具发现(目前 pack 启动时静态注册)
- Tool 并行的跨工具状态依赖(当前并发批次内工具相互独立;若工具间有数据依赖,需声明 ordering)

## 十一、验收清单

架构改造完成的标志:

- [ ] `engine/` 下 `grep -rE "form|formCode|template|field"` 无结果
- [ ] `domains/njmind_form/` 包含全部 njmind 业务知识
- [ ] 写一个 `DummyTool` + `DummyPack` 能跑通端到端(证明 Engine 不绑 njmind)
- [ ] 三个意图(create/modify/general)行为与改造前一致
- [ ] 压缩三级保护可用:70% 阈值触发 + 熔断器 + PTL 防御(附录 C.1 #7/#8/#10)
- [ ] 压缩状态重启补偿生效:`tool.summarize_artifact` 内容注入下一轮 + 工具能力复灌,压缩后 LLM 不丢"在做什么"和"有什么工具"
- [ ] append-only 存储验证:写 100 轮后崩溃重启,状态完整恢复
- [ ] **Unicode 清洗生效**:上游返回含零宽字符的模板,经 sanitize_obj 后干净(附录 D.1 #1)
- [ ] **日志不泄漏凭证**:打印 forward_headers 时 Authorization/cookie 显示为 ***(附录 D.1 #2)
- [ ] **落库有确认环节**:create/modify 在 persist 前 emit confirm,用户确认才落库;dry_run 模式可预览(附录 D.1 #5)
- [ ] 工具失败回流:`error_for_llm` 能让下一轮 LLM 感知上次失败
- [ ] SSE、header 透传、对话存储全部工作
- [ ] **接口约束达成(v4)**:后端 SSE/请求体重构为 `{type, tool, payload, summary}`,前端配套同步改,一个版本内发齐
- [ ] **C.2-A 追问机制**:工具产出 ask → 前端渲染追问 UI → 用户回答带 answers 重发 → dispatcher 检测 pending_ask 重跑工具;追问上限 3 轮
- [ ] **C.2-B 并行执行**:多个 is_concurrency_safe 工具并发执行;不安全的串行;并发批次 context 修改延迟 apply
- [ ] **C.2-C section 缓存**:静态 section 渲染后缓存,动态段每次重算;`cacheable: false` 的强制重算生效
- [ ] **C.2-D 压缩 sidechain**:压缩在 forked 线程执行,主对话流不阻塞;`compact_trace` 条目记录压缩轨迹
- [ ] **C.2-E override/append**:assemble 按 override→静态→动态→append 顺序拼装;override 不走 Jinja2
- [ ] **老数据不迁移**:旧表重命名 `_legacy_`,新表从零开始,无迁移脚本
- [ ] 现有测试全部通过(或等价改造后通过)

---

## 附录 A:决策记录

| 决策 | 选择 | 否决的方案 | 理由 |
|------|------|-----------|------|
| 定位 | 工具助手 | 通用管线框架 | 工具助手是系统本质,管线是 njmind 的实现细节 |
| 分层 | 六边形 | 简单分层 | 依赖反转,真正可替换 |
| 接入 | Python 插件包 | 纯 JSON/YAML | 表达复杂逻辑(拼音转 key、类型推断) |
| 管线 | 固定拓扑活在复合工具内 | 全声明式 | 复用 njmind 经验,避免重写拓扑 |
| State | 不透明 artifact 容器 | 泛型 State 子类 | Engine 零领域知识 |
| Prompt | pack 内 Jinja2 模板 | Python 字符串 / 全局目录 | 可维护、可 hot-reload |
| 调度 | 单轮多工具选择(v4 升级自单步) | 多步 Agent Loop / 意图路由 | 单轮可控,支持并行但不循环,成本可控 |
| 工具协议 | 自建轻量 Tool ABC | MCP-First | 上游是 REST 不是 MCP,绑 MCP 不可行 |
| 迁移 | 绞杀者模式 | 大爆炸重写 | 生产系统不能停机 |
| Tool 安全默认 | Fail-Closed | 默认安全 | 借鉴 CC,默认破坏性,pack 显式声明才安全 |
| 工具执行 | 加 validate_input 拦截层 | 直调 execute | 借鉴 CC 六段 pipeline,留 hook/审计位 |
| 存储模型 | append-only 事件流 | 覆盖式快照 | 借鉴 CC,崩溃只需重放尾部,压缩不丢原始 |
| 压缩保护 | 三级(阈值+熔断+PTL) | 单一阈值 | 借鉴 CC,应对各种边界(附录 C.1 #7/#8/#10) |
| Prompt 组织 | section 装配 | 单文件模板 | 借鉴 CC,段级缓存、注入隔离更清晰 |
| 追问建模 | 内置 AskTool(ToolResult.ask) | 异常短路 | v4 升级(C.2-A):追问答案进历史,支持多轮重跑 |
| 并发模型 | isConcurrencySafe + 分批 | 全串行 | v4 升级(C.2-B):Fail-Closed 默认串行,只读工具显式声明并发 |
| 压缩执行 | forked sidechain | 主链同步 | v4 升级(C.2-D):不阻塞主对话流,独立超时/重试/轨迹 |
| Prompt 来源 | override/append 优先级 | 单一 pack 来源 | v4 升级(C.2-E):留口子给 embed 宿主注入 |
| 老数据 | 不迁移,新表重建 | 数据迁移脚本 | 开发期无生产数据,避免迁移风险 |
| 接口策略 | 后端重构 + 前端同步改 | 适配层逐字段不变 | 前后端都是我们控制,一个版本内同步发,避免适配层技术债 |

## 附录 B:与 chat-bi 项目的对照

chat-bi 是参考过的多轮对话项目。本项目与它的差异:

| 维度 | chat-bi | 本项目 |
|------|---------|--------|
| 制品 | SQL 查询 | njmind 表单配置 |
| 多步 | 单步(一次出 SQL) | 复合(6 步管线) |
| 压缩 | 是 | 是(对标实现) |
| 工具化 | 否(只有 SQL 一种制品) | 是(多种工具并存) |

本项目的"复合工具"抽象是 chat-bi 没有的,因为 chat-bi 只有单一制品类型。

## 附录 C:Claude Code 设计借鉴

研读了 [claude-code-analysis](https://github.com/996Code/claude-code-analysis) 的架构总览、工具调用、Prompt 管理、会话存储、压缩、技能六个章节后,把可借鉴的设计点记录于此。每个点标注:CC 做法 → 落到本文档的位置 → 当前是否采纳。

### C.1 已采纳(融入正文)

| # | CC 设计 | 源码引用 | 落到本文档 | 价值 |
|---|--------|---------|-----------|------|
| 1 | **Tool 协议 Fail-Closed 默认值** | `src/Tool.ts:757` | 4.1 `Tool.is_destructive=True` | 防误用,默认破坏性需显式声明安全 |
| 2 | **执行前六段 pipeline**(schema→validateInput→PreHooks→permission→call→PostHooks) | `services/tools/toolExecution.ts` | 5.1 `ToolDispatcher.run` 第 4 步 `validate_input` 拦截 | 把"校验"与"执行"分开,留 hook/审计位 |
| 3 | **ToolResult 错误回流**(失败也化为文本回灌 LLM) | `query.ts` normalize | 4.1 `ToolResult.error_for_llm` + 5.1 except 分支 | 让下一轮选择能感知"上次失败了、为什么" |
| 4 | **Prompt section 装配**(静态主干 + DYNAMIC_BOUNDARY + 动态段) | `constants/systemPromptSections.ts:483` | 6.1 `prompts/_sections/` 子目录 | 段间装配比整块模板更灵活、可缓存 |
| 5 | **append-only 事件流存储**(只 insert 不覆盖,compacted 也追加) | `sessionStorage.ts:634,686` | 5.2 `ConversationManager.append/load/save` | 崩溃只需重放尾部,压缩不丢原始数据 |
| 6 | **SessionMeta 轻量表**(列表只读头尾不 JOIN messages) | `sessionStoragePortable.ts:17` | 5.2 `list_meta()` | 列表页性能 |
| 7 | **压缩预留 Summary Token**(20K) | `services/compact/autoCompact.ts:33` | 5.2 `should_compress` 有效窗口 | 压缩本身有预算,不会自己撑爆 |
| 8 | **压缩 PTL 防御**(摘要本身超限时剥洋葱重试) | `services/compact/compact.ts:462` | 5.2 `compress` 失败处理 | 最后的救命稻草,有损但不锁死 |
| 9 | **压缩状态重启补偿**(重新注入正在做的 plan/skill/文件) | `services/compact/compact.ts:517` | 5.2 `dynamic_context` + pack `summarize_artifact` | 压缩后不丢"在做什么" |
| 10 | **熔断器**(连续失败停止) | `autoCompact.ts` MAX_CONSECUTIVE | 5.2 `compress` 熔断 | 已有,继续保留 |
| 11 | **requires_follow_up 钩子**(默认 False,为多步留口) | Tool 接口 | 4.1 `Tool.requires_follow_up` | 当前单步,不焊死协议 |
| 12 | **CompositeTool ≈ CC 的 Skill**(工作流+触发条件封装) | `main.tsx:158` | 4.1 CompositeTool 注释 | 概念对标,避免重新发明 |

### C.2 本期纳入(v4 从"延后"升级)

> v3 时这 5 项标记为"延后考虑"。v4 评审决定**全部纳入本期**,拆成独立任务在迁移阶段 2-4 落地。理由:5 项分别属于 prompt / dispatcher / 压缩 三个不交叉的子系统,可并行推进,且都是 CC 验证过的成熟设计,不照搬复杂度但借鉴工程细节。

| # | CC 设计 | 落到本文档 | 本期做法 |
|---|--------|-----------|---------|
| A | **Clarification 建模为内置 AskTool**(而非异常短路) | §4.1 `ToolResult.ask` + `AskSpec`/`AskQuestion`/`AskOption`;§5.1 `_run_single`/`_resume_ask`;§8.3 SSE `type=ask` | 工具产出 ask → SSE 推前端 → 用户带 answers 重发 → dispatcher 检测 pending_ask 重跑工具。`ClarificationRaised` 异常保留作向后兼容(迁移期未改造工具仍可用) |
| B | **Tool 并行执行 + isConcurrencySafe** | §4.1 `Tool.is_concurrency_safe`(Fail-Closed 默认 False);§5.1 `_select_tools`/`_partition_tool_calls`/`_run_concurrent` | LLM 可返回 1..N 工具;连续 safe 的并发(ThreadPoolExecutor),unsafe 串行;context 修改延迟到批次完 apply(借鉴 CC 延迟 contextModifier) |
| C | **Prompt section 级缓存**(以 pack+name 为 key) | §5.4 `PromptLoader.render`,cache key=(pack, name, vars 可哈希部分) | 静态 section 渲染后缓存;frontmatter `cacheable: false` 强制重算(如含时间戳的段) |
| D | **Forked Agent 压缩 sidechain 独立存储** | §5.2 `compress` 在 forked 线程执行;`compact_trace` 条目记录轨迹 | 压缩不阻塞主对话流;独立超时/重试;失败三级保护兜底;轨迹供调优和审计 |
| E | **customSystemPrompt 替换 vs appendSystemPrompt 挂尾** | §5.4 `PromptOverrides` + `assemble` 优先级(override→静态→动态→append) | 当前 pack 唯一来源,但留口子给 embed 宿主注入 override(身份)/append(合规规则);override 不走 Jinja2 防注入 |

### C.3 关键认知(影响整体设计)

**1. Transcript 是唯一真理**

CC 把一切运行状态(工具结果、计划、技能、用户输入)都化为文本回灌给模型。这印证了我们"单轮选择"路线正确——我们甚至不让 LLM 填参数(参数从 state 抽取),比 CC 更保守,但同样遵守"状态文本化"。

**2. 我们的"单步"已升级为"单轮多工具并行"(v4)**

CC 是多步 Function Calling(`query.ts:351` 的 while 循环,看完结果再决定下一步)。v3 时我们刻意单步单工具。v4(C.2-B)升级为:**单轮选择 1..N 个工具一次执行,但不做"看完结果再选下一批"的 Agent Loop**。理由:
- 我们的领域(表单生成)多数是"一次调用一个工具"的语义,不需要 LLM 自由组合多步
- 但"加字段A 和 删字段B"这类需求天然有多个独立操作,并行执行更自然
- 单轮选择比多步 loop 更可控、SSE 流式更简单、成本更低
- `requires_follow_up` 钩子为未来 Agent Loop 留了口子,不在协议上焊死

**3. Tool / Skill / Command 三分的启示**

CC 区分三个概念:Tool(原子能力)、Skill(按触发条件编排工作流)、Command(用户直接 `/` 调用)。我们用 `Tool` + `CompositeTool` 二分表达同样的分层:
- `Tool`(含 ChatTool)= CC 的 Tool
- `CompositeTool`(含 CreateFormTool/ModifyFormTool)= CC 的 Skill
- Command 暂不需要(没有用户直接调用的场景)

未来 `CompositeTool.when` 可升级为结构化 trigger(关键词/状态谓词),提升 `_select_tools` 准确率。

### C.4 我们与 CC 的根本差异

| 维度 | Claude Code | 我们 |
|------|-------------|------|
| 定位 | 通用编程助手 | 领域工具助手(调上层系统能力) |
| 上游能力 | 本地文件系统 + 任意 Shell 命令 | 远程 REST API(njmind modeler) |
| 主循环 | 多步 Agent Loop(看完结果再选下一步) | 单轮多工具选择(可选 1..N 个一次执行,不循环) |
| 工具来源 | 内建 + 用户目录 + MCP | Tool Pack(目前一个) |
| 安全模型 | 需要权限系统(本地命令危险) | 信任上游 + header 透传 |
| 持久化 | JSONL 文件 | SQLite + append-only 事件流(C.1 #5,老数据不迁移) |
| 追问 | AskUserQuestionTool(同步交互) | ToolResult.ask + 异步重跑(C.2-A,SSE 单向约束) |
| 压缩 | Forked Agent 独立进程 | forked 线程 sidechain(C.2-D) |

CC 的设计是"最大灵活性的通用框架",我们的是"领域收敛的工具助手"。借鉴它的**工程细节**(Fail-Closed、append-only、压缩三级保护、错误回流、追问工具化、并发分批、prompt section 装配),但**不照搬它的复杂度**(多步 loop、本地权限系统、MCP 生态、Forked Agent 独立进程)。

## 附录 D:多视角重读发现(v3 补充)

第二遍换了 5 个视角(安全/健壮性/编排/记忆/隔离)重读 claude-code-analysis 的另外 10 个章节(02-security / 03-privacy / 04d-mcp / 04e-sandbox / 04h-multi-agent / 04-agent-memory / 05-differentiators / 06-extra-findings / 06b-negative-keyword)。这一遍挖出的是**藏在工程细节里的小巧思**,和附录 C 的"大设计"互补。

### D.1 必须补的(真实缺口,已在正文强化)

| # | 发现 | 视角 | CC 源码 | 落到本文档 |
|---|------|------|---------|-----------|
| 1 | **Unicode 隐写清洗**——上游返回内容若含零宽字符(`\u200B-F`)、方向反转字符(`\u202A-E`)、BOM、PUA,可注入隐藏指令。CC 的 `partiallySanitizeUnicode` 循环 10 次 NFKC + 递归处理 JSON | 安全 | `utils/sanitization.ts` | D.2 / 4.2 AssetClient |
| 2 | **日志层凭证 redact**——我们透传 Authorization/cookie,但任何 `logger.info` 或 traceback 都可能泄漏。CC 有 `SECRET_RULES` 扫描 30+ 规则 + `AnalyticsMetadata_I_VERIFIED_...` 类型签名约束 | 安全 | `teamMemorySync/secretScanner.ts`、`services/analytics/index.ts` | D.3 / 5.3 StreamBridge |
| 3 | **压缩后能力复灌**——压缩历史后,工具能力声明(tool schema)要重新注入,否则 LLM 忘记自己有什么工具。CC 的 `buildPostCompactMessages` 重建 tool schema + 文件附件 | 健壮性 | `compact/compact.ts:517` | 5.2 ConversationManager.compress |
| 4 | **工具内部 step 产出绝不进历史**——CompositeTool 中间 step(parse_fields、fetched_templates)只活在 state,只有最终 summary 入 ConversationManager。这是 task-notification 模式的核心,也是对 ToolResult 三层设计的强力背书 | 编排 | `04h L332-339` task-notification | 4.1 ToolResult 注释 + 5.2 save |
| 5 | **落库前确认环节**——create_form/modify_form 真正改上游数据库,应在 validate 通过后、persist 前 emit `confirm` SSE 事件,用户确认才继续。这是 CC permission prompt 的等价物,比 dry-run 更轻 | 隔离 | `04e bashPermissions.ts:530` | D.4 / 6.2 CreateFormTool |

### D.2 安全增强:Unicode 清洗 + 注入隔离(补 4.2 节)

AssetClient 返回的所有上游内容(模板、schema、guide、validate 结果)在进入 prompt 前**必须经过清洗**:

```python
# sdk/sanitize.py(新增)
MAX_UNICODE_ITERATIONS = 10

def sanitize_text(text: str) -> str:
    """清洗 Unicode 隐写(对标 CC partiallySanitizeUnicode)。
    循环 NFKC 归一化 + 删除零宽/方向反转/BOM/PUA 字符。"""
    for _ in range(MAX_UNICODE_ITERATIONS):
        prev = text
        text = unicodedata.normalize("NFKC", text)
        text = text.translate(_INVISIBLE_CHARS)  # 零宽、方向反转、BOM、PUA
        if text == prev:
            break
    return text

def sanitize_obj(obj):
    """递归清洗 dict/list/str(对标 CC recursivelySanitizeUnicode)。"""
    ...
```

**接入点**:HttpAssetClient 的每个 get_* 方法在返回前调 `sanitize_obj`。pack 渲染 prompt 时,上游数据只作为 user-role 独立 section 注入,**绝不进 Jinja2 变量渲染**(附录 C.1 已述)。

### D.3 安全增强:日志 redact filter(补 5.3 节)

Engine 启动时挂一个 logging filter,自动 redact 敏感模式:

```python
# engine/logging_filter.py(新增)
_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[^\s]+"), r"\1***"),
    (re.compile(r"(sk-)[a-zA-Z0-9]{20,}"), r"\1***"),
    # cookie 整体 redact
    (re.compile(r"(cookie:\s*)[^\s]+", re.I), r"\1***"),
]

class RedactFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for pat, repl in _SECRET_PATTERNS:
            msg = pat.sub(repl, msg)
        record.msg = msg
        return True
```

**接入点**:Engine 初始化时 `logging.getLogger().addFilter(RedactFilter())`。这覆盖所有 `logger.info(f"... headers={forward_headers}")` 这类无意泄漏。

### D.4 隔离增强:落库前确认(补 6.2/6.3 节)

CompositeTool 的 `_step_validate` 通过后、`_step_persist` 前,插入确认环节:

```python
# CompositeTool 新增(补 4.1 节)
class PersistConfirmed(Exception):
    """落库前等待用户确认。Engine 转 confirm SSE,用户确认后重入。"""

# CreateFormTool._step_persist
def _step_persist(self, state, ctx):
    if not state.get("_confirmed"):
        ctx.emit("confirm", {
            "action": "create",
            "artifact_preview": state["artifact"],
            "message": "即将创建表单,请确认"
        })
        raise PersistConfirmed()
    ctx.asset_client.persist_artifact(state["artifact"], "create")
```

**Engine 侧**:收到 PersistConfirmed 后 emit confirm 事件,前端展示 artifact 预览 + 确认按钮;用户确认后带 `_confirmed=True` 重入同个工具(走会话状态标记,非新选择)。

**dry-run 可选**:ToolContext 加 `dry_run: bool`,True 时 pipeline 跑到 validate 为止跳过 persist,artifact 直接返回前端预览。对 modify_form 特别有用(看 diff)。

### D.5 健壮性增强(零散补丁)

| 增强点 | CC 做法 | 落地位置 |
|--------|---------|---------|
| 迭代硬上限 | `MAX_ITERATIONS=10`、`MAX_INCLUDE_DEPTH=5` | JSON 解析器、CompositeTool.steps 上限(防 DoS) |
| 并发原子提交 | `queuedContextModifiers` 收集后批次 apply | ConversationManager.append 批量化 |
| HttpAssetClient 连接复用 | `connectToServer` memoize + 15min auth cache | 阶段 1 抽 AssetClient 时实现 |
| Tool 资源预算声明 | Tool 接口可声明 max_llm_calls / max_steps | SDK 协议层(未来按需) |
| persist 类 validate_input 非空 | 显式 deny 优先于 auto-allow | create_form/modify_form 的 validate_input 必须实现 |

### D.6 明确不做的(诚实评估,防过度设计)

这一遍也确认了一些**不该做**的,记录以防未来误引入:

| 不做的事 | CC 做的原因 | 我们不做的原因 |
|---------|------------|---------------|
| 跨会话记忆(Auto/Agent/Team Memory) | CC 是长期协作同一代码库,用户偏好/项目知识收益高 | 我们是制品型工具,一次性任务;错误记忆持续污染成本 > 收益 |
| OS 级沙箱(文件系统/网络隔离) | CC 让 LLM 执行任意本地 Shell 命令 | 我们不执行本地代码、不碰文件系统,只调远程 REST |
| MCP 传输层/认证联邦 | CC 接入任意 MCP server,需协议对等联邦 | 上游是单一确定 REST,不是多 server 动态发现 |
| 信号进控制流(负面关键词路由) | CC 刻意避免——`matchesNegativeKeyword` 只 logEvent,不改 prompt/不切模型 | 埋点保持纯观测,不喂回工具选择逻辑 |
| Session Memory 独立层 | CC 作为 compact 的 SummaryMessage 缓存 | 我们已有 compact,Session Memory 是升级项非必需 |

**判断原则**:CC 的很多机制是"通用编程助手"的产物(长期协作、本地执行、多 server 联邦)。我们是"领域工具助手"(一次性制品生成、远程 API、单一上游),借鉴**工程细节**但**不照搬场景复杂度**。

### D.7 对已有设计的验证(这一遍的额外收获)

重读也**验证了**我们已有设计的正确性:

1. **ToolResult 三层设计(artifact/summary/extra)得到强力背书**——这正是 CC task-notification 的 Python 表达。子 agent 长输出只写 sidechain,主 agent 只收 summary + result。
2. **单轮选择的简化是合理的**——CC 的负面关键词分析揭示它刻意不在热路径用 LLM 做分类(用廉价正则)。我们连分类都不做(单轮选工具),更彻底。v4 升级为单轮多工具并行后,这个原则依然成立(不做多步循环)。
3. **CompositeTool ≈ CC subagent**——上下文隔离、结果标准化回流、嵌套禁止,这些原则我们已部分体现,可进一步固化。
4. **append-only + PTL 防御**——CC 的 compact 失败保护与我们的设计完全同向,这一遍补了"压缩后能力复灌"这个遗漏点。

### D.8 阅读覆盖度

| 章节 | 行数 | 第一遍 | 第二遍 |
|------|------|--------|--------|
| 01-architecture-overview | 531 | ✓ 架构 | — |
| 02-security-analysis | 586 | — | ✓ 安全 |
| 03-privacy-avoidance | 157 | — | ✓ 安全 |
| 04-agent-memory | 878 | — | ✓ 记忆 |
| 04b-tool-call-implementation | 393 | ✓ 工具调用 | — |
| 04c-skills-implementation | 261 | ✓ 技能 | — |
| 04d-mcp-implementation | 297 | — | ✓ 编排 |
| 04e-sandbox-implementation | 826 | — | ✓ 隔离 |
| 04f-context-management | 195 | ✓ 压缩 | — |
| 04g-prompt-management | 789 | ✓ Prompt | — |
| 04h-multi-agent | 922 | — | ✓ 编排 |
| 04i-session-storage-resume | 757 | ✓ 存储 | — |
| 05-differentiators | 306 | — | ✓ 健壮性 |
| 06-extra-findings | 318 | — | ✓ 健壮性 |
| 06b-negative-keyword | 474 | — | ✓ 健壮性 |

两遍合计覆盖 15 个章节、约 7900 行。剩余章节(07 代码索引、08 竞品对比、09 总结、10 文件树、11 彩蛋)为参考性内容,不影响架构决策。

