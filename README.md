# LLM Form Modeler

**LLM 驱动的多插件智能助手引擎** — 通过 LangGraph StateGraph 编排意图识别、工具执行与追问恢复，支持自然语言驱动多种业务能力（表单配置、请假申请、审批查询等），Engine 层零领域知识。

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Ant Design Vue + Pinia |
| 后端 | Python 3.12 + FastAPI + LangGraph StateGraph |
| LLM | OpenAI 兼容接口（Qwen3 / GPT / 任意兼容模型） |
| 存储 | SQLite（对话历史 + LangGraph Checkpoint） |
| 上游 | AssetClient 抽象（HTTP 适配，环境变量配置） |

---

## 一、核心架构：三层六边形

```
┌─────────────────────────────────────────────────────────────────────┐
│                         前端 (Vue 3)                                │
│                                                                     │
│   独立模式 (三栏布局)          嵌入模式 (IM 聊天窗 + SDK)            │
│   StandaloneLayout             EmbeddedLayout + embed.js            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP / SSE
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Python 后端 (FastAPI :18080)                      │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  API 层 (api/)                                               │   │
│  │  /api/config/chat  /api/conversations  /mcp  /health         │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ Engine 层 (engine/) — 零领域知识                          │   │
│  │                                                              │   │
│  │  ┌────────────────────────────────────────────────────────┐  │   │
│  │  │  LangGraph StateGraph                                  │  │   │
│  │  │                                                        │  │   │
│  │  │  classify_intent ──→ route_by_tool ──→ execute_tool    │  │   │
│  │  │        │                                  │            │  │   │
│  │  │        │                            interrupt?         │  │   │
│  │  │        │                           ↙        ↘          │  │   │
│  │  │        │                     挂起追问    正常完成       │  │   │
│  │  │        │                        │           │          │  │   │
│  │  │        │                   Command(resume)  │          │  │   │
│  │  │        │                        │           ▼          │  │   │
│  │  │        │                        └──→ execute_tool      │  │   │
│  │  │        │                             (重跑工具)         │  │   │
│  │  │        │                                  │            │  │   │
│  │  │        │                                  ▼            │  │   │
│  │  │        └──────────────────────→ handle_result ──→ END  │  │   │
│  │  │                                                        │  │   │
│  │  │  Checkpoint: InMemorySaver (thread_id = conv_id)       │  │   │
│  │  └────────────────────────────────────────────────────────┘  │   │
│  │                                                              │   │
│  │  辅助模块:                                                   │   │
│  │  ├── stream.py      graph.stream → SSE 桥接 (实时 chunk)    │   │
│  │  ├── conversation.py 多轮对话管理 (append-only 事件流)      │   │
│  │  ├── compression.py  上下文压缩 (70% 阈值 + 熔断器)         │   │
│  │  ├── prompt_loader.py Jinja2 模板加载 (缓存 + 覆写/追加)    │   │
│  │  └── logging_filter.py 日志脱敏过滤器                       │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ SDK 层 (sdk/) — 协议定义                                  │   │
│  │                                                              │   │
│  │  ├── tool.py        Tool / CompositeTool / ToolResult        │   │
│  │  │                  ToolContext / AskSpec / AskQuestion       │   │
│  │  ├── registry.py    ToolRegistry (自动发现 + 注册)           │   │
│  │  ├── asset_client.py AssetClient ABC (submit/query)          │   │
│  │  └── sanitize.py    Unicode 隐写清洗                         │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ Domain Packs (domains/) — 领域知识全部在此                │   │
│  │                                                              │   │
│  │  ┌─────────────────────┐                                    │   │
│  │  │ njmind_form         │                                    │   │
│  │  │                     │                                    │   │
│  │  │ tools/              │                                    │   │
│  │  │  create_form (6步)  │                                    │   │
│  │  │  modify_form (3步)  │                                    │   │
│  │  │  get_form (1步)     │                                    │   │
│  │  │  clone_form (3步)   │                                    │   │
│  │  │  image_form (3步)   │                                    │   │
│  │  │  chat (兜底)        │                                    │   │
│  │  │                     │                                    │   │
│  │  │ prompts/            │                                    │   │
│  │  │  chat.j2  parse.j2  │                                    │   │
│  │  │  generate.j2  ...   │                                    │   │
│  │  │                     │                                    │   │
│  │  │ config.yaml         │                                    │   │
│  │  └─────────────────────┘                                    │   │
│  │                                                              │   │
│  │  ┌──────────────────────────────┐                            │   │
│  │  │ 新插件只需:                   │                            │   │
│  │  │ 1. 创建 domains/xxx/ 目录     │                            │   │
│  │  │ 2. 实现 pack.py              │                            │   │
│  │  │ 3. 定义 Tool 子类             │                            │   │
│  │  │ → 自动发现, 零配置上线        │                            │   │
│  │  └──────────────────────────────┘                            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  Adapters (adapters/)                                        │   │
│  │  HttpAssetClient — HTTP 上游适配 (ASSET_BASE_URL 环境变量)   │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
┌────────────────┐   ┌─────────────────────┐
│ 上游业务 API   │   │ LLM 推理服务         │
│ (ASSET_BASE_URL│   │ (OpenAI 兼容接口)    │
│  提交/查询数据)│   │ Qwen3 / GPT / ...    │
└────────────────┘   └─────────────────────┘
```

### 架构试金石

```bash
# Engine 层不能包含任何领域知识
grep -rE "form|formCode|template|field|leave|请假" backend/src/engine/
# → 必须返回空
```

---

## 二、LangGraph StateGraph 核心流程

### 2.1 图结构

```python
START → classify_intent (LLM 选工具, 从 registry 动态生成 prompt)
  │
  ├─ route_by_tool ──→ execute_tool (执行工具, 支持 interrupt)
  │                         │
  │                    ToolResult.ask?
  │                    ├─ 是 → interrupt() 挂起 → SSE needsClarification
  │                    │       前端发 answers → Command(resume=answers)
  │                    │       → execute_tool 重跑 (带 clarify_answers)
  │                    └─ 否 → handle_result → END
  │
  └─ route_after_result ──→ rerun (追问恢复后重跑) / done (结束)
```

### 2.2 追问恢复机制 (LangGraph 原生 interrupt)

```
用户: "帮我创建一个请假申请表"
  │
  ▼
classify_intent → 选中 create_form
  │
  ▼
execute_tool → CreateFormTool.execute()
  │
  ├─ _step_parse_fields: LLM 提取字段 → 部分字段不明确
  │  → 关键信息缺失 → 设置 _need_clarify 标记
  │
  ├─ ToolResult(ask=AskSpec(...))
  │
  └─ interrupt({questions, summary}) → 图挂起
     │
     ▼
  SSE → 前端渲染追问卡片
     │
     用户回答: {请假类型: "年假/事假/病假", 日期范围: "开始-结束"}
     │
     ▼
  Command(resume=answers) → 图从断点恢复
     │
     ├─ interrupt() 返回 answers
     ├─ 注入 tool_state["clarify_answers"]
     ├─ 清除 _need_clarify / _clarify_spec 标记
     └─ 重跑 execute_tool
           │
           ├─ _step_parse_fields: LLM 提取 → 字段完整 ✓
           ├─ _step_fetch_templates: 获取字段模板 ✓
           ├─ _step_generate: LLM 生成完整配置 ✓
           ├─ _step_validate: 上游 API 校验 ✓
           └─ ToolResult(artifact=form_config) → handle_result → END
```

### 2.3 GraphState 定义

```python
class GraphState(TypedDict, total=False):
    # 输入
    user_input: str
    conversation_history: list[dict]
    compressed_history: str
    conversation_id: str
    forward_headers: dict          # 嵌入模式透传的请求头
    current_config: dict | None    # 已有配置 (modify 用)

    # 意图识别
    tool_name: str                 # 选中的工具名
    intent_reason: str

    # 工具执行
    tool_state: dict               # 工具内部 state (透传, Engine 不读)
    tool_result: dict | None       # 工具执行结果

    # 追问 (LangGraph interrupt)
    pending_questions: list[dict]
    clarify_answers: dict          # resume 值

    # SSE 事件收集
    sse_events: list[dict]
```

---

## 三、插件系统

### 3.1 自动发现机制

```
domains/
├── njmind_form/          ← 表单配置插件
│   ├── pack.py           ← create_registry() 注册工具
│   ├── models.py         ← ParsedField 等数据模型
│   ├── tools/
│   │   ├── create_form.py   (CompositeTool, 6步管线)
│   │   ├── modify_form.py   (CompositeTool, 3步管线)
│   │   ├── get_form.py      (Tool, 查询已有表单)
│   │   ├── clone_form.py    (Tool, 复制表单)
│   │   ├── image_form.py    (Tool, 图片识别→表单)
│   │   └── chat.py          (Tool, 兜底闲聊)
│   └── prompts/
│       ├── chat.j2
│       ├── parse.j2
│       ├── generate.j2
│       └── ...
│
└── (新插件只需创建目录 + pack.py + tools/)
```

### 3.2 Tool 协议

```python
class Tool(ABC):
    name: str                    # 工具名 (LLM 选择时看到)
    description: str             # 工具说明
    when: str                    # "何时用" 描述

    # 安全声明 (Fail-Closed 默认值)
    is_destructive: bool = True
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    requires_existing_artifact: bool = False

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult

class CompositeTool(Tool):
    steps: list[str] = []        # 管线步骤名
    pipeline_steps: list[dict]   # 前端展示用

    def run_pipeline(self, state, ctx):
        for step in self.steps:
            if state.get("_need_clarify"): break
            getattr(self, f"_step_{step}")(state, ctx)
```

### 3.3 ToolResult 三态

```python
class ToolResult:
    artifact: dict | None        # 制品 (config/data)
    artifact_type: str           # "config" | "data"
    reply: str | None            # 闲聊回复
    ask: AskSpec | None          # 追问 (非空 → interrupt)
    summary: str                 # 摘要 (进对话历史)
    error_for_llm: str | None    # 错误 (回流给 LLM)
    extra: dict                  # 扩展数据
```

### 3.4 动态能力上报

ChatTool（兜底工具）通过 `ctx.registry` 动态查询所有已注册工具的能力描述，生成系统 prompt：

```python
def _build_capabilities(self, ctx):
    caps = []
    for tool in ctx.registry.all():
        if tool.name != self.name:
            caps.append(f"- {tool.name}: {tool.when}")
    return "\n".join(caps)
```

**新增插件后，ChatTool 的能力描述自动更新，无需修改 Engine 或 ChatTool 代码。**

---

## 四、当前插件能力

### 4.1 njmind_form — 表单配置

| 工具 | 类型 | 管线 | 说明 |
|------|------|------|------|
| create_form | CompositeTool | 6步 | 自然语言 → 完整表单配置 |
| modify_form | CompositeTool | 3步 | 自然语言修改已有配置 |
| get_form | Tool | - | 根据 formCode 查询已有表单 |
| clone_form | Tool | - | 复制已有表单并修改标识 |
| image_form | Tool | - | 图片识别 → 表单配置 (多模态) |
| chat | Tool | - | 兜底闲聊 + 动态能力描述 |

**CREATE 管线 (6步)：**
```
fetch_guide → list_assets → parse_fields(LLM) → fetch_templates → generate(LLM) → validate
```

**MODIFY 管线 (3步)：**
```
fetch_guide → modify(LLM) → validate
```

### 4.2 leave_application — 请假申请 (Demo 插件)

> 注：此为架构演示插件，当前未在 pack.py 中注册。

| 工具 | 类型 | 管线 | 说明 |
|------|------|------|------|
| submit_leave | CompositeTool | 3步 | 提交请假申请 (支持追问) |
| query_status | Tool | - | 查询审批状态 |

**SUBMIT 管线 (3步)：**
```
parse_info(LLM) → validate_rules(API) → submit(API)
```

**关键设计：破坏性操作(is_destructive=True)信息不足时追问，不填默认值。**

---

## 五、SSE 实时进度

```
后端 (graph.stream)                    前端
┌──────────────┐
│ classify_    │──stage("正在理解您的意图...")──→  🔄 正在理解...
│ intent       │                                      │
└──────┬───────┘                                      ▼
       ▼                                        ┌──────────┐
┌──────────────┐                                 │ 进度条   │
│ execute_tool │──pipeline_definition──→         │ 动画     │
│              │  [{step: "解析字段"}, ...]       │          │
│  _step_      │──stage("解析中...")──→          │ ✓ 解析   │
│  parse_fields│                                  │ ○ 生成   │
│              │──stage("生成中...")──→          │ ○ 校验   │
│  _step_      │                                  └──────────┘
│  generate    │──stage("校验中...")──→
│              │
│  _step_      │──stage("校验通过 ✓")──→
│  validate    │
│              │──result({artifactType, data})──→  📋 数据卡片
│  submit      │                                    或
│              │──done()──→                       📝 配置 JSON
└──────────────┘
```

**实现要点：**
- `graph.stream()` 是同步 API，在线程池中执行
- 每个 chunk 通过 `loop.call_soon_threadsafe()` 实时推 SSE（不等全部完成）
- interrupt 时检查 `graph.get_state()` 获取中断数据

---

## 六、嵌入主系统

### 方式 A：SDK 嵌入（推荐）

```html
<script src="http://你的部署:13080/embed.js"></script>
<script>
  const assistant = new LLMFormModeler({
    baseUrl: 'http://192.168.99.22:13080',
    userId: 'zhangsan',
    position: 'bottom-right',
    onConfigGenerated: (config) => { /* ... */ },
    onConfigApply: (config) => { /* 写入主系统设计器 */ },
  })
</script>
```

### 方式 B：iframe 嵌入

```html
<iframe
  src="http://你的部署:13080/?embed=true&userId=用户ID"
  style="width: 400px; height: 600px; border: none;"
></iframe>
```

### 请求头透传

嵌入模式下，主系统的请求头会自动透传到后端（用于上游 API 鉴权等）：

```
主系统 → 前端 (iframe/SDK) → 后端 → AssetClient → 上游 API
         (X-User-Id, X-Tenant-Id, Authorization 等全部透传)
```

---

## 七、快速开始

### 环境要求

- Python 3.12+
- Node.js 20+

### 配置

编辑 `.env`：

```env
# 上游业务 API (AssetClient 的 base_url)
ASSET_BASE_URL=http://192.168.99.22:19999

# LLM 推理服务 (OpenAI 兼容接口)
LLM_BASE_URL=http://127.0.0.1:1234/v1
LLM_API_KEY=local-dev-key
LLM_MODEL=qwen/qwen3.6-35b-a3b
LLM_MAX_TOKENS=16384
LLM_TIMEOUT=300

# 服务端口
BACKEND_PORT=18080
FRONTEND_PORT=13080
```

### 启动

```bash
# 后端
cd backend
source venv/bin/activate
uvicorn src.main:app --reload --host 0.0.0.0 --port 18080

# 前端
cd frontend
npm install && npm run dev
```

### 访问

| 地址 | 说明 |
|------|------|
| http://localhost:13080/ | 独立模式（三栏布局） |
| http://localhost:13080/?embed=true | 嵌入模式（IM 聊天窗口） |
| http://localhost:13080/embed-demo.html | 嵌入演示页（模拟主系统） |
| http://localhost:13080/embed.js | 嵌入 SDK |
| http://localhost:18080/docs | API 文档（Swagger） |
| http://localhost:18080/health | 健康检查 |

---

## 八、API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config/chat` | **统一对话入口** (SSE 流式, 走 LangGraph) |
| GET | `/api/conversations` | 对话列表（按 userId） |
| GET | `/api/conversations/:id` | 对话详情（含消息历史） |
| POST | `/api/conversations` | 创建对话 |
| DELETE | `/api/conversations/:id` | 删除对话 |
| GET | `/api/skills/templates` | 获取模板列表（代理上游） |
| GET | `/api/skills/guide` | 获取配置指南（代理上游） |
| POST | `/mcp` | MCP 协议（JSON-RPC 2.0） |
| GET | `/health` | 健康检查 |

**ChatRequest 格式：**

```json
{
  "message": "用户消息",
  "conversation_id": "conv_xxx",
  "answers": {"leaveType": "年假", "startDate": "2026-07-22"},
  "image_base64": "data:image/png;base64,..."
}
```

- `answers` 非空时走 `Command(resume=answers)` 追问恢复路径
- `image_base64` 非空时传入 `tool_state` 供 ImageFormTool 使用
- 请求头自动透传到上游（嵌入模式）

---

## 九、目录结构

```
llm-to-modler/
├── backend/
│   └── src/
│       ├── main.py                # FastAPI 入口, 构建 Graph
│       ├── mcp_server.py          # MCP 协议服务 (使用 LangGraph)
│       │
│       ├── engine/                # ★ Engine 层 (零领域知识)
│       │   ├── graph.py           # StateGraph 构建 + compile
│       │   ├── graph_state.py     # GraphState TypedDict
│       │   ├── nodes.py           # 节点函数 (classify/execute/handle)
│       │   ├── stream.py          # graph.stream → SSE 桥接
│       │   ├── conversation.py    # 多轮对话管理
│       │   ├── compression.py     # 上下文压缩 + build_compressed_history
│       │   ├── prompt_loader.py   # Jinja2 模板加载
│       │   ├── dispatcher.py      # 旧调度器 (遗留, MCP 兼容)
│       │   └── logging_filter.py  # 日志脱敏
│       │
│       ├── sdk/                   # ★ SDK 层 (协议定义)
│       │   ├── tool.py            # Tool/CompositeTool/ToolResult/AskSpec
│       │   ├── registry.py        # ToolRegistry (自动发现)
│       │   ├── asset_client.py    # AssetClient ABC
│       │   └── sanitize.py        # Unicode 隐写清洗
│       │
│       ├── domains/               # ★ Domain Packs (领域知识全部在此)
│       │   ├── njmind_form/       # 表单配置插件
│       │   │   ├── pack.py
│       │   │   ├── models.py      # ParsedField 等数据模型
│       │   │   ├── tools/         # create/modify/get/clone/image/chat
│       │   │   └── prompts/       # Jinja2 模板
│       │
│       ├── adapters/              # 适配器
│       │   └── http_asset_client.py  # HTTP 上游实现
│       │
│       ├── api/                   # 路由层
│       │   ├── config.py          # /api/config/chat
│       │   ├── conversations.py   # /api/conversations
│       │   ├── skills.py          # /api/skills
│       │   ├── health.py          # /health
│       │   └── sse.py             # SSE 工具类
│       │
│       ├── llm/
│       │   └── client.py          # LLM 客户端 (OpenAI 兼容, 支持多模态)
│       │
│       └── services/
│           ├── conversation_store.py  # SQLite 存储
│           └── upstream_client.py     # 上游客户端
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── chat/              # 聊天组件 (ChatPanel/ChatInput/ClarificationCard)
│   │   │   └── json/              # JSON 查看器
│   │   ├── layouts/               # 独立/嵌入布局
│   │   ├── stores/                # Pinia 状态管理
│   │   ├── services/              # API 调用 + SSE
│   │   └── composables/           # Header 透传等
│   └── public/
│       └── embed-demo.html        # 嵌入演示页
│
├── .env                           # 环境变量
├── README.md                      # 本文档
└── TECH-ROADMAP.md                # 技术路径文档
```

---

## 十、设计亮点

### 1. Engine 零领域知识

Engine 层不知道"表单"、"请假"等任何业务概念。所有领域知识封装在 `domains/` 下的插件包中。新增业务能力 = 新增一个插件目录。

### 2. LangGraph 原生 interrupt/resume

追问流程不使用自研状态机，而是利用 LangGraph 的 `interrupt()` + `Command(resume=...)` 原生机制。Checkpoint 自动持久化状态，断点恢复零额外代码。

### 3. 动态能力上报

ChatTool 通过 `ctx.registry.all()` 动态查询所有已注册工具的能力描述。新增插件后，ChatTool 的"我能做什么"自动更新，无需修改任何代码。

### 4. 破坏性操作安全设计

`is_destructive=True` 的工具（如提交请假申请）在信息不足时**必须追问**，不填默认值。通过 `ToolResult.ask` + `AskSpec` 声明式定义追问问题，Engine 统一处理 interrupt。

### 5. SSE 实时流式

`graph.stream()` 的每个 chunk 通过 `call_soon_threadsafe` 实时推送到前端，不等全部执行完成。前端实时展示每一步进度动画。

### 6. 请求头全链路透传

嵌入模式下，主系统的 HTTP 请求头（X-User-Id、Authorization 等）通过 `forward_headers` 全链路透传到上游 API，实现零侵入的身份传递。

### 7. Fail-Closed 安全默认

所有工具属性默认保守值：`is_destructive=True`、`is_read_only=False`、`is_concurrency_safe=False`。插件必须显式声明安全属性，避免误用。

---

## 十一、Docker 部署

```yaml
# docker-compose.yml
services:
  backend:
    build: ./backend
    ports:
      - "18080:8000"
    env_file: .env

  frontend:
    build: ./frontend
    ports:
      - "13080:80"
    depends_on:
      - backend
```

```bash
docker-compose up -d
```
