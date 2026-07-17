## Context

当前 njmind-modeler 项目通过 Java 编译时生成表单配置的 JSON Schema、模板和 Skill 文件。这些 Skill 文件定义了表单字段类型、配置规则、模板结构等，是 AI 工具生成表单配置的"规则源"。

**核心问题**：
1. 配置生成逻辑硬编码在 Java 编译流程中，更新迭代需要重新编译
2. 缺少自然语言交互界面，用户需要手动编写配置 JSON
3. 前端表单渲染底码已存在于其他项目，本项目只需提供**对话窗口 + 基于规则生成 JSON**

**关键约束**：
- Skill 文件由 njmind-modeler 项目动态生成，本项目只消费不生产
- 主体流程固定（输入 → 解析 → 生成 → 校验 → 输出），但 Skill 规则动态注入
- 前端表单渲染由其他项目负责，本项目前端 = 对话窗口 + JSON 输出展示
- 需要与现有 njmind-modeler 的 JSON Schema 和配置格式完全兼容
- 第一期聚焦表单配置，架构需预留列表配置、流程配置的扩展点

## Goals / Non-Goals

**Goals:**
- 消费外部 Skill 文件（JSON Schema + 模板 + 规则），作为配置生成的规则源
- 实现自然语言 → 表单配置 JSON 的转换引擎（固定流程 + 动态规则注入）
- 提供 MCP 协议接口，供 AI 工具直接调用
- 提供 Web 对话界面，支持多轮对话生成配置、JSON 输出展示和导出
- 提供 REST API，供其他系统集成
- 架构可扩展，后续支持列表配置、流程配置

**Non-Goals:**
- 不生产 Skill 文件（由 njmind-modeler 负责）
- 不实现表单渲染/运行时（由前端业务系统负责，底码已有）
- 不实现字段的拖拽编辑/可视化编辑器（前端只做对话 + JSON 展示）
- 不处理用户认证/权限管理（第一期）
- 不实现配置的持久化存储（第一期只生成和导出，不存储）

## Decisions

### 1. LLM 编排框架：LangGraph

**选择**：LangGraph（LangChain 团队出品的状态图工作流引擎）

**理由**：
- **固定流程 + 动态规则注入**完美匹配 LangGraph 的 StateGraph 模型：
  - 节点（Node）= 固定流程步骤（解析意图 → 提取字段 → 生成配置 → Schema 校验 → 输出）
  - 状态（State）= 携带动态注入的 Skill 规则（字段类型定义、JSON Schema、模板、约束条件）
  - 边（Edge）= 流程分支（校验通过 → 输出 / 校验失败 → 重试）
- **内置校验重试循环**：校验节点失败后自动回到生成节点，携带错误信息，最多 N 次
- **状态持久化**：内置 Checkpoint 机制，支持多轮对话上下文保持
- **流式输出**：原生支持 streaming，每个节点执行完可以推送中间结果
- **与 LangChain 生态兼容**：可复用 LangChain 的 ChatModel、Output Parser 等组件

**备选方案对比**：
| 方案 | 优势 | 劣势 | 结论 |
|------|------|------|------|
| **LangGraph** | 状态图控制流、内置重试/检查点、流式输出 | 学习曲线略高 | ✅ 最适合 |
| LangChain | 链式调用简单 | 复杂工作流控制弱，重试需手写 | ❌ 不够灵活 |
| DeepAgent | 多 Agent 协作 | 偏通用 Agent，对固定流程过重 | ❌ 杀鸡用牛刀 |
| 原生 OpenAI SDK | 最轻量 | 需手写状态管理/重试/上下文，维护成本高 | ❌ 重复造轮子 |

**LangGraph 工作流设计**：
```
                    ┌─────────────────────────────────┐
                    │         State（动态注入）         │
                    │  - skill_rules: SkillRuleSet     │
                    │  - json_schema: FormSchema       │
                    │  - templates: TemplateMap        │
                    │  - conversation_history: Message[]│
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   Node 1: parse_intent          │
                    │   解析用户意图（新建/修改/查询）    │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   Node 2: build_prompt          │
                    │   从 State 取 Skill 规则         │
                    │   组装 system prompt + schema    │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   Node 3: generate_config       │
                    │   调用 LLM，Structured Output   │
                    │   输出 FormConfig JSON          │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   Node 4: validate_config       │
                    │   JSON Schema 校验              │
                    └──────┬───────────────┬─────────┘
                           │               │
                      通过 ▼           失败 ▼（retry < 3）
                    ┌──────────┐   ┌──────────────┐
                    │  输出结果  │   │ 回到 Node 3   │
                    └──────────┘   │ 携带错误信息   │
                                   └──────────────┘
```

### 2. LLM 调用：OpenAI 兼容接口 + Structured Output

**选择**：
- 使用 `openai` Python SDK（标准接口规范）
- 通过 `response_format: { type: "json_schema", json_schema: {...} }` 强制输出符合 Schema
- 支持配置 `base_url` + `api_key`，兼容任何 OpenAI 协议的服务（OpenAI / Azure / 通义 / DeepSeek / 本地 Ollama 等）

**具体依赖**：
```
openai>=1.30.0       # OpenAI 标准 SDK
langgraph>=0.2.0     # 工作流编排
langchain-openai>=0.2.0  # LangChain OpenAI 集成
pydantic>=2.0        # 数据模型校验
```

**配置示例**：
```yaml
llm:
  base_url: "https://api.openai.com/v1"  # 或任何兼容接口
  api_key: "${OPENAI_API_KEY}"
  model: "gpt-4o"
  temperature: 0.1       # 低温度，配置生成需要确定性
  max_tokens: 4096
  timeout: 60
```

### 3. 架构：Node.js + Python 双服务 + 轻量前端

**选择**：
```
┌──────────────────────────────────────────────────────────┐
│                    用户 / AI 工具                          │
└──────┬───────────────────────────────────┬───────────────┘
       │ HTTP/SSE                          │ JSON-RPC 2.0
       │                                   │
┌──────▼───────────────────────────────────▼───────────────┐
│              packages/api (Node.js)                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐ │
│  │  REST API     │  │  MCP Server  │  │ Skill Consumer │ │
│  │  (Express)    │  │  (json-rpc)  │  │ (文件监听+缓存) │ │
│  └──────┬───────┘  └──────────────┘  └───────┬────────┘ │
│         │                                     │          │
└─────────┼─────────────────────────────────────┼──────────┘
          │ HTTP                                │ 文件读取
          │                                     │
┌─────────▼─────────────────────────────────────┼──────────┐
│           packages/engine (Python)            │          │
│  ┌──────────────────────────────────────┐     │          │
│  │         LangGraph 工作流              │     │          │
│  │  parse_intent → build_prompt →       │     │          │
│  │  generate_config → validate_config   │     │          │
│  └──────────────────────────────────────┘     │          │
│  ┌──────────────┐  ┌────────────────────┐     │          │
│  │  FastAPI      │  │  OpenAI SDK        │     │          │
│  │  (uvicorn)    │  │  (兼容接口)        │     │          │
│  └──────────────┘  └────────────────────┘     │          │
└────────────────────────────────────────────────┼──────────┘
                                                 │
┌────────────────────────────────────────────────┼──────────┐
│           外部 Skill 文件目录                    │          │
│  (由 njmind-modeler 动态生成并同步)              ◄─────────┘
│  ├── mcp-schemas/*.schema.json
│  ├── mcp-templates/*.template.json
│  ├── mcp-guides/guide.json
│  └── mcp-skills/*/SKILL.md
└───────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────┐
│           packages/web (React)                            │
│  ┌────────────────────────────────────────────────────┐   │
│  │  对话窗口 + JSON 输出展示 + 导出                     │   │
│  │  (Ant Design Bubble.List + Monaco Editor)          │   │
│  └────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘
```

### 4. 前端：对话窗口 + JSON 展示（轻量）

**选择**：React + Ant Design + Monaco Editor

**理由**：
- 前端表单渲染底码已有，本项目只需提供**对话窗口**（用户输入自然语言）+ **JSON 展示**（生成的配置）
- Ant Design 的 `Bubble`（对话气泡）组件天然适合对话场景
- Monaco Editor 用于 JSON 语法高亮展示和编辑
- 不需要拖拽编辑器、表单预览等重型功能

**具体依赖**：
```json
{
  "dependencies": {
    "react": "^18.3",
    "antd": "^5.20",
    "@ant-design/x": "^1.0",       // Ant Design 对话组件（Bubble、Conversations）
    "@monaco-editor/react": "^4.6", // JSON 编辑器
    "ahooks": "^3.8"               // SSE hook、请求 hook
  }
}
```

**前端页面结构**：
```
┌─────────────────────────────────────────────────────┐
│  Header: LLM Form Modeler                    [导出] │
├──────────────────────────┬──────────────────────────┤
│                          │                          │
│   对话区域                │   JSON 输出区域           │
│   (Ant Design Bubble)    │   (Monaco Editor)        │
│                          │                          │
│   👤 创建一个员工表单，    │   {                      │
│      包含姓名、工号、部门  │     "formName": "...",   │
│                          │     "fields": [...]      │
│   🤖 已为您生成员工表单   │   }                      │
│      配置，包含 3 个字段   │                          │
│                          │                          │
│   👤 把工号改成必填       │                          │
│                          │                          │
│   🤖 已更新工号字段的     │                          │
│      required 属性       │                          │
│                          │                          │
├──────────────────────────┴──────────────────────────┤
│  [输入框: 描述你需要的表单...]              [发送]    │
└─────────────────────────────────────────────────────┘
```

### 5. Node.js ↔ Python 通信：HTTP REST + SSE 透传

**选择**：
- Python FastAPI 暴露 `/api/engine/generate`（SSE）和 `/api/engine/modify`（SSE）
- Node.js 调用 Python SSE 接口，透传 SSE 事件流给前端
- 使用 `EventSource` 或 `fetch` + ReadableStream 消费 Python SSE

**通信协议**：
```
POST /api/engine/generate
Content-Type: application/json
Accept: text/event-stream

Request:  { "description": "...", "skill_context": {...}, "conversation_id": "..." }
Response: SSE stream
  event: stage       data: {"stage": "parsing", "message": "正在解析意图..."}
  event: stage       data: {"stage": "generating", "message": "正在生成配置..."}
  event: stage       data: {"stage": "validating", "message": "正在校验..."}
  event: retry       data: {"attempt": 1, "error": "..."}
  event: result      data: {"config": {...}, "valid": true}
  event: done        data: {}
```

### 6. MCP Server：独立模块

**选择**：
- 使用 `@modelcontextprotocol/sdk`（官方 TypeScript SDK）
- 在 Node.js API 层内作为独立模块，共享 Skill Consumer 的缓存数据
- 传输层：HTTP Streamable（MCP 最新规范）

**具体依赖**：
```json
{
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0"
  }
}
```

### 7. 完整依赖清单

**packages/api (Node.js)**：
```json
{
  "dependencies": {
    "express": "^4.21",
    "@modelcontextprotocol/sdk": "^1.0",
    "ajv": "^8.17",
    "chokidar": "^4.0",
    "cors": "^2.8",
    "helmet": "^8.0",
    "express-rate-limit": "^7.4"
  },
  "devDependencies": {
    "typescript": "^5.6",
    "@types/express": "^5.0",
    "vitest": "^2.1",
    "tsx": "^4.19"
  }
}
```

**packages/engine (Python)**：
```toml
[project]
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.32",
    "langgraph>=0.2",
    "langchain-openai>=0.2",
    "openai>=1.30",
    "pydantic>=2.0",
    "sse-starlette>=2.0",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.7",
]
```

**packages/web (React)**：
```json
{
  "dependencies": {
    "react": "^18.3",
    "react-dom": "^18.3",
    "antd": "^5.20",
    "@ant-design/x": "^1.0",
    "@monaco-editor/react": "^4.6",
    "ahooks": "^3.8"
  },
  "devDependencies": {
    "typescript": "^5.6",
    "vite": "^5.4",
    "@vitejs/plugin-react": "^4.3"
  }
}
```

## Risks / Trade-offs

**[Risk] LangGraph 学习曲线** → 核心概念只有 State/Node/Edge，1-2 天可上手。团队已有 LangChain 经验则更快。
**[Risk] LLM 输出不稳定** → LangGraph 校验节点 + Structured Output 双重保障，最多重试 3 次。
**[Risk] Python 和 Node.js 双服务部署复杂** → Docker Compose 统一编排，一键启动。
**[Risk] Skill 文件格式变更** → 版本化 Skill 文件，启动时校验格式兼容性。
**[Risk] 对话上下文过长** → LangGraph Checkpoint 管理上下文窗口，超过 token 限制自动摘要。
**[Trade-off] 第一期不持久化配置** → 简化实现，但用户需要手动导出保存。
**[Trade-off] 依赖外部 Skill 文件** → 解耦生产与消费，但增加部署依赖。
**[Trade-off] 前端不做表单渲染** → 聚焦核心能力，表单渲染由已有项目负责。
