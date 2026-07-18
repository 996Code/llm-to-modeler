# 工具助手架构改造设计

**状态**:草案待评审(v2 — 融入 Claude Code 设计借鉴,见附录 C)
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
| `classify_intent_node` | `ToolDispatcher._select_tool()` | LLM 从注册表选工具 |
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
    """工具执行结果。"""
    artifact: Optional[dict] = None     # 不透明制品(Engine 不读内部)
    reply: Optional[str] = None         # 给用户的文本回复
    needs_clarification: bool = False
    clarification_questions: list[str] = []
    summary: str = ""                   # 用于对话历史与压缩(标准化、无运行时噪声)
    extra: dict = {}                    # 领域自由扩展(不进对话历史)
    error_for_llm: Optional[str] = None # 失败时给下一轮 LLM 的标准化错误文本(对标 CC 的错误回流)

class ClarificationRaised(Exception):
    """工具中途需要追问时抛出,Engine 转成 SSE result 事件。
    设计说明:对标 Claude Code 把"向人类提问"建模为 AskUserQuestionTool。
    当前用异常短路更简单;未来若要追问可被 LLM 再次选择,可改为内置 AskTool。"""
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
    以提升 _select_tool 的准确率。
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
    """资产来源的抽象。pack 用它取模板/schema/guide,不关心是 HTTP 还是本地。"""

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

### 5.1 ToolDispatcher:单步选择

```python
# engine/dispatcher.py
class ToolDispatcher:
    """单步工具选择与执行。"""

    def run(self, user_input: str, conv_id: str, ctx_extra: dict):
        # 1. 加载对话状态(append-only 读取,见 5.2)
        state = self._load_state(conv_id)
        state["user_input"] = user_input

        # 2. 压缩检查(机制归 Engine,状态补偿内容归 tool)
        state = self._maybe_compress(state)

        # 3. LLM 选工具(单步,从 registry 选)
        tool = self._select_tool(user_input, state)

        # 4. 执行拦截层(借鉴 CC 六段 pipeline 的前段)
        #    schema 校验已由 input_schema 表达;这里做语义校验
        err = tool.validate_input(state)
        if err is not None:
            result = ToolResult(error_for_llm=err, summary=f"输入校验失败: {err}")
            yield from self._emit_result(tool, result)
            self._save_state(conv_id, state, result)
            return

        # 5. 执行工具(工具内部自己处理 retry / clarify / pipeline)
        try:
            result = tool.execute(state, self._build_ctx(state, ctx_extra))
        except ClarificationRaised as e:
            yield from self._emit_clarification(e.questions)
            self._save_state(conv_id, state, None)
            return
        except Exception as e:
            # 失败回流(借鉴 CC 的错误回流):异常包装成 error_for_llm,
            # 让下一轮 LLM 选择能感知"上次工具失败了、为什么"
            result = ToolResult(error_for_llm=str(e), summary=f"工具执行失败: {e}")

        # 6. 按 ToolResult 状态发 SSE
        if result.needs_clarification:
            yield from self._emit_clarification(result.clarification_questions)
        else:
            yield from self._emit_result(tool, result)

        # 7. 保存对话状态(artifact 进 state,summary 进历史,append-only)
        self._save_state(conv_id, state, result)

        # 8. follow-up 预留(当前单步,工具可声明需要继续)
        # if tool.requires_follow_up(result): → 下一轮循环(未来 Agent Loop 入口)

    def _select_tool(self, user_input, state) -> Tool:
        """调一次 LLM,从 registry 选一个工具。
        Prompt = 工具清单 + 对话历史 + 用户输入。
        LLM 只返回工具名(因为工具参数在 execute 内部从 state 抽取,
        不需要 LLM 填参数——这是"单步选择"与 Function Calling 的区别)。
        """
        tools_desc = self.registry.describe_for_llm(state)
        # 具体实现细节见后续实现计划文档。
        ...

    def _maybe_compress(self, state):
        if self._conversation.should_compress(state):
            state["compressed_history"] = self._conversation.compress(state)
        return state
```

**关键**:`_select_tool` 内部只问一次 LLM,返回单个 Tool 实例。**不进入多步 loop**。

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
        """只追加,不覆盖。kind ∈ {user, assistant, tool_result, compacted, checkpoint}。
        崩溃恢复只需重放尾部,压缩也不删旧行而是写一条 compacted 条目。"""

    def load(self, conv_id) -> dict:
        """读取并重建状态。按 kind 分流:
        - user/assistant/tool_result 按序重建 messages
        - compacted 标记压缩点,其后为 keep-recent,其前为已压缩
        - checkpoint 用于持久化 artifact 快照、active_tool 等
        """

    def save(self, conv_id, state, result):
        """append 用户输入 + 工具产出(summary 标准化后)。
        ToolResult.summary 入历史,ToolResult.extra 不入(借鉴 CC normalizeMessagesForAPI 的噪声剔除)。
        artifact 写 checkpoint 条目(不进 messages,避免膨胀)。"""

    def list_meta(self) -> list[SessionMeta]:
        """列表页只读 SessionMeta(title/summary/updated_at),
        不 JOIN messages(对标 CC lite reader 只读头尾 64KB)。"""

    # ── 压缩:三级保护(对标 CC autoCompact.ts + compact.ts)──
    def should_compress(self, state) -> bool:
        """token > 有效窗口的 70%。有效窗口 = 总窗口 - 预留 Summary Token。"""

    def compress(self, state, tool: Tool) -> str:
        """调 tool.summarize_artifact() 拿状态补偿 → 调 LLM 摘要旧历史 →
        保留最近 N 轮。失败时:
        1) 熔断器:连续 3 次失败 → 120s 内不再尝试
        2) PTL 防御:摘要本身超限 → 剥掉 20% 旧分组重试(最多 N 次)
        """

    # ── 动态上下文注入(对标 CC 的 session_guidance/scratchpad dynamic section)──
    def dynamic_context(self, state) -> dict:
        """返回当前会话的动态态(当前 artifact 摘要、压缩历史等),
        供 prompt 装配时作为独立 section 注入,与静态 prompt 解耦。"""
```

**机制归 Engine,内容归 pack**:
- Engine 拥有:append-only 存储、阈值判断、keep-recent、熔断器、PTL 防御、调 LLM 摘要、动态上下文注入
- pack 提供:`tool.summarize_artifact(artifact)` 返回状态补偿文本
- 压缩 prompt 模板由 pack 的 prompts 提供(因为要提"表单"还是"报表")

**与现有 SQLite 的关系**:当前 `conversation_store.py` 用 conversations + messages 两表 + 覆盖式 save。迁移时 messages 表加 `kind` 列(支持 compacted/checkpoint),`save` 改成 `append` 序列;列表查询改走单独的 SessionMeta 视图。详见阶段 4。

### 5.3 StreamBridge

```python
# engine/stream.py
class StreamManager:
    """asyncio.Queue 桥接同步工具执行到 SSE。"""
    def emit(self, event_type: str, data: dict): ...  # stage/result/error/done
    def run_tool_streaming(self, dispatcher, ...) -> AsyncGenerator: ...
```

完全复用现有 `sse.py` 的实现,只改一处:result 事件的 payload 通过 `tool.format_result(state)` 拿,不再硬读 `formFieldConfigVos`。

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

### 阶段 1:抽 AssetClient

1. 把 `upstream_client.py` 的路径表挪到 `njmind_form/config.yaml`
2. 实现 `HttpAssetClient`,委托给现有 `UpstreamClient`
3. 让 pack 提供 `HttpAssetClient` 实例,Engine 通过 `ToolContext.asset_client` 注入
4. 删除 `upstream_client.py` 里的路径常量

**交付**:上游路径全部进配置,AssetClient 抽象可用。

### 阶段 2:抽 Prompt

1. 把 `prompt_builder.py` 的 4 套 prompt 拆成 `njmind_form/prompts/*.j2`
2. **按 CC 的 section 模式组织**(附录 C.1 #4):静态片段放 `prompts/_sections/`(intro/field_types/output_rules/safety),工具 prompt 用 Jinja2 `{% include %}` 引用,动态内容(当前 artifact、压缩历史)作为独立 context 注入而非塞进模板变量
3. 加载器:pack 提供 `_render(name, **vars)` 和 `_render_sections(names)`,支持段级缓存(可标记 `cacheable=False` 强制重算)
4. **注入防护**(附录 C.1):pack 渲染的领域 prompt 视为 trusted;AssetClient 返回的模板/数据作为 user-role 或独立 section 注入,**绝不进 Jinja2 变量渲染**
5. 删除 `prompt_builder.py`(逻辑搬进工具的 `_step_*` 方法)

**交付**:prompt 全部进 pack,`prompt_builder.py` 消失,section 装配可用。

### 阶段 3:把管线搬进工具

1. 实现 `CreateFormTool` / `ModifyFormTool` / `ChatTool`,内部 `execute` 调用当前 `nodes.py` 的节点函数(先不重写,只是搬位置)
2. 把 `nodes.py` 的 `TYPE_TO_TEMPLATE`/`TYPE_NAMES` 挪到 `config.yaml`
3. `graph.py` 的拓扑挪到 `CompositeTool.steps` + `run_pipeline`
4. 实现 `ToolDispatcher._select_tool`,替换 `classify_intent_node`
5. 加入执行拦截层(附录 C.1 #2):`validate_input` 在 `execute` 前调用,失败走 `error_for_llm` 回流
6. 切换 `/api/chat` 走 `ToolDispatcher` 而非旧 graph

**交付**:三意图变成三工具,Engine 通过 ToolDispatcher 调度,njmind 知识全部在 pack 内。

### 阶段 4:存储改造 + 清理 + 验证

1. **存储改 append-only**(附录 C.1 #5):`conversation_store.py` 的 messages 表加 `kind` 列(user/assistant/tool_result/compacted/checkpoint),`save` 改成 `append` 序列;新增 `SessionMeta` 视图供列表查询
2. 删除 `graph.py`、`nodes.py`(已迁完)
3. 压缩器升级(附录 C.1 #7/#8/#9):加 Summary Token 预留、PTL 防御、`dynamic_context` 状态重启补偿
4. 压缩器内容钩子化:Engine 调 `tool.summarize_artifact()`
5. SSE result payload 钩子化:Engine 调 `tool.format_result()`
6. 验证架构试金石:`grep -rE "form|formCode|template" engine/` 应无结果
7. 端到端回归:三个意图 + 压缩(含 PTL 防御)+ header 透传 + SSE + 会话恢复

**交付**:Engine 零领域知识,存储工业级,迁移完成。

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

### 8.3 SSE 结果:通用协议 + 领域 payload

```typescript
// SSE result 事件(通用 schema)
{
  event: "result",
  data: {
    type: "config" | "clarification" | "reply",   // 通用三态
    tool: "create_form",                          // 哪个工具产出
    payload: { ... },                             // pack 自定义(不透明)
    summary: "已生成「请假申请表」"                // pack 提供
  }
}
```

前端按 `type` 路由展示:`config` 渲染配置卡片,`clarification` 渲染追问,`reply` 渲染文本。

## 九、风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| 迁移期间双套代码混乱 | 高 | 绞杀者模式:每阶段旧代码先委托、后删除,从不同时存在两套实现 |
| ToolDispatcher 的 LLM 选工具准确率 | 中 | 单步选择 + when 字段约束 + 兜底(选错时 `validate_input` 或 execute 失败,走 `error_for_llm` 回流让下轮自纠,对标 CC) |
| 复合工具内部 retry/clarify 逻辑复杂 | 中 | `CompositeTool.run_pipeline` 提供 step 编排,ClarificationRaised 异常短路 |
| pack 内 prompt 的 Jinja2 变量命名不统一 | 中 | 阶段 2 制定变量命名规范(user_input/history/artifact/guide 统一) |
| **append-only 改造数据迁移风险** | 中 | 新增 `kind` 列默认值填充旧行;保留旧 `save` 方法做灰度,验证后切换;SQLite 事务保证原子 |
| **压缩 PTL 防御误伤** | 低 | 剥洋葱有损,加日志告警;`MAX_PTL_RETRIES` 上限 3 次,超限停止并保留原始未压缩版本 |
| **注入风险:上游返回的模板含恶意内容** | 低 | 注入防护原则(附录 C.1 #4):上游数据绝不进 Jinja2 变量渲染,只作为 user-role 独立 section |
| 抽象过度:未来接的新领域不是"制品生成"型 | 低 | 先不为这个优化;Tool ABC 足够通用,SimpleTool 已覆盖非制品场景 |

## 十、不在本期范围

- 多步 Agent Loop(未来按需)
- MCP 客户端能力(未来按需,需上游支持)
- 多 pack 共存的路由(目前只一个 pack)
- Tool 并行执行(目前单步选择)
- 动态工具发现(目前 pack 启动时静态注册)

## 十一、验收清单

架构改造完成的标志:

- [ ] `engine/` 下 `grep -rE "form|formCode|template|field"` 无结果
- [ ] `domains/njmind_form/` 包含全部 njmind 业务知识
- [ ] 写一个 `DummyTool` + `DummyPack` 能跑通端到端(证明 Engine 不绑 njmind)
- [ ] 三个意图(create/modify/general)行为与改造前一致
- [ ] 压缩三级保护可用:70% 阈值触发 + 熔断器 + PTL 防御(附录 C.1 #7/#8/#10)
- [ ] 压缩状态重启补偿生效:`tool.summarize_artifact` 内容注入下一轮,压缩后 LLM 不丢"在做什么"
- [ ] append-only 存储验证:写 100 轮后崩溃重启,状态完整恢复
- [ ] 工具失败回流:`error_for_llm` 能让下一轮 LLM 感知上次失败
- [ ] SSE、header 透传、对话存储全部工作
- [ ] `/api/chat` 接口对外不变(前端无感)
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
| 调度 | 单步工具选择 | 多步 Agent Loop / 意图路由 | 单步可控,与现状平滑对应 |
| 工具协议 | 自建轻量 Tool ABC | MCP-First | 上游是 REST 不是 MCP,绑 MCP 不可行 |
| 迁移 | 绞杀者模式 | 大爆炸重写 | 生产系统不能停机 |
| Tool 安全默认 | Fail-Closed | 默认安全 | 借鉴 CC,默认破坏性,pack 显式声明才安全 |
| 工具执行 | 加 validate_input 拦截层 | 直调 execute | 借鉴 CC 六段 pipeline,留 hook/审计位 |
| 存储模型 | append-only 事件流 | 覆盖式快照 | 借鉴 CC,崩溃只需重放尾部,压缩不丢原始 |
| 压缩保护 | 三级(阈值+熔断+PTL) | 单一阈值 | 借鉴 CC,应对各种边界(附录 C.1 #7/#8/#10) |
| Prompt 组织 | section 装配 | 单文件模板 | 借鉴 CC,段级缓存、注入隔离更清晰 |

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

### C.2 延后考虑(记录但本期不做)

| # | CC 设计 | 为什么延后 |
|---|--------|-----------|
| A | **Clarification 建模为内置 AskTool**(而非异常短路) | 当前 `ClarificationRaised` 异常更简单;等真有"追问可被 LLM 再选"的需求再改 |
| B | **Tool 并行执行 + isConcurrencySafe** | 当前单步选择无并发;未来引入 Agent Loop 时再加 |
| C | **Prompt section 级缓存**(以 pack+name 为 key) | 先用 Jinja2 简单实现;性能瓶颈出现再加缓存层 |
| D | **Forked Agent 压缩 sidechain 独立存储** | 当前压缩在主链内完成;若压缩逻辑变复杂再隔离 |
| E | **customSystemPrompt 替换 vs appendSystemPrompt 挂尾** | 当前只有 pack 一种来源;多来源混入时再区分 |

### C.3 关键认知(影响整体设计)

**1. Transcript 是唯一真理**

CC 把一切运行状态(工具结果、计划、技能、用户输入)都化为文本回灌给模型。这印证了我们"单步选择"路线正确——我们甚至不让 LLM 填参数(参数从 state 抽取),比 CC 更保守,但同样遵守"状态文本化"。

**2. 我们的简化是合理的**

CC 是多步 Function Calling(`query.ts:351` 的 while 循环),我们刻意单步。理由:
- 我们的领域(表单生成)是"一次调用一个工具"的语义,不需要 LLM 自由组合
- 单步更可控、SSE 流式更简单、成本更低
- `requires_follow_up` 钩子为未来留了口子,不在协议上焊死

**3. Tool / Skill / Command 三分的启示**

CC 区分三个概念:Tool(原子能力)、Skill(按触发条件编排工作流)、Command(用户直接 `/` 调用)。我们用 `Tool` + `CompositeTool` 二分表达同样的分层:
- `Tool`(含 ChatTool)= CC 的 Tool
- `CompositeTool`(含 CreateFormTool/ModifyFormTool)= CC 的 Skill
- Command 暂不需要(没有用户直接调用的场景)

未来 `CompositeTool.when` 可升级为结构化 trigger(关键词/状态谓词),提升 `_select_tool` 准确率。

### C.4 我们与 CC 的根本差异

| 维度 | Claude Code | 我们 |
|------|-------------|------|
| 定位 | 通用编程助手 | 领域工具助手(调上层系统能力) |
| 上游能力 | 本地文件系统 + 任意 Shell 命令 | 远程 REST API(njmind modeler) |
| 主循环 | 多步 Agent Loop | 单步工具选择 |
| 工具来源 | 内建 + 用户目录 + MCP | Tool Pack(目前一个) |
| 安全模型 | 需要权限系统(本地命令危险) | 信任上游 + header 透传 |
| 持久化 | JSONL 文件 | SQLite(可改 append-only 模式) |

CC 的设计是"最大灵活性的通用框架",我们的是"领域收敛的工具助手"。借鉴它的**工程细节**(Fail-Closed、append-only、压缩三级保护、错误回流),但**不照搬它的复杂度**(多步 loop、权限系统、MCP 生态)。

