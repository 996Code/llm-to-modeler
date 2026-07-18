# 工具助手架构改造设计

**状态**:草案待评审
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
    summary: str = ""                   # 用于对话历史与压缩
    extra: dict = {}                    # 领域自由扩展

class ClarificationRaised(Exception):
    """工具中途需要追问时抛出,Engine 转成 SSE result 事件。"""
    def __init__(self, questions: list[str]):
        self.questions = questions

class Tool(ABC):
    """工具基类。"""
    name: str                     # 工具名,LLM 选择时看到
    description: str              # 工具说明,LLM 选择时看到
    when: str                     # 简短描述"何时用"(填进选择 prompt)

    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema,描述这个工具需要的参数(从 state 抽取)。"""

    @abstractmethod
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具。可中途 emit progress,可抛 ClarificationRaised。"""

    # ── 可选 hooks(默认实现,pack 按需覆写)──
    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿文本。默认空。"""
        return ""

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从制品生成标题。默认空。"""
        return ""

class CompositeTool(Tool):
    """复合工具基类:内部有多步 pipeline 的工具。
    提供 step 注册 + progress 自动 emit 的便利。"""
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
        # 1. 加载对话状态
        state = self._load_state(conv_id)
        state["user_input"] = user_input

        # 2. 压缩检查(机制归 Engine,状态补偿内容归 tool)
        state = self._maybe_compress(state)

        # 3. LLM 选工具(单步)
        tool = self._select_tool(user_input, state)

        # 4. 执行工具(工具内部自己处理 retry / clarify / pipeline)
        try:
            result = tool.execute(state, self._build_ctx(state, ctx_extra))
        except ClarificationRaised as e:
            yield from self._emit_clarification(e.questions)
            self._save_state(conv_id, state, None)
            return

        # 5. 按 ToolResult 三态发 SSE
        if result.needs_clarification:
            yield from self._emit_clarification(result.clarification_questions)
        else:
            yield from self._emit_result(tool, result)

        # 6. 保存对话状态(artifact 进 state,summary 进历史)
        self._save_state(conv_id, state, result)

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

封装多轮 / 压缩 / 存储:

```python
# engine/conversation.py
class ConversationManager:
    def load(self, conv_id) -> dict: ...
    def save(self, conv_id, state, result): ...
    def should_compress(self, state) -> bool: ...
    def compress(self, state, tool: Tool) -> str:
        """调 tool.summarize_artifact() 拿状态补偿,
           调 LLM 把旧历史压成摘要,
           keep-recent 最近 N 轮。"""
```

**机制归 Engine,内容归 pack**:
- Engine 拥有:阈值判断、keep-recent、熔断器、调 LLM 摘要
- pack 提供:`tool.summarize_artifact(artifact)` 返回状态补偿文本
- 压缩 prompt 模板由 pack 的 prompts 提供(因为要提"表单"还是"报表")

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
│   ├── select.j2              # 工具选择 prompt(可选,有默认)
│   ├── parse.j2               # 字段解析
│   ├── generate.j2            # 配置组装
│   ├── modify.j2              # 配置修改
│   ├── validate.j2            # 校验后处理
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
2. 加载器:pack 提供 `_render(name, **vars)`,用 Jinja2 渲染
3. 删除 `prompt_builder.py`(逻辑搬进工具的 `_step_*` 方法)

**交付**:prompt 全部进 pack,`prompt_builder.py` 消失。

### 阶段 3:把管线搬进工具

1. 实现 `CreateFormTool` / `ModifyFormTool` / `ChatTool`,内部 `execute` 调用当前 `nodes.py` 的节点函数(先不重写,只是搬位置)
2. 把 `nodes.py` 的 `TYPE_TO_TEMPLATE`/`TYPE_NAMES` 挪到 `config.yaml`
3. `graph.py` 的拓扑挪到 `CompositeTool.steps` + `run_pipeline`
4. 实现 `ToolDispatcher._select_tool`,替换 `classify_intent_node`
5. 切换 `/api/chat` 走 `ToolDispatcher` 而非旧 graph

**交付**:三意图变成三工具,Engine 通过 ToolDispatcher 调度,njmind 知识全部在 pack 内。

### 阶段 4:清理与验证

1. 删除 `graph.py`、`nodes.py`(已迁完)
2. 压缩器内容钩子化:Engine 调 `tool.summarize_artifact()`
3. SSE result payload 钩子化:Engine 调 `tool.format_result()`
4. 验证架构试金石:`grep -rE "form|formCode|template" engine/` 应无结果
5. 端到端回归:三个意图 + 压缩 + header 透传 + SSE

**交付**:Engine 零领域知识,迁移完成。

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
| ToolDispatcher 的 LLM 选工具准确率 | 中 | 单步选择 + when 字段约束 + 兜底(选错时 tool.execute 抛错,Engine 转交用户) |
| 复合工具内部 retry/clarify 逻辑复杂 | 中 | `CompositeTool.run_pipeline` 提供 step 编排,ClarificationRaised 异常短路 |
| pack 内 prompt 的 Jinja2 变量命名不统一 | 中 | 阶段 2 制定变量命名规范(user_input/history/artifact/guide 统一) |
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
- [ ] 压缩、SSE、header 透传、对话存储全部工作
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

## 附录 B:与 chat-bi 项目的对照

chat-bi 是参考过的多轮对话项目。本项目与它的差异:

| 维度 | chat-bi | 本项目 |
|------|---------|--------|
| 制品 | SQL 查询 | njmind 表单配置 |
| 多步 | 单步(一次出 SQL) | 复合(6 步管线) |
| 压缩 | 是 | 是(对标实现) |
| 工具化 | 否(只有 SQL 一种制品) | 是(多种工具并存) |

本项目的"复合工具"抽象是 chat-bi 没有的,因为 chat-bi 只有单一制品类型。
