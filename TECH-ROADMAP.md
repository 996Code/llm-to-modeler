# LLM Form Modeler — 技术路径文档

> 自然语言 → 低码配置生成引擎
>
> 版本：v0.5 | 日期：2026-07-21

---

## 目录

1. [项目定位](#1-项目定位)
2. [系统架构](#2-系统架构)
3. [技术栈](#3-技术栈)
4. [核心引擎：LangGraph 工作流](#4-核心引擎langgraph-工作流)
5. [Skill 消费机制](#5-skill-消费机制)
6. [数据流全链路](#6-数据流全链路)
7. [API 设计](#7-api-设计)
8. [MCP 协议层](#8-mcp-协议层)
9. [用户身份 & 对话历史](#9-用户身份--对话历史)
10. [前端设计](#10-前端设计)
11. [部署](#11-部署)
12. [项目目录结构](#12-项目目录结构)
13. [开发路线图](#13-开发路线图)

---

## 1. 项目定位

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   用户说："创建一个请假申请表，包含请假类型、日期、原因"         │
│                                                                 │
│                          ↓                                      │
│                                                                 │
│   系统输出：符合 njmind 低码平台 Schema 的 FormConfig JSON       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**做什么**：自然语言 → 低码配置 JSON（基于 Skill 规则 + LLM 生成）

**不做什么**：表单渲染、运行时、用户管理、数据持久化（这些由低码平台负责）

**上下游关系**：

```
    上游（规则源）                   本项目                    下游（消费方）
    ┌──────────────┐            ┌──────────────┐          ┌──────────────────┐
    │ njmind-modeler│            │ llm-to-modler│          │ AI 工具           │
    │              │  Skill文件  │              │  MCP     │ (Claude/OpenCode)│
    │ 编译时生成    │───────────→│  Vue + Python │←────────│                  │
    │ Schema/模板/ │            │              │          │ Web 前端          │
    │ Skill 文件   │            │              │  HTTP    │ (对话+JSON导出)   │
    └──────────────┘            └──────────────┘─────────→│                  │
                                                          │ 低码平台          │
                                                          │ (表单渲染/运行时) │
                                                          └──────────────────┘
```

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户 / AI 工具                            │
└──────────────┬──────────────────────────────────┬───────────────┘
               │ HTTP/SSE (前端)                   │ JSON-RPC (AI工具)
               │                                   │
┌──────────────▼───────────────────────────────────▼───────────────┐
│                                                                  │
│                    Python 后端 (FastAPI)                          │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    路由层 (FastAPI)                         │  │
│  │                                                            │  │
│  │  REST API              MCP Server           SSE 端点       │  │
│  │  /api/config/*         /mcp                 /api/stream/*  │  │
│  │  /api/skills/*         (tools + resources)                 │  │
│  │  /api/conversations/*                                       │  │
│  └──────────────────────────┬─────────────────────────────────┘  │
│                              │                                    │
│  ┌──────────────────────────▼─────────────────────────────────┐  │
│  │                  业务服务层                                  │  │
│  │                                                            │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐   │  │
│  │  │ Skill        │  │ Schema       │  │ Conversation   │   │  │
│  │  │ Consumer     │  │ Validator    │  │ Manager        │   │  │
│  │  │              │  │              │  │                │   │  │
│  │  │ 文件监听     │  │ jsonschema   │  │ 多轮对话       │   │  │
│  │  │ 内存缓存     │  │ 校验         │  │ 上下文管理     │   │  │
│  │  │ 热更新       │  │              │  │                │   │  │
│  │  └──────────────┘  └──────────────┘  └────────────────┘   │  │
│  └──────────────────────────┬─────────────────────────────────┘  │
│                              │                                    │
│  ┌──────────────────────────▼─────────────────────────────────┐  │
│  │                LangGraph 工作流引擎                          │  │
│  │                                                            │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │  │
│  │  │ 加载Skill │→│ 意图分类  │→│ 数据准备  │→│ 配置生成  │  │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └────┬─────┘  │  │
│  │                                                  │         │  │
│  │                                        ┌─────────▼──────┐  │  │
│  │                                        │ Schema 校验    │  │  │
│  │                                        └────┬──────┬────┘  │  │
│  │                                          通过▼   失败▼     │  │
│  │                                        ┌─────┐ ┌────────┐  │  │
│  │                                        │ 输出 │ │重试(≤3)│  │  │
│  │                                        └─────┘ └────────┘  │  │
│  └────────────────────────────────────────────────────────────┘  │
│                              │                                    │
│  ┌──────────────────────────▼─────────────────────────────────┐  │
│  │                  LLM 调用层                                  │  │
│  │                                                            │  │
│  │  OpenAI SDK (兼容接口)                                     │  │
│  │  base_url 可配 → OpenAI / 通义 / DeepSeek / Ollama         │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
               │ 文件读取
               │
┌──────────────▼───────────────────────────────────────────────────┐
│              外部 Skill 文件目录 (由 njmind-modeler 生成)          │
│                                                                  │
│  skills/                                                         │
│  ├── _shared/RULES.md          ← 共享规则                       │
│  ├── njmind-form-field-create/SKILL.md                           │
│  ├── njmind-form-field-update/SKILL.md                           │
│  ├── njmind-form-field-get/SKILL.md                              │
│  ├── njmind-form-field-clone/SKILL.md                            │
│  ├── njmind-form-field-image/SKILL.md                            │
│  ├── mcp-schemas/*.json        ← 18 个 JSON Schema              │
│  ├── mcp-templates/*.json      ← 20 个模板                      │
│  └── mcp-guides/guide.json     ← 字段类型+关键词索引             │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    前端 (Vue 3)                                   │
│                                                                  │
│  ┌─────────────────────────┬──────────────────────────────┐      │
│  │  对话区域                │  JSON 输出区域                │      │
│  │  (对话气泡组件)          │  (Monaco Editor 只读)        │      │
│  │                         │                              │      │
│  │  👤 创建一个请假表单     │  {                           │      │
│  │  🤖 已生成配置...        │    "formName": "请假申请表",  │      │
│  │                         │    "fields": [...]            │      │
│  │  [输入框...]   [发送]    │  }                           │      │
│  │                         │  [导出JSON] [复制]            │      │
│  └─────────────────────────┴──────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────┘
```

**三层六边形架构：Engine(零领域知识) → SDK(协议) → Domain Pack(插件)**

---

## 3. 技术栈

### 3.1 总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        技术栈                                    │
├──────────────┬──────────────────────────────────────────────────┤
│              │                                                  │
│  前端         │  Vue 3 + TypeScript                             │
│              │  + Ant Design Vue 4 (组件库)                     │
│              │  + Monaco Editor (JSON 展示)                     │
│              │  + Vite 5 (构建工具)                              │
│              │  + Pinia (状态管理)                               │
│              │                                                  │
├──────────────┼──────────────────────────────────────────────────┤
│              │                                                  │
│  后端         │  Python 3.11 + FastAPI                          │
│  (一个服务    │  + LangGraph 0.2 (工作流编排)                   │
│   全包)       │  + LangChain-OpenAI 0.2 (LLM 集成)             │
│              │  + OpenAI SDK 1.30+ (兼容接口)                   │
│              │  + mcp (Python MCP SDK, 协议服务)                │
│              │  + Pydantic 2 (数据模型)                         │
│              │  + jsonschema (Schema 校验)                      │
│              │  + watchdog (文件监听)                            │
│              │  + sse-starlette (SSE 流式输出)                  │
│              │  + uvicorn (ASGI 服务器)                         │
│              │                                                  │
├──────────────┼──────────────────────────────────────────────────┤
│              │                                                  │
│  部署         │  Docker Compose                                 │
│              │  + python:3.11-slim (后端)                       │
│              │  + nginx:alpine (前端静态资源 + 反代)             │
│              │                                                  │
└──────────────┴──────────────────────────────────────────────────┘
```

### 3.2 后端依赖清单

```toml
# pyproject.toml
[project]
name = "llm-form-modeler"
requires-python = ">=3.11"

dependencies = [
    # Web 框架
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sse-starlette>=2.0",

    # LLM 工作流
    "langgraph>=0.2",
    "langchain-openai>=0.2",
    "openai>=1.30",

    # 数据校验
    "pydantic>=2.0",
    "jsonschema>=4.20",

    # 文件监听
    "watchdog>=5.0",

    # MCP 协议
    "mcp>=1.0",

    # 工具
    "httpx>=0.27",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.7",
]
```

### 3.3 前端依赖清单

```json
{
  "dependencies": {
    "vue": "^3.5",
    "ant-design-vue": "^4.2",
    "@monaco-editor/loader": "^1.4",
    "pinia": "^2.2"
  },
  "devDependencies": {
    "typescript": "^5.6",
    "vite": "^5.4",
    "@vitejs/plugin-vue": "^5.1"
  }
}
```

### 3.4 为什么选 LangGraph？

```
你的场景                          LangGraph 的能力
─────────                         ──────────────

固定流程                           StateGraph
(意图→数据→生成→校验)              (节点=步骤, 边=流转)
        ↕                                 ↕
动态规则注入                       State 携带 Skill 规则
(Skill 文件由外部更新)             (热加载, 不重启)
        ↕                                 ↕
校验重试循环                       Conditional Edge
(失败→回到生成节点, ≤3次)          (条件分支, 自动循环)
        ↕                                 ↕
多轮对话上下文                     Checkpoint
(记住之前的配置, 增量修改)         (状态持久化, 对话恢复)
        ↕                                 ↕
流式输出                           Streaming
(实时展示生成进度)                 (每个节点完成即推送)
```

**不选 DeepAgents 的原因**：

```
┌──────────────────────────────────────────────────────────────┐
│  1. "Trust the LLM" 理念 → LLM 可能跳步/犯错                │
│     你的场景需要确定性流程（必须先 get_schema 再 generate）    │
│                                                              │
│  2. Skill 格式不兼容 → SKILL.md 是 markdown 指令             │
│     需要转换，维护成本高                                      │
│                                                              │
│  3. 底层就是 LangGraph → 多一层抽象，不如直接用               │
│                                                              │
│  4. 社区小，文档少 → 遇到问题难排查                          │
│                                                              │
│  ✅ 结论：直接用 LangGraph + Skill Prompt 注入               │
└──────────────────────────────────────────────────────────────┘
```

### 3.5 LLM 模型策略

```
                    ┌─────────────────────────┐
                    │   OpenAI 兼容接口        │
                    │   (统一协议)             │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
     ┌────────▼───────┐ ┌───────▼───────┐ ┌────────▼───────┐
     │  OpenAI GPT-4o │ │ 通义千问 Qwen │ │  DeepSeek V3   │
     │  (默认)        │ │ (国内备选)    │ │ (高性价比)     │
     └────────────────┘ └───────────────┘ └────────────────┘

    配置方式（.env）：
    ┌──────────────────────────────────────────────┐
    │  LLM_BASE_URL=https://api.openai.com/v1      │
    │  LLM_API_KEY=sk-xxx                          │
    │  LLM_MODEL=gpt-4o                            │
    │  LLM_TEMPERATURE=0.1   ← 低温度, 要确定性    │
    │  LLM_MAX_TOKENS=4096                         │
    └──────────────────────────────────────────────┘
```

---

## 4. 核心引擎：LangGraph StateGraph

### 4.1 新架构（三层六边形）

```
Engine 层 (零领域知识,不含任何 form 相关代码)
──────────────────────────────────────────────

  LangGraph StateGraph:
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  START → classify_intent (LLM 选工具,动态从 registry)    │
  │             │                                            │
  │             ├─ "create_form"  → execute_tool → END      │
  │             ├─ "modify_form"  → execute_tool → END      │
  │             ├─ "get_form"     → execute_tool → END      │
  │             ├─ "clone_form"   → execute_tool → END      │
  │             ├─ "image_form"   → execute_tool → END      │
  │             ├─ "chat"         → execute_tool → END      │
  │             └─ fallback       → execute_tool → END      │
  │                                                          │
  │  execute_tool 内部:                                      │
  │    1. 从 registry 取 Tool 实例                           │
  │    2. 构建 ToolContext (llm_client + asset_client + ...) │
  │    3. tool.execute(state, ctx)                           │
  │    4. 如果 ToolResult.ask → interrupt() 挂起             │
  │    5. 用户回答 → Command(resume=answers) → 重跑同一工具  │
  │                                                          │
  │  Checkpoint: InMemorySaver, thread_id = conversation_id  │
  └──────────────────────────────────────────────────────────┘

SDK 层 (协议定义)
──────────────────────────────────────────────

  Tool          — 原子工具基类 (name/description/when/safety)
  CompositeTool — 多步管线工具 (steps + run_pipeline)
  ToolResult    — 三态: artifact / ask / error_for_llm
  AskSpec       — 追问协议 (questions + options)
  ToolContext    — 依赖注入 (llm/asset/emit/conversation/registry)
  ToolRegistry  — 注册表 (register/all/get/describe_for_llm)

Domain Pack 层 (插件化,零耦合)
──────────────────────────────────────────────

  njmind_form/pack.py:
    create_registry()      → 注册 6 个工具
    create_prompt_loader() → 加载 Jinja2 prompt 模板

  触摸石: grep -rE "form|formCode|template|field" engine/ → 必须为空
```

### 4.2 六个工具的工作流差异

```
                         ┌──────────────┐
                         │  用户输入     │
                         │ "创建请假表单"│
                         └──────┬───────┘
                                │
                    ╔═══════════▼═══════════╗
                    ║  Node 1: load_skill   ║
                    ║                       ║
                    ║  读取 SKILL.md        ║
                    ║  读取 RULES.md        ║
                    ║  注入 State           ║
                    ╚═══════════╤═══════════╝
                                │
                    ╔═══════════▼═══════════╗
                    ║  Node 2: intent       ║
                    ║                       ║
                    ║  LLM 分类意图:        ║
                    ║  ├─ 完整表单创建      ║
                    ║  ├─ 字段列表生成      ║
                    ║  ├─ 单字段创建        ║
                    ║  ├─ 属性配置          ║
                    ║  └─ 咨询问答          ║
                    ╚═══════════╤═══════════╝
                                │
                    ╔═══════════▼═══════════╗
                    ║  Node 3: prepare      ║
                    ║                       ║
                    ║  从 Skill 缓存获取:   ║
                    ║  ├─ guide.json        ║
                    ║  ├─ 字段 Schema       ║
                    ║  └─ 字段模板          ║
                    ║                       ║
                    ║  从 keywordIndex      ║
                    ║  匹配字段类型         ║
                    ╚═══════════╤═══════════╝
                                │
                    ╔═══════════▼═══════════╗
                    ║  Node 4: generate     ║
                    ║                       ║
                    ║  LLM 生成配置:        ║
                    ║  ┌─────────────────┐  ║
                    ║  │ System Prompt:  │  ║
                    ║  │ + SKILL.md 规则 │  ║
                    ║  │ + RULES.md 约束 │  ║
                    ║  │ + guide.json    │  ║
                    ║  │ + Schema 定义   │  ║
                    ║  │ + 模板 JSON     │  ║
                    ║  └─────────────────┘  ║
                    ║                       ║
                    ║  输出: Structured     ║
                    ║  JSON (Schema 约束)   ║
                    ╚═══════════╤═══════════╝
                                │
                    ╔═══════════▼═══════════╗
                    ║  Node 5: validate     ║
                    ║                       ║
                    ║  jsonschema 校验      ║
                    ║  5 维度:              ║
                    ║  ├─ 表单级 (F1-F9)    ║
                    ║  ├─ 标识符 (V3-V6)    ║
                    ║  ├─ 类型 (T1-T4)      ║
                    ║  ├─ 类型特有 (D1-D16) ║
                    ║  └─ 通用 (C1-C8)      ║
                    ╚═══════════╤═══════════╝
                                │
                    ┌───────────┴───────────┐
                    │                       │
               通过 ▼                  失败 ▼
                    │                       │
                    │              ┌────────┴────────┐
                    │              │ 重试次数 < 3?    │
                    │              └────────┬────────┘
                    │                  是 ↙     ↘ 否
                    │            ┌──────┐    ┌──────────┐
                    │            │回到  │    │标记"需   │
                    │            │Node4 │    │手动调整" │
                    │            │携带  │    │返回      │
                    │            │错误  │    └──────────┘
                    │            └──────┘
                    ▼
          ╔═══════════════════════╗
          ║  Node 6: confirm      ║
          ║                       ║
          ║  展示配置摘要:        ║
          ║  ┌─────────────────┐  ║
          ║  │ 表单名: 请假申请 │  ║
          ║  │ 字段数: 5       │  ║
          ║  │ 布局: 4列       │  ║
          ║  └─────────────────┘  ║
          ║                       ║
          ║  等待用户确认         ║
          ╚═══════════╤═══════════╝
                      │
               ┌──────┴──────┐
               │             │
          确认 ▼         修改 ▼
               │             │
               ▼             └──→ 回到 Node 4
          ╔═══════════════════════╗    (携带修改指令)
          ║  Node 7: submit       ║
          ║                       ║
          ║  返回最终 JSON        ║
          ╚═══════════════════════╝
```

### 4.2 五个 Skill 的工作流差异

```
┌──────────────────────────────────────────────────────────────────────┐
│                     工具工作流对比                                    │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────────┤
│  步骤     │ create   │ modify   │ get      │ clone    │ image        │
├──────────┼──────────┼──────────┼──────────┼──────────┼──────────────┤
│ LLM提取  │ -        │ -        │ formCode │ 源code+  │ -            │
│          │          │          │          │ 新名称   │              │
│ 图片分析  │ ❌       │ ❌       │ ❌       │ ❌       │ ✅ 多模态    │
│ 读源表单  │ ❌       │ ✅       │ ✅       │ ✅       │ ❌           │
│ 读指南    │ ✅       │ ✅       │ ❌       │ ❌       │ ✅           │
│ 读模板    │ ✅       │ ✅       │ ❌       │ ❌       │ ✅           │
│ LLM生成   │ ✅ 全新  │ ✅ 增量  │ ❌       │ ✅ 拷贝+ │ ✅ 图片→配置 │
│           │          │          │          │   修改   │              │
│ 校验模式  │ CREATE   │ UPDATE   │ -        │ CREATE   │ CREATE       │
│ 追问支持  │ ✅       │ ❌       │ ❌       │ ❌       │ ❌           │
│ 提交API   │ create   │ update   │ -        │ create   │ (仅生成)     │
├──────────┴──────────┴──────────┴──────────┴──────────┴──────────────┤
│  共同点：所有工具共享 ToolResult 三态(artifact/ask/error)            │
│  安全声明：is_destructive / is_read_only / is_concurrency_safe      │
│  插件化：新 pack 只需实现 pack.py 的 create_registry()              │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.3 LangGraph GraphState 结构

```python
class GraphState(TypedDict):
    """LangGraph 工作流状态"""

    # ── 输入 ──
    user_input: str              # 用户消息
    conversation_history: list   # 对话历史 [{role, content}]
    compressed_history: str      # 压缩后的对话文本
    conversation_id: str         # 会话 ID (= checkpoint thread_id)
    forward_headers: dict        # 嵌入模式透传的请求头
    current_config: dict | None  # 已有配置(modify 用)

    # ── 意图识别 ──
    tool_name: str               # 选中的工具名
    intent_reason: str           # 选择理由

    # ── 工具执行 ──
    tool_state: dict             # 工具内部 state(透传,含 image_base64 等)
    tool_result: ToolResult | None  # 工具执行结果

    # ── 追问(LangGraph interrupt) ──
    pending_questions: list      # interrupt value
    clarify_answers: dict        # resume value

    # ── SSE 事件队列 ──
    sse_events: list             # 节点产出的事件列表
```

### 4.4 Prompt 策略

```
┌─────────────────────────────────────────────────────────────┐
│                Prompt 策略(工具自治)                          │
│                                                              │
│  Engine 层(零领域知识):                                      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  classify_intent:                                    │   │
│  │    从 registry.all() 动态生成工具清单                 │   │
│  │    "可选工具:\n- create_form: ... \n- modify_form: ..."│   │
│  │    LLM 返回 {"tools": ["tool_name"], "reason": "..."} │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Domain Pack 层(工具内部):                                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  每个 Tool/CompositeTool 自行管理 prompt:             │   │
│  │  - Jinja2 模板 (domains/njmind_form/prompts/)        │   │
│  │  - ctx.prompt_loader.render("njmind_form", name, ...)│   │
│  │  - 或内联 system_prompt (简单工具如 get_form)        │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  优势:新 pack 只需实现 Tool,Engine 零改动                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. 插件自动发现机制

### 5.1 Pack 加载流程

```
    启动时自动加载
    ──────────────────────────────────────────────────

    main.py
       │
       ▼
    domains/__init__.py: load_all_packs()
       │
       ├─ 扫描 domains/*/pack.py
       │
       ├─ 调用每个 pack.py 的 create_registry() + create_prompt_loader()
       │
       ├─ 合并所有 registry → 统一 registry
       │
       └─ 传入 build_graph(registry=registry, ...)

    新增 pack 只需:
    ┌──────────────────────────────────────────────────────────┐
    │  1. 创建 domains/my_pack/ 目录                           │
    │  2. 实现 pack.py:                                        │
    │     def create_registry() -> ToolRegistry:               │
    │         registry = ToolRegistry()                        │
    │         registry.register(MyTool())                      │
    │         return registry                                  │
    │     def create_prompt_loader() -> PromptLoader:          │
    │         ...                                              │
    │  3. 实现 tools/my_tool.py:                               │
    │     class MyTool(Tool):                                  │
    │         name = "my_tool"                                 │
    │         description = "..."                              │
    │         when = "..."                                     │
    │  4. Engine 零改动,自动被 LLM 意图识别                    │
    └──────────────────────────────────────────────────────────┘
```

### 5.2 资源获取方式

```
    旧架构(Skill Consumer + 文件监听)      新架构(Upstream HTTP API)
    ──────────────────────────────────     ────────────────────────────

    skills/                                UpstreamClient
    ├── _shared/RULES.md                   ├── get_guide()     → HTTP
    ├── mcp-schemas/*.json                 ├── get_template()  → HTTP
    ├── mcp-templates/*.json               ├── list_templates()→ HTTP
    └── mcp-guides/guide.json              └── validate_form() → HTTP
                                           HttpAssetClient (SDK 抽象层)
                                           ├── get_guide()
                                           ├── get_template()
                                           ├── validate_artifact()
                                           ├── persist_artifact()
                                           ├── get_form()          ← 新增
                                           └── list_templates()

    优势:
    ┌──────────────────────────────────────────────────────────┐
    │  ✅ 无需 watchdog / 文件同步                              │
    │  ✅ 无需 skills/ 目录挂载                                │
    │  ✅ 上游更新即时生效(无缓存时)                            │
    │  ✅ 适合 Docker / 远程部署                                │
    │  ✅ AssetClient 抽象层,pack 不感知 HTTP 细节              │
    └──────────────────────────────────────────────────────────┘
```

---

## 6. 数据流全链路

### 6.1 新建表单（Create）完整时序

```
用户              Vue 前端            Python 后端              LLM
 │                  │                     │                      │
 │ "创建请假表单"   │                     │                      │
 │─────────────────→│                     │                      │
 │                  │ POST /api/config/   │                      │
 │                  │ generate            │                      │
 │                  │────────────────────→│                      │
 │                  │                     │                      │
 │                  │                     │ ┌─ LangGraph 开始 ─┐ │
 │                  │                     │ │                   │ │
 │                  │                     │ │ load_skill        │ │
 │                  │                     │ │ (读取 SKILL.md)   │ │
 │                  │                     │ │                   │ │
 │  event: stage    │  SSE                │ │ classify_intent   │ │
 │  "正在解析..."   │←───────────────────│←│──────────────────→│ │
 │←─────────────────│                     │ │ "完整表单创建"    │ │
 │                  │                     │ │                   │ │
 │                  │                     │ │ prepare_data      │ │
 │                  │                     │ │ 从缓存取          │ │
 │                  │                     │ │ guide+schema+tmpl │ │
 │                  │                     │ │                   │ │
 │  event: stage    │  SSE                │ │ generate_config   │ │
 │  "正在生成..."   │←───────────────────│←│──────────────────→│ │
 │←─────────────────│                     │ │ 组装 Prompt       │ │
 │                  │                     │ │ + Schema 约束     │ │
 │                  │                     │ │←─────────────────│ │
 │                  │                     │ │ 返回 JSON         │ │
 │                  │                     │ │                   │ │
 │                  │                     │ │ validate_config   │ │
 │                  │                     │ │ jsonschema 校验   │ │
 │                  │                     │ │                   │ │
 │  event: result   │  SSE                │ │ ✅ 通过           │ │
 │  {config JSON}   │←───────────────────│←│                   │ │
 │←─────────────────│                     │ │                   │ │
 │                  │                     │ └─ LangGraph 结束 ─┘ │
 │                  │                     │                      │
 │  "确认提交？"    │  event: confirm     │                      │
 │←─────────────────│←───────────────────│                      │
 │                  │                     │                      │
 │  "确认"          │                     │                      │
 │─────────────────→│ POST /api/config/   │                      │
 │                  │ submit              │                      │
 │                  │────────────────────→│                      │
 │  ✅ 创建成功     │  {formCode: "xx"}   │                      │
 │←─────────────────│←───────────────────│                      │
 │                  │                     │                      │
```

### 6.2 修改表单（Update）

```
用户              Vue 前端            Python 后端
 │                  │                     │
 │ "把工号改成必填" │                     │
 │─────────────────→│                     │
 │                  │ POST /api/config/   │
 │                  │ modify              │
 │                  │ {currentConfig,     │
 │                  │  instruction}       │
 │                  │────────────────────→│
 │                  │                     │
 │                  │                     │ 1. intent: 修改字段属性
 │                  │                     │ 2. 定位工号字段
 │                  │                     │ 3. get_schema 确认约束
 │                  │                     │ 4. 修改 isRequiredField=1
 │                  │                     │ 5. validate(UPDATE 模式)
 │                  │                     │ 6. 返回 diff
 │                  │←────────────────────│
 │  "已更新工号字段" │  SSE: result        │
 │←─────────────────│                     │
 │                  │                     │
```

### 6.3 SSE 事件流协议

```
┌─────────────────────────────────────────────────────────────────┐
│                    SSE 事件流协议                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  event: stage                                                    │
│  data: {"stage":"loading_skill","message":"加载 Skill 规则..."}  │
│                                                                  │
│  event: stage                                                    │
│  data: {"stage":"parsing","message":"正在解析您的描述..."}        │
│                                                                  │
│  event: stage                                                    │
│  data: {"stage":"preparing","message":"获取字段类型定义..."}      │
│                                                                  │
│  event: stage                                                    │
│  data: {"stage":"generating","message":"正在生成表单配置..."}     │
│                                                                  │
│  event: stage                                                    │
│  data: {"stage":"validating","message":"正在校验配置..."}         │
│                                                                  │
│  event: retry  (仅校验失败时)                                    │
│  data: {"attempt":1,"max":3,"errors":[...]}                      │
│                                                                  │
│  event: result                                                   │
│  data: {"config":{...},"valid":true,"summary":"5个字段..."}      │
│                                                                  │
│  event: confirm                                                  │
│  data: {"message":"请确认配置，或告诉我需要修改的地方"}           │
│                                                                  │
│  event: done                                                     │
│  data: {}                                                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 7. API 设计

### 7.1 路由总表

```
┌─────────────────────────────────────────────────────────────────┐
│                       全部路由 (Python FastAPI)                   │
├──────────────┬────────┬─────────────────────────────────────────┤
│  模块         │ 方法   │ 路径                                    │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  统一入口     │ POST   │ /api/config/chat                      │
│  (核心)       │        │ body: {message, conversationId,        │
│              │        │        answers?, image_base64?}          │
│              │        │ response: SSE stream                    │
│              │        │                                         │
│              │        │ 支持:正常消息 / 追问恢复 / 图片上传     │
│              │        │                                         │
│              │ POST   │ /api/config/modify                      │
│              │        │ body: {currentConfig, instruction}      │
│              │        │ response: SSE stream                    │
│              │        │                                         │
│              │ POST   │ /api/config/validate                    │
│              │        │ body: {config}                          │
│              │        │ response: {valid, errors[]}             │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  Skill 查询   │ GET    │ /api/skills/field-types                 │
│  (只读)       │        │ response: [{code, name, keywords...}]   │
│              │        │                                         │
│              │ GET    │ /api/skills/templates                   │
│              │        │ response: [{name, category, desc...}]   │
│              │        │                                         │
│              │ GET    │ /api/skills/templates/{name}            │
│              │        │ response: {template JSON}               │
│              │        │                                         │
│              │ GET    │ /api/skills/schemas                     │
│              │        │ response: [{name, desc...}]             │
│              │        │                                         │
│              │ GET    │ /api/skills/schemas/{name}              │
│              │        │ response: {schema JSON}                 │
│              │        │                                         │
│              │ GET    │ /api/skills/guide                       │
│              │        │ response: {guide JSON}                  │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  会话管理     │ POST   │ /api/conversations                      │
│              │ GET    │ /api/conversations                      │
│              │ GET    │ /api/conversations/{id}                 │
│              │ DELETE │ /api/conversations/{id}                 │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  MCP 协议     │ POST   │ /mcp                                    │
│              │        │ body: JSON-RPC 2.0 request              │
│              │        │ response: JSON-RPC 2.0 response         │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│  健康检查     │ GET    │ /health                                 │
└──────────────┴────────┴─────────────────────────────────────────┘
```

### 7.2 请求/响应示例

```
POST /api/config/generate

Request:
┌──────────────────────────────────────────────────────────┐
│  {                                                        │
│    "description": "创建一个请假申请表，包含请假类型、     │
│     开始日期、结束日期、请假原因",                         │
│    "conversationId": "conv_abc123"                        │
│  }                                                        │
└──────────────────────────────────────────────────────────┘

Response (SSE):
┌──────────────────────────────────────────────────────────┐
│  event: stage                                            │
│  data: {"stage":"generating","message":"正在生成..."}     │
│                                                          │
│  event: result                                           │
│  data: {                                                 │
│    "config": {                                           │
│      "formCode": "qingjia_sqb",                          │
│      "formName": "请假申请表",                            │
│      "formColumnsNumber": 4,                             │
│      "titleFieldKey": "qingjialeixing",                  │
│      "formTitle": "$qingjialeixing$",                    │
│      "formFieldConfigVos": [                             │
│        {                                                 │
│          "fieldTitleKey": "qingjialeixing",              │
│          "fieldTitleText": "请假类型",                    │
│          "formFieldType": 4,                             │
│          "fieldWidth": 12,                               │
│          "optionSettings": [                             │
│            {"optionName":"年假","optionValue":"1"},       │
│            {"optionName":"事假","optionValue":"2"},       │
│            {"optionName":"病假","optionValue":"3"}        │
│          ]                                               │
│        },                                                │
│        {                                                 │
│          "fieldTitleKey": "kaishiriqi",                  │
│          "fieldTitleText": "开始日期",                    │
│          "formFieldType": 2,                             │
│          "fieldWidth": 12                                │
│        },                                                │
│        ...                                               │
│      ],                                                  │
│      "bottomButtons": [...]                              │
│    },                                                    │
│    "valid": true,                                        │
│    "summary": "已生成请假申请表，包含4个字段"             │
│  }                                                       │
└──────────────────────────────────────────────────────────┘
```

---

## 8. MCP 协议层

```
┌─────────────────────────────────────────────────────────────────┐
│                    MCP Protocol (Python mcp SDK)                 │
│                                                                  │
│  AI 工具 (Claude Code / OpenCode / 其他)                         │
│       │                                                          │
│       │  JSON-RPC 2.0 over HTTP                                  │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  MCP Server (Python mcp SDK)                              │   │
│  │  使用 LangGraph StateGraph (非旧 ToolDispatcher)          │   │
│  │                                                           │   │
│  │  ┌─── Tools ──────────────────────────────────────────┐  │   │
│  │  │                                                     │  │   │
│  │  │  get_form_config    → graph.invoke → ToolResult     │  │   │
│  │  │  validate_form      → 校验配置 JSON                 │  │   │
│  │  │  list_templates     → 列出可用模板                  │  │   │
│  │  │  get_template       → 获取指定模板                  │  │   │
│  │  │  get_guide          → 获取配置指南                  │  │   │
│  │  │                                                     │  │   │
│  │  └─────────────────────────────────────────────────────┘  │   │
│  │                                                           │   │
│  │  ┌─── Resources ──────────────────────────────────────┐  │   │
│  │  │                                                     │  │   │
│  │  │  njmind://schemas/form-config    → 表单配置 Schema  │  │   │
│  │  │  njmind://schemas/form-field     → 字段配置 Schema  │  │   │
│  │  │  njmind://guide                  → 配置指南         │  │   │
│  │  │  njmind://field-types            → 字段类型定义     │  │   │
│  │  │                                                     │  │   │
│  │  └─────────────────────────────────────────────────────┘  │   │
│  │                                                           │   │
│  │  数据来源: Skill Consumer 内存缓存                         │
│  │  (与 REST API 共享同一份缓存)                              │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  调用示例 (Claude Code):                                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  mcp__llm-form-modeler__get_form_config({                │   │
│  │    description: "创建一个包含姓名和手机号的表单"          │   │
│  │  })                                                      │   │
│  │  // → 返回 FormConfig JSON                               │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 9. 用户身份 & 对话历史

### 9.1 用户身份：无登录，透传上层身份

```
┌─────────────────────────────────────────────────────────────────┐
│                    身份认证策略                                   │
│                                                                  │
│  本项目不做登录。用户身份由上层系统（主系统）透传。               │
│                                                                  │
│  透传方式:                                                       │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  方式 1: HTTP Header (推荐)                               │   │
│  │  ─────────────────────────────                            │   │
│  │  主系统 → 前端 → 后端                                     │   │
│  │                                                           │   │
│  │  前端请求时携带:                                           │   │
│  │  X-User-Id: user_12345                                    │   │
│  │  X-User-Name: 张三                                        │   │
│  │  X-Tenant-Id: tenant_001   (可选，多租户隔离)             │   │
│  │                                                           │   │
│  │  方式 2: URL 参数 (嵌入模式)                              │   │
│  │  ─────────────────────────────                            │   │
│  │  iframe src="https://modeler.example.com/                 │   │
│  │    ?embed=true                                            │   │
│  │    &userId=user_12345                                     │   │
│  │    &userName=张三"                                        │   │
│  │                                                           │   │
│  │  方式 3: postMessage (嵌入模式动态传递)                   │   │
│  │  ─────────────────────────────────────                    │   │
│  │  window.postMessage({                                     │   │
│  │    type: 'MODELER_INIT',                                  │   │
│  │    payload: {                                             │   │
│  │      userId: 'user_12345',                                │   │
│  │      userName: '张三',                                    │   │
│  │      tenantId: 'tenant_001'                               │   │
│  │    }                                                      │   │
│  │  }, '*');                                                 │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  后端处理:                                                       │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  1. 从 Header / Query / postMessage 提取用户信息          │   │
│  │  2. 中间件注入 request.state.user = {userId, userName}    │   │
│  │  3. 所有数据操作按 userId 隔离                            │   │
│  │  4. 不校验身份真实性（信任上层）                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  优先级: Header > postMessage > URL 参数                        │
└─────────────────────────────────────────────────────────────────┘
```

### 9.2 对话历史存储：SQLite

```
┌─────────────────────────────────────────────────────────────────┐
│                    存储方案                                       │
│                                                                  │
│  选择: SQLite (Python 内置，零依赖)                              │
│                                                                  │
│  为什么:                                                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  ✅ Python 内置 sqlite3，无需额外安装                     │   │
│  │  ✅ LangGraph Checkpoint 原生支持 SQLite                 │   │
│  │  ✅ 单文件部署，Docker Volume 挂载即可                   │   │
│  │  ✅ 数据量不大（一个对话 ~10-50 条消息，每条 ~2-10KB）   │   │
│  │  ✅ 按 userId 隔离，单用户数据量很小                     │   │
│  │  ✅ 后续可平滑迁移到 PostgreSQL                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  文件位置:                                                       │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  data/                                                    │   │
│  │  ├── conversations.db    ← 对话历史                      │   │
│  │  └── checkpoints.db      ← LangGraph Checkpoint          │   │
│  │                                                           │   │
│  │  Docker: Volume 挂载到 /app/data/                        │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 9.3 数据模型

```
┌─────────────────────────────────────────────────────────────────┐
│                    数据库表结构                                   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  conversations (会话表)                                   │   │
│  │  ─────────────────────────                                │   │
│  │  id              TEXT PRIMARY KEY   -- UUID               │   │
│  │  user_id         TEXT NOT NULL      -- 用户ID (上层透传)  │   │
│  │  tenant_id       TEXT               -- 租户ID (可选)      │   │
│  │  title           TEXT               -- 会话标题 (自动生成) │   │
│  │  skill_name      TEXT DEFAULT 'create'                    │   │
│  │  current_config  TEXT               -- 当前配置 JSON      │   │
│  │  created_at      DATETIME                                │   │
│  │  updated_at      DATETIME                                │   │
│  │                                                           │   │
│  │  INDEX: (user_id, updated_at DESC)                        │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  messages (消息表)                                        │   │
│  │  ─────────────────────                                    │   │
│  │  id              TEXT PRIMARY KEY   -- UUID               │   │
│  │  conversation_id TEXT NOT NULL      -- FK → conversations │   │
│  │  role            TEXT NOT NULL      -- 'user' | 'assistant'│   │
│  │  content         TEXT NOT NULL      -- 消息内容            │   │
│  │  config_snapshot TEXT               -- 配置快照 (JSON)     │   │
│  │  metadata        TEXT               -- 扩展信息 (JSON)     │   │
│  │  created_at      DATETIME                                │   │
│  │                                                           │   │
│  │  INDEX: (conversation_id, created_at)                     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  关系:                                                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  conversations 1 ──── N messages                          │   │
│  │                                                           │   │
│  │  user_123 的会话:                                         │   │
│  │  ┌────────────────────────────────────────────────────┐  │   │
│  │  │ conv_001: "请假申请表"                              │  │   │
│  │  │   ├── msg_001: user      "创建请假申请表"           │  │   │
│  │  │   ├── msg_002: assistant "已生成配置..." + config   │  │   │
│  │  │   ├── msg_003: user      "把工号改成必填"           │  │   │
│  │  │   └── msg_004: assistant "已更新..." + config       │  │   │
│  │  │                                                     │  │   │
│  │  │ conv_002: "员工信息表"                              │  │   │
│  │  │   ├── msg_005: user      "创建员工信息表"           │  │   │
│  │  │   └── msg_006: assistant "已生成配置..." + config   │  │   │
│  │  └────────────────────────────────────────────────────┘  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 9.4 对话历史 API

```
┌─────────────────────────────────────────────────────────────────┐
│                    对话历史 API (更新)                            │
├──────────────┬────────┬─────────────────────────────────────────┤
│  操作         │ 方法   │ 路径                                    │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  创建会话     │ POST   │ /api/conversations                      │
│              │        │ Header: X-User-Id: user_123             │
│              │        │ body: {title?, skillName?}               │
│              │        │ response: {id, title, createdAt}        │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  我的会话列表 │ GET    │ /api/conversations                      │
│              │        │ Header: X-User-Id: user_123             │
│              │        │ response: [{id, title, updatedAt,       │
│              │        │           lastMessage, configSummary}]  │
│              │        │ (只返回当前用户的会话)                   │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  获取会话详情 │ GET    │ /api/conversations/{id}                 │
│              │        │ Header: X-User-Id: user_123             │
│              │        │ response: {                              │
│              │        │   conversation: {...},                  │
│              │        │   messages: [                           │
│              │        │     {role:'user', content:'...'},       │
│              │        │     {role:'assistant', content:'...',   │
│              │        │      configSnapshot: {...}}             │
│              │        │   ],                                    │
│              │        │   currentConfig: {...}                  │
│              │        │ }                                       │
│              │        │ (校验 userId 归属)                      │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  删除会话     │ DELETE │ /api/conversations/{id}                 │
│              │        │ Header: X-User-Id: user_123             │
│              │        │ (校验 userId 归属，级联删除 messages)    │
│              │        │                                         │
├──────────────┼────────┼─────────────────────────────────────────┤
│              │        │                                         │
│  生成配置时   │ POST   │ /api/config/generate                    │
│  自动保存     │        │ Header: X-User-Id: user_123             │
│              │        │ body: {conversationId, description}     │
│              │        │                                         │
│              │        │ → 自动保存 user message                 │
│              │        │ → 自动保存 assistant message            │
│              │        │ → 自动更新 conversation.currentConfig   │
│              │        │ → 自动更新 conversation.title (首次)    │
│              │        │                                         │
└──────────────┴────────┴─────────────────────────────────────────┘
```

### 9.5 对话历史数据流

```
用户              前端              后端 (Python)           SQLite
 │                  │                     │                    │
 │ 打开页面         │                     │                    │
 │─────────────────→│                     │                    │
 │                  │ GET /api/           │                    │
 │                  │ conversations       │                    │
 │                  │ X-User-Id: user_123 │                    │
 │                  │────────────────────→│                    │
 │                  │                     │ SELECT * FROM      │
 │                  │                     │ conversations      │
 │                  │                     │ WHERE user_id=?    │
 │                  │                     │───────────────────→│
 │                  │                     │←───────────────────│
 │  会话列表        │  [{conv1}, {conv2}] │                    │
 │←─────────────────│←────────────────────│                    │
 │                  │                     │                    │
 │ 点击会话1        │                     │                    │
 │─────────────────→│                     │                    │
 │                  │ GET /api/           │                    │
 │                  │ conversations/conv1 │                    │
 │                  │ X-User-Id: user_123 │                    │
 │                  │────────────────────→│                    │
 │                  │                     │ SELECT messages    │
 │                  │                     │ WHERE conv_id=?    │
 │                  │                     │───────────────────→│
 │                  │                     │←───────────────────│
 │  历史消息        │  {messages, config} │                    │
 │←─────────────────│←────────────────────│                    │
 │                  │                     │                    │
 │ "创建请假表单"   │                     │                    │
 │─────────────────→│                     │                    │
 │                  │ POST /api/config/   │                    │
 │                  │ generate            │                    │
 │                  │ {conversationId,    │                    │
 │                  │  description}       │                    │
 │                  │ X-User-Id: user_123 │                    │
 │                  │────────────────────→│                    │
 │                  │                     │ INSERT message     │
 │                  │                     │ (role='user')      │
 │                  │                     │───────────────────→│
 │                  │                     │                    │
 │                  │                     │ LangGraph 生成     │
 │                  │                     │ ...                │
 │                  │                     │                    │
 │                  │                     │ INSERT message     │
 │                  │                     │ (role='assistant', │
 │                  │                     │  configSnapshot)   │
 │                  │                     │───────────────────→│
 │                  │                     │                    │
 │                  │                     │ UPDATE conversation│
 │                  │                     │ .currentConfig     │
 │                  │                     │───────────────────→│
 │                  │                     │                    │
 │  SSE: result     │  SSE                │                    │
 │←─────────────────│←────────────────────│                    │
 │                  │                     │                    │
```

### 9.6 LangGraph Checkpoint 集成

```
┌─────────────────────────────────────────────────────────────────┐
│                    LangGraph 状态持久化                           │
│                                                                  │
│  LangGraph 使用 SQLite Checkpoint 保存工作流状态:               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  from langgraph.checkpoint.sqlite import SqliteSaver      │   │
│  │                                                           │   │
│  │  # 初始化 Checkpoint 存储                                 │   │
│  │  checkpointer = SqliteSaver.from_conn_string(            │   │
│  │      "data/checkpoints.db"                                │   │
│  │  )                                                        │   │
│  │                                                           │   │
│  │  # 编译工作流时注入 checkpointer                          │   │
│  │  app = workflow.compile(checkpointer=checkpointer)        │   │
│  │                                                           │   │
│  │  # 运行时传入 thread_id (= conversation_id)              │   │
│  │  config = {"configurable": {"thread_id": conversation_id}}│   │
│  │  result = app.invoke(input, config)                       │   │
│  │                                                           │   │
│  │  # 恢复对话时，LangGraph 自动从 Checkpoint 加载状态      │   │
│  │  # 包括: conversation_history, current_config,            │   │
│  │  #        validation_errors, retry_count 等               │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  双存储分工:                                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                                                           │   │
│  │  conversations.db          checkpoints.db                 │   │
│  │  ────────────────          ──────────────                 │   │
│  │  • 会话元数据              • LangGraph 工作流状态         │   │
│  │  • 消息历史 (展示用)       • 中间节点状态                 │   │
│  │  • 当前配置快照            • 对话上下文 (LLM 用)          │   │
│  │  • 用户归属                • Checkpoint 数据              │   │
│  │  • 会话列表查询            • 工作流恢复                   │   │
│  │                                                           │   │
│  │  conversations.db → 前端展示、会话列表、消息历史          │   │
│  │  checkpoints.db   → LangGraph 内部状态恢复               │   │
│  │                                                           │   │
│  │  两者通过 conversation_id 关联                            │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 9.7 嵌入模式下的对话历史

```
┌─────────────────────────────────────────────────────────────────┐
│                    嵌入模式对话历史策略                            │
│                                                                  │
│  嵌入模式下，对话历史的特殊性:                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                                                           │   │
│  │  1. 用户从主系统打开聊天窗口                              │   │
│  │     → postMessage 传入 userId                            │   │
│  │     → 前端调用 GET /api/conversations?userId=xxx         │   │
│  │     → 如果有历史会话，显示最近一个；没有则自动创建        │   │
│  │                                                           │   │
│  │  2. 用户关闭聊天窗口                                     │   │
│  │     → 对话已实时保存到 SQLite                            │   │
│  │     → 下次打开自动恢复                                   │   │
│  │                                                           │   │
│  │  3. 嵌入模式不显示会话列表侧边栏                         │   │
│  │     → 默认使用最近一个会话                               │   │
│  │     → 可选：提供"新建会话"按钮                           │   │
│  │                                                           │   │
│  │  4. 主系统可以传入 formCode 关联                         │   │
│  │     → 按 formCode 查找历史会话                           │   │
│  │     → 实现"同一个表单的对话延续"                         │   │
│  │                                                           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  嵌入模式会话恢复流程:                                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                                                           │   │
│  │  主系统 postMessage:                                      │   │
│  │  {                                                        │   │
│  │    type: 'MODELER_INIT',                                  │   │
│  │    payload: {                                             │   │
│  │      userId: 'user_123',                                  │   │
│  │      formCode: 'leave_apply'  ← 可选                     │   │
│  │    }                                                      │   │
│  │  }                                                        │   │
│  │       │                                                    │   │
│  │       ▼                                                    │   │
│  │  前端收到后:                                               │   │
│  │  1. 调用 GET /api/conversations?userId=user_123           │   │
│  │     &formCode=leave_apply                                  │   │
│  │  2. 如果有 → 加载最近会话的历史消息                        │   │
│  │  3. 如果没有 → POST /api/conversations 创建新会话          │   │
│  │  4. 展示对话界面                                           │   │
│  │                                                           │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10. 前端设计

### 10.1 两种运行模式

前端支持两种运行模式，通过 URL 参数或环境变量切换：

```
┌─────────────────────────────────────────────────────────────────┐
│                    前端运行模式                                   │
│                                                                  │
│  模式 A: 独立模式 (Standalone)                                   │
│  ─────────────────────────────                                   │
│  独立访问，完整三栏布局                                            │
│  URL: https://modeler.example.com/                               │
│  场景: 开发者/管理员独立使用                                       │
│                                                                  │
│  模式 B: 嵌入模式 (Embedded / IM 聊天窗口)                       │
│  ─────────────────────────────────────────                       │
│  嵌入主系统，IM 聊天窗口风格                                       │
│  URL: https://modeler.example.com/?embed=true                    │
│  或: <iframe src="https://modeler.example.com/?embed=true">      │
│  场景: 用户在低码平台中，边看表单边对话生成配置                    │
│                                                                  │
│  判断逻辑:                                                       │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  if (url.query.embed === 'true'                          │   │
│  │      || window.parent !== window                          │   │
│  │      || env.VITE_EMBED_MODE === 'true') {                │   │
│  │    → 嵌入模式                                             │   │
│  │  } else {                                                │   │
│  │    → 独立模式                                             │   │
│  │  }                                                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 10.2 模式 A：独立模式（全页面）

```
桌面端 (≥1024px):
┌─────────────────────────────────────────────────────────────────┐
│  LLM Form Modeler                        [新建会话] [导出] [⚙]  │
├──────────┬──────────────────────────────┬───────────────────────┤
│          │                              │                       │
│  会话列表 │     对话区域                  │    JSON 输出区域      │
│  (侧边栏) │     (对话气泡)               │    (Monaco Editor)   │
│          │                              │                       │
│  ┌─────┐ │  ┌────────────────────────┐  │  ┌─────────────────┐ │
│  │会话1│ │  │ 🤖 已为您生成请假申请表 │  │  │ {               │ │
│  │请假  │ │  │    配置，包含 4 个字段  │  │  │   "formCode":   │ │
│  │申请  │ │  │                        │  │  │   "qingjia_sqb",│ │
│  ├─────┤ │  │  ┌──────────────────┐  │  │  │   "formName":   │ │
│  │会话2│ │  │  │ 表单名: 请假申请表│  │  │  │   "请假申请表",  │ │
│  │员工  │ │  │  │ 字段数: 4        │  │  │  │   ...           │ │
│  │信息  │ │  │  │ 布局: 4列        │  │  │  │ }               │ │
│  ├─────┤ │  │  └──────────────────┘  │  │  │                 │ │
│  │会话3│ │  │                        │  │  │  [复制JSON]      │ │
│  │...   │ │  │                        │  │  │  [下载文件]      │ │
│  └─────┘ │  │                        │  │  └─────────────────┘ │
│          │                              │                       │
│          ├──────────────────────────────┴───────────────────────┤
│          │  📎 描述你需要的表单...                        [发送] │
│          └──────────────────────────────────────────────────────┘
│
│  侧边栏 15% │ 对话区 50% │ JSON区 35%
└─────────────────────────────────────────────────────────────────┘
```

### 9.3 模式 B：嵌入模式（IM 聊天窗口）

这是核心需求——像 IM 聊天窗口一样嵌入主系统。

#### 嵌入方式

```
┌─────────────────────────────────────────────────────────────────┐
│                    主系统 (低码平台)                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  主系统页面 (表单设计器 / 业务页面)                        │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                                          │   │
│  │                                              ┌──────────┐│   │
│  │                                              │ 💬       ││   │
│  │                                              │ 浮动按钮  ││   │
│  │                                              └────┬─────┘│   │
│  │                                                   │ 点击  │   │
│  │                                                   ▼       │   │
│  │                                     ┌─────────────────────┐│   │
│  │                                     │  LLM Form Modeler   ││   │
│  │                                     │─────────────────────││   │
│  │                                     │                     ││   │
│  │                                     │  🤖 您好，请描述    ││   │
│  │                                     │     您需要的表单    ││   │
│  │                                     │                     ││   │
│  │                                     │  👤 创建请假申请表  ││   │
│  │                                     │                     ││   │
│  │                                     │  🤖 已生成配置:     ││   │
│  │                                     │  ┌───────────────┐  ││   │
│  │                                     │  │ 表单: 请假申请 │  ││   │
│  │                                     │  │ 字段: 4个      │  ││   │
│  │                                     │  │ [查看JSON ▼]   │  ││   │
│  │                                     │  └───────────────┘  ││   │
│  │                                     │  [应用配置] [复制]  ││   │
│  │                                     │─────────────────────││   │
│  │                                     │  [输入框...] [发送] ││   │
│  │                                     └─────────────────────┘│   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

#### 三种嵌入方案

```
┌─────────────────────────────────────────────────────────────────┐
│                    嵌入方案对比                                   │
├──────────────┬──────────────────┬───────────────┬───────────────┤
│              │ iframe (推荐)    │ Web Component │ JS SDK        │
├──────────────┼──────────────────┼───────────────┼───────────────┤
│ 隔离性       │ ✅ 完全隔离      │ ✅ Shadow DOM │ ❌ 共享上下文 │
│ 样式冲突     │ ✅ 无冲突        │ ✅ 无冲突     │ ⚠️ 可能冲突   │
│ 通信方式     │ postMessage      │ 自定义事件    │ 直接调用      │
│ 部署复杂度   │ ✅ 简单          │ ⚠️ 中等      │ ⚠️ 中等      │
│ 跨域支持     │ ✅ 天然支持      │ ⚠️ 需同源    │ ⚠️ 需同源    │
│ 主系统集成度 │ ⚠️ 需要桥接     │ ✅ 像原生组件 │ ✅ 完全控制   │
├──────────────┼──────────────────┼───────────────┼───────────────┤
│ 推荐度       │ ✅ 首选          │ 备选          │ 不推荐        │
└──────────────┴──────────────────┴───────────────┴───────────────┘
```

#### iframe 嵌入方案（推荐）

```
主系统侧 (宿主):
┌─────────────────────────────────────────────────────────────────┐
│  <!-- 1. 引入 SDK 脚本 -->                                       │
│  <script src="https://modeler.example.com/embed.js"></script>   │
│                                                                  │
│  <!-- 2. 初始化 -->                                              │
│  <script>                                                        │
│    const modeler = new LLMFormModeler({                         │
│      baseUrl: 'https://modeler.example.com',                    │
│      position: 'bottom-right',  // 浮动按钮位置                  │
│      theme: 'light',            // 主题                         │
│      locale: 'zh-CN',           // 语言                         │
│      onConfigGenerated: (config) => {                           │
│        // 收到生成的配置，应用到表单设计器                        │
│        formDesigner.setConfig(config);                           │
│      }                                                          │
│    });                                                           │
│  </script>                                                       │
│                                                                  │
│  embed.js 内部自动:                                              │
│  1. 创建浮动按钮 DOM                                             │
│  2. 点击后创建 iframe                                            │
│  3. 监听 postMessage 通信                                        │
└─────────────────────────────────────────────────────────────────┘

通信协议 (postMessage):
┌─────────────────────────────────────────────────────────────────┐
│                    主系统 ←→ 聊天窗口 通信                        │
│                                                                  │
│  主系统 → 聊天窗口:                                              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  // 传递当前表单上下文                                    │   │
│  │  iframe.contentWindow.postMessage({                      │   │
│  │    type: 'MODELER_INIT',                                │   │
│  │    payload: {                                            │   │
│  │      currentFormConfig: {...},  // 当前表单配置(可选)     │   │
│  │      formCode: 'leave_apply',   // 当前表单编码(可选)    │   │
│  │      theme: 'light',                                     │   │
│  │      locale: 'zh-CN'                                     │   │
│  │    }                                                     │   │
│  │  }, '*');                                                │   │
│  │                                                          │   │
│  │  // 主动触发打开/关闭                                     │   │
│  │  iframe.contentWindow.postMessage({                      │   │
│  │    type: 'MODELER_TOGGLE',                               │   │
│  │    payload: { visible: true }                            │   │
│  │  }, '*');                                                │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  聊天窗口 → 主系统:                                              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  // 配置生成完成                                          │   │
│  │  window.parent.postMessage({                             │   │
│  │    type: 'MODELER_CONFIG_GENERATED',                     │   │
│  │    payload: {                                            │   │
│  │      config: {...},       // 完整的 FormConfig JSON      │   │
│  │      summary: '已生成请假申请表'                          │   │
│  │    }                                                     │   │
│  │  }, '*');                                                │   │
│  │                                                          │   │
│  │  // 用户点击"应用配置"                                    │   │
│  │  window.parent.postMessage({                             │   │
│  │    type: 'MODELER_CONFIG_APPLY',                         │   │
│  │    payload: { config: {...} }                            │   │
│  │  }, '*');                                                │   │
│  │                                                          │   │
│  │  // 请求关闭聊天窗口                                      │   │
│  │  window.parent.postMessage({                             │   │
│  │    type: 'MODELER_CLOSE',                                │   │
│  │    payload: {}                                           │   │
│  │  }, '*');                                                │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

#### 嵌入模式 UI 布局

```
嵌入模式 - 浮动按钮:
┌──────────────────────────────────────────┐
│                                          │
│                                          │
│                                          │
│                                          │
│                                          │
│                                          │
│                                      ┌──┐│
│                                      │💬││  ← 48x48 圆形按钮
│                                      └──┘│     右下角固定定位
└──────────────────────────────────────────┘

嵌入模式 - 聊天窗口展开:
┌──────────────────────────────────────────┐
│                                      ┌──┐│
│                                      │ ✕││  ← 关闭按钮
│                    ┌─────────────────┤  ││
│                    │ LLM 表单助手     ├──┘│  ← 标题栏
│                    │─────────────────│   │
│                    │                 │   │
│                    │ 🤖 您好，请描述  │   │  ← 对话区域
│                    │    您需要的表单  │   │     (自动滚动)
│                    │                 │   │
│                    │ 👤 创建请假表    │   │
│                    │                 │   │
│                    │ 🤖 已生成配置:  │   │
│                    │ ┌─────────────┐ │   │
│                    │ │ 表单: 请假  │ │   │  ← 配置摘要卡片
│                    │ │ 字段: 4个   │ │   │
│                    │ │ [查看JSON]  │ │   │
│                    │ └─────────────┘ │   │
│                    │                 │   │
│                    │─────────────────│   │
│                    │ [输入...]  [发送]│   │  ← 输入区域
│                    └─────────────────┘   │
│                                          │
└──────────────────────────────────────────┘

尺寸: 宽 380px, 高 560px (可拖拽调整)
位置: 右下角，距边缘 16px
动画: 展开/收起带 transition
```

#### 嵌入模式 - JSON 查看弹窗

```
嵌入模式下，点击"查看 JSON"弹出抽屉:

┌──────────────────────────────────────────┐
│  配置 JSON                         [✕]   │
│──────────────────────────────────────────│
│                                          │
│  ┌────────────────────────────────────┐  │
│  │ {                                  │  │
│  │   "formCode": "qingjia_sqb",       │  │
│  │   "formName": "请假申请表",         │  │
│  │   "formFieldConfigVos": [          │  │
│  │     {                              │  │
│  │       "fieldTitleText": "请假类型", │  │
│  │       "formFieldType": 4,          │  │
│  │       ...                          │  │
│  │     }                              │  │
│  │   ]                                │  │
│  │ }                                  │  │
│  └────────────────────────────────────┘  │
│                                          │
│  [复制 JSON]  [应用配置]  [下载文件]      │
│                                          │
└──────────────────────────────────────────┘
```

### 9.4 模式对比

```
┌─────────────────────────────────────────────────────────────────┐
│                    独立模式 vs 嵌入模式                            │
├──────────────┬──────────────────────┬───────────────────────────┤
│              │ 独立模式              │ 嵌入模式                   │
├──────────────┼──────────────────────┼───────────────────────────┤
│ 布局         │ 三栏 (侧边栏+对话+JSON)│ 单栏 (纯对话)             │
│ 会话管理     │ ✅ 侧边栏多会话       │ ❌ 单会话 (简化)           │
│ JSON 展示    │ ✅ 右侧常驻面板       │ ⚠️ 弹窗/抽屉查看          │
│ 导航栏       │ ✅ 完整 Header        │ ⚠️ 简化标题栏              │
│ 与主系统通信 │ ❌ 无                 │ ✅ postMessage             │
│ "应用配置"   │ ❌ 手动复制/下载      │ ✅ 一键推送到主系统         │
│ 浮动按钮     │ ❌ 无                 │ ✅ 右下角浮动               │
│ 窗口尺寸     │ 全屏                  │ 380x560px (可拖拽)         │
│ 部署方式     │ 独立域名              │ iframe 嵌入主系统          │
└──────────────┴──────────────────────┴───────────────────────────┘
```

### 9.5 Vue 组件树

```
App.vue
├── 路由判断: ?embed=true → EmbeddedLayout / StandaloneLayout
│
├── layouts/
│   ├── StandaloneLayout.vue          ← 模式 A: 独立全页面
│   │   ├── AppHeader.vue
│   │   │   ├── Logo
│   │   │   ├── NewConversationButton
│   │   │   ├── ExportButton
│   │   │   └── SettingsButton
│   │   ├── ConversationSider.vue     ← 侧边栏会话列表
│   │   │   └── ConversationItem.vue × N
│   │   └── MainContent.vue
│   │       ├── ChatPanel.vue
│   │       └── JsonPanel.vue         ← 常驻 JSON 面板
│   │
│   └── EmbeddedLayout.vue            ← 模式 B: IM 聊天窗口
│       ├── EmbeddedHeader.vue        ← 简化标题栏 (标题 + 关闭)
│       ├── ChatPanel.vue             ← 复用同一个对话组件
│       │   ├── MessageList.vue
│       │   │   ├── UserMessage.vue
│       │   │   └── AssistantMessage.vue
│       │   │       ├── TextContent.vue
│       │   │       ├── ConfigSummaryCard.vue  ← 配置摘要卡片
│       │   │       │   ├── [查看JSON] → 弹出 JsonDrawer
│       │   │       │   └── [应用配置] → postMessage 推送
│       │   │       └── StreamingProgress.vue
│       │   └── ChatInput.vue
│       ├── JsonDrawer.vue            ← 抽屉式 JSON 查看
│       │   └── MonacoEditor (只读)
│       └── FloatingButton.vue        ← 浮动按钮 (宿主侧)
│
├── components/                       ← 两种模式共享
│   ├── chat/
│   │   ├── ChatPanel.vue             ← 核心对话组件 (两种模式复用)
│   │   ├── ChatInput.vue
│   │   ├── MessageList.vue
│   │   ├── UserMessage.vue
│   │   ├── AssistantMessage.vue
│   │   ├── ConfigSummaryCard.vue     ← 配置摘要卡片
│   │   └── StreamingProgress.vue
│   └── json/
│       ├── JsonPanel.vue             ← 独立模式: 常驻面板
│       └── JsonDrawer.vue            ← 嵌入模式: 抽屉弹窗
│
├── embed/                            ← 嵌入模式专属
│   ├── bridge.ts                     ← postMessage 通信桥
│   ├── FloatingButton.vue            ← 浮动按钮组件
│   └── embed.js                      ← 给主系统引入的 SDK 入口
│                                       (编译为独立 JS 文件)
│
├── stores/ (Pinia)
│   ├── conversation.store.ts
│   └── config.store.ts
│
└── composables/ (Vue composables)
    ├── useSSE.ts
    ├── useConversation.ts
    ├── useConfig.ts
    └── useEmbedBridge.ts             ← 嵌入模式通信 hook
```

### 9.6 embed.js SDK（给主系统引入）

```
主系统引入方式:
┌─────────────────────────────────────────────────────────────────┐
│  <script src="https://modeler.example.com/embed.js"></script>   │
│  <script>                                                        │
│    const modeler = new LLMFormModeler({                         │
│      baseUrl: 'https://modeler.example.com',                    │
│      position: 'bottom-right',                                  │
│      theme: 'light',                                            │
│      onConfigGenerated: (config) => {                           │
│        formDesigner.setConfig(config);  // 应用到表单设计器      │
│      },                                                         │
│      onConfigApply: (config) => {                               │
│        formDesigner.applyConfig(config);                        │
│      }                                                          │
│    });                                                           │
│                                                                  │
│    // API:                                                       │
│    modeler.open();      // 打开聊天窗口                          │
│    modeler.close();     // 关闭聊天窗口                          │
│    modeler.toggle();    // 切换显示/隐藏                         │
│    modeler.destroy();   // 销毁实例                              │
│    modeler.setContext({ currentFormConfig: {...} });             │
│                         // 传递当前表单上下文                     │
│  </script>                                                       │
│                                                                  │
│  embed.js 内部实现:                                              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  class LLMFormModeler {                                  │   │
│  │    constructor(options) {                                │   │
│  │      this.baseUrl = options.baseUrl;                     │   │
│  │      this.iframe = null;                                 │   │
│  │      this.floatingBtn = null;                            │   │
│  │      this._createFloatingButton();                       │   │
│  │      this._initMessageListener();                        │   │
│  │    }                                                     │   │
│  │                                                          │   │
│  │    _createFloatingButton() {                             │   │
│  │      // 创建浮动按钮 DOM                                  │   │
│  │      // 点击 → _createIframe()                           │   │
│  │    }                                                     │   │
│  │                                                          │   │
│  │    _createIframe() {                                     │   │
│  │      // 创建 iframe                                      │   │
│  │      // src = baseUrl + '?embed=true'                    │   │
│  │      // 监听 postMessage                                 │   │
│  │    }                                                     │   │
│  │                                                          │   │
│  │    _initMessageListener() {                              │   │
│  │      // 监听 MODELER_CONFIG_GENERATED                    │   │
│  │      // 监听 MODELER_CONFIG_APPLY                        │   │
│  │      // 监听 MODELER_CLOSE                               │   │
│  │      // → 回调 options.onConfigGenerated 等              │   │
│  │    }                                                     │   │
│  │  }                                                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10. 部署

### 10.1 Docker Compose

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Compose                                │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  nginx (port 80)                                        │    │
│  │  ─────────────────────                                  │    │
│  │  /             → Vue 静态资源 (dist/)                    │    │
│  │  /embed.js     → Vue 打包的 embed SDK (独立入口)         │    │
│  │  /api/*        → proxy_pass → backend:8000              │    │
│  │  /mcp          → proxy_pass → backend:8000              │    │
│  │                                                          │    │
│  │  CORS Headers:                                           │    │
│  │  Access-Control-Allow-Origin: *  (支持跨域 iframe 嵌入)  │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────┐       │
│  │  backend (port 8000)                                  │       │
│  │  ──────────────────                                   │       │
│  │  Python 3.11 + FastAPI + uvicorn                      │       │
│  │  (REST API + MCP + LangGraph + Skill Consumer)        │       │
│  │                                                       │       │
│  │  Volume: /app/skills/ (readonly)                      │       │
│  └──────────────────────────────────────────────────────┘       │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────┐       │
│  │  skills-volume                                        │       │
│  │  ─────────────────                                    │       │
│  │  由 njmind-modeler 编译输出，同步方式:                │       │
│  │  • CI/CD 复制 / Volume 挂载 / init container 拉取    │       │
│  └──────────────────────────────────────────────────────┘       │
│                                                                  │
│  .env:                                                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  LLM_BASE_URL=https://api.openai.com/v1                  │   │
│  │  LLM_API_KEY=sk-xxx                                      │   │
│  │  LLM_MODEL=gpt-4o                                        │   │
│  │  SKILLS_DIR=/app/skills                                  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**就两个容器：nginx + python backend，完事。**

### 10.2 启动顺序

```
    docker-compose up
           │
           ▼
    ┌──────────────┐
    │ 1. skills     │  Volume 就绪
    │    volume     │
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ 2. backend   │  FastAPI + uvicorn
    │  (Python)    │  加载 Skill 文件到缓存
    └──────┬───────┘
           │ health check OK
           ▼
    ┌──────────────┐
    │ 3. nginx     │  反代 + 静态资源
    └──────────────┘
```

---

## 11. 项目目录结构

```
llm-to-modler/
│
├── backend/                             # Python 后端
│   ├── pyproject.toml
│   ├── Dockerfile
│   │
│   └── src/
│       ├── main.py                      # FastAPI 入口
│       │
│       ├── api/                         # 路由层
│       │   ├── config.py                # /api/config/chat (统一 SSE 入口)
│       │   ├── conversations.py         # /api/conversations/*
│       │   ├── skills.py                # /api/skills/*
│       │   ├── health.py                # /health
│       │   └── sse.py                   # StreamManager / SSEEvent
│       │
│       ├── engine/                      # Engine 层 (零领域知识)
│       │   ├── graph.py                 # StateGraph 构建 + compile
│       │   ├── graph_state.py           # GraphState TypedDict
│       │   ├── nodes.py                 # classify_intent / execute_tool / handle_result
│       │   ├── stream.py                # graph.stream → SSE 桥接
│       │   ├── compression.py           # 压缩 sidechain + build_compressed_history
│       │   ├── conversation.py          # ConversationManager
│       │   ├── prompt_loader.py         # Jinja2 模板加载
│       │   ├── logging_filter.py        # 日志凭证脱敏
│       │   └── dispatcher.py            # [遗留] ToolDispatcher (MCP 兼容)
│       │
│       ├── sdk/                         # SDK 层 (协议定义)
│       │   ├── tool.py                  # Tool / CompositeTool / ToolResult / AskSpec
│       │   ├── registry.py              # ToolRegistry
│       │   ├── asset_client.py          # AssetClient 抽象
│       │   └── sanitize.py              # 文本脱敏
│       │
│       ├── domains/                     # Domain Pack 层 (插件化)
│       │   ├── __init__.py              # load_all_packs()
│       │   └── njmind_form/             # 表单配置 pack
│       │       ├── pack.py              # create_registry() + create_prompt_loader()
│       │       ├── models.py            # ParsedField 等数据模型
│       │       ├── prompts/             # Jinja2 模板
│       │       └── tools/
│       │           ├── create_form.py   # 6步管线
│       │           ├── modify_form.py   # 3步管线
│       │           ├── get_form.py      # 查询
│       │           ├── clone_form.py    # 复制
│       │           ├── image_form.py    # 图片识别
│       │           ├── chat.py          # 闲聊
│       │           └── _config_loader.py
│       │
│       ├── adapters/                    # 适配器层
│       │   └── http_asset_client.py     # HttpAssetClient (UpstreamClient 包装)
│       │
│       ├── llm/                         # LLM 调用
│       │   └── client.py                # OpenAI 客户端(支持多模态)
│       │
│       ├── services/                    # 基础服务
│       │   ├── upstream_client.py       # 上游 HTTP 客户端
│       │   └── conversation_store.py    # SQLite 对话存储
│       │
│       └── mcp_server.py                # MCP Server (使用 LangGraph)
│
├── frontend/                            # Vue 前端
│   ├── package.json
│   ├── vite.config.ts
│   │
│   └── src/
│       ├── App.vue
│       ├── main.ts
│       ├── embed.ts                     # embed SDK 入口
│       │
│       ├── layouts/                     # 布局
│       │   ├── StandaloneLayout.vue     # 独立模式 (全页面三栏)
│       │   └── EmbeddedLayout.vue       # 嵌入模式 (IM 聊天窗口)
│       │
│       ├── components/
│       │   ├── chat/
│       │   │   ├── ChatPanel.vue        # 对话面板(含配置/数据卡片)
│       │   │   └── ChatInput.vue        # 输入框+图片上传+发送
│       │   └── json/
│       │       └── JsonPanel.vue        # JSON 展示面板
│       │
│       ├── services/
│       │   └── api.ts                   # 统一 chat() + SSE
│       │
│       ├── stores/
│       │   └── conversation.ts          # Pinia store
│       │
│       └── composables/
│           └── forwardHeaders.ts        # Header 透传
│
├── .env                                 # 环境变量
├── .env.example                         # 环境变量模板
├── TECH-ROADMAP.md                      # ← 本文档
└── README.md
```

---

## 12. 开发路线图

```
Week 1: 基础搭建
═══════════════════════════════════════════════════════════════

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ 项目初始化    │→│ 共享类型定义  │→│ Skill 文件    │
  │              │  │              │  │ 准备         │
  │ backend/     │  │ Pydantic     │  │ 从 modeler   │
  │ frontend/    │  │ models       │  │ 复制 skills/ │
  └──────────────┘  └──────────────┘  └──────────────┘

Week 2-3: 核心引擎
═══════════════════════════════════════════════════════════════

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ Skill        │→│ LangGraph    │→│ FastAPI      │
  │ Consumer     │  │ 工作流       │  │ API + SSE    │
  │              │  │              │  │              │
  │ • watchdog   │  │ • State      │  │ • /generate  │
  │ • 内存缓存   │  │ • 7 个节点   │  │ • /modify    │
  │ • 热更新     │  │ • 条件边重试 │  │ • /validate  │
  │ • jsonschema │  │ • Prompt组装 │  │ • SSE 流     │
  └──────────────┘  └──────────────┘  └──────────────┘

Week 4: MCP + 会话
═══════════════════════════════════════════════════════════════

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ MCP Server   │→│ 会话管理     │→│ 5 个 Skill   │
  │              │  │              │  │ 全部实现     │
  │ • tools/*    │  │ • 多轮对话   │  │              │
  │ • resources  │  │ • 上下文保持 │  │ • create     │
  │ • JSON-RPC   │  │ • 历史查看   │  │ • update     │
  └──────────────┘  └──────────────┘  │ • get/clone  │
                                      │ • image      │
                                      └──────────────┘

Week 5: 前端
═══════════════════════════════════════════════════════════════

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ 对话窗口     │→│ JSON 展示    │→│ 会话管理     │
  │              │  │              │  │ + 响应式     │
  │ • 消息列表   │  │ • Monaco     │  │              │
  │ • 输入框     │  │ • 只读       │  │ • 新建/切换  │
  │ • SSE 消费   │  │ • 复制/下载  │  │ • 桌面/移动  │
  └──────────────┘  └──────────────┘  └──────────────┘

Week 6: 集成 + 部署
═══════════════════════════════════════════════════════════════

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │ Docker       │→│ 端到端测试   │→│ 文档         │
  │              │  │              │  │              │
  │ • Dockerfile │  │ • NL→Config  │  │ • README     │
  │ • compose    │  │ • 多轮对话   │  │ • 配置说明   │
  │ • nginx      │  │ • MCP 调用   │  │ • API 文档   │
  └──────────────┘  └──────────────┘  └──────────────┘
```

### 里程碑验收标准

```
┌─────────────────────────────────────────────────────────────────┐
│  M1: 核心引擎 MVP ✅ 已完成                                     │
│  ─────────────────────────────                                   │
│  ✅ LangGraph StateGraph 工作流可运行                            │
│  ✅ 自然语言 → FormConfig JSON 生成成功                          │
│  ✅ 上游校验 + 自动重试（≤3 次）                                 │
│  ✅ SSE 流式输出正常                                             │
│                                                                  │
│  M2: API + MCP 可用 ✅ 已完成                                   │
│  ─────────────────────────────                                   │
│  ✅ REST API 统一入口 (/api/config/chat)                        │
│  ✅ MCP Server 使用 LangGraph (非旧 dispatcher)                  │
│  ✅ 6 个工具全部实现 (create/modify/get/clone/image/chat)       │
│  ✅ 追问 interrupt/resume 机制                                   │
│  ✅ 多轮对话上下文保持                                           │
│                                                                  │
│  M3: 前端可用 ✅ 已完成                                         │
│  ─────────────────────────                                       │
│  ✅ 对话窗口可输入并展示结果                                     │
│  ✅ JSON 面板实时展示配置                                        │
│  ✅ 图片上传支持 (ImageFormTool)                                 │
│  ✅ Pipeline 进度条 + 追问卡片                                   │
│  ✅ 独立模式 + 嵌入模式                                         │
│                                                                  │
│  M4: 部署完成 ✅ 已完成                                         │
│  ─────────────────────────                                       │
│  ✅ Docker Compose 一键启动                                      │
│  ✅ 端到端流程验证通过                                           │
│  ✅ 文档齐全 (README + TECH-ROADMAP)                            │
│  ✅ 三层六边形架构: Engine → SDK → Domain Pack                   │
│  ✅ 插件自动发现: 新 pack 零改动 Engine                          │
└─────────────────────────────────────────────────────────────────┘
```
