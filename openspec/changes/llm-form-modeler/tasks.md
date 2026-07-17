# LLM Form Modeler 实施任务清单

## 1. 项目初始化与基础设施

- [ ] 1.1 创建项目根目录结构：`backend/`、`frontend/`、`skills/`
- [ ] 1.2 初始化 Python 后端项目：`backend/pyproject.toml`，配置依赖（fastapi、uvicorn、langgraph、langchain-openai、openai、pydantic、jsonschema、watchdog、mcp、sse-starlette、httpx、python-dotenv）
- [ ] 1.3 创建后端目录结构：`backend/src/api/`、`backend/src/services/`、`backend/src/graph/`、`backend/src/llm/`、`backend/src/mcp/`
- [ ] 1.4 初始化 Vue 3 前端项目：`frontend/package.json`，使用 Vite + TypeScript
- [ ] 1.5 安装前端依赖：vue、ant-design-vue、@monaco-editor/loader、pinia
- [ ] 1.6 创建前端目录结构：`frontend/src/layouts/`、`frontend/src/components/`、`frontend/src/embed/`、`frontend/src/composables/`、`frontend/src/stores/`
- [ ] 1.7 配置 `.gitignore`、`README.md`、`.env.example`
- [ ] 1.8 创建 `skills/` 目录，从 njmind-modeler 复制 Skill 文件（RULES.md、5 个 SKILL.md、mcp-schemas、mcp-templates、mcp-guides）

## 2. 后端核心：Skill 消费模块

- [ ] 2.1 实现 Skill 文件加载器（`backend/src/services/skill_consumer.py`）：扫描 skills 目录，解析 `.schema.json`、`.template.json`、`guide.json`、`RULES.md`
- [ ] 2.2 实现内存缓存层：存储已加载的 Schema、模板、字段类型、指南数据（dict 结构）
- [ ] 2.3 实现文件监听器（watchdog）：监听 skills 目录变化，触发增量热更新
- [ ] 2.4 实现 JSON Schema 校验器（jsonschema）：对配置 JSON 进行 Schema 校验，返回错误详情
- [ ] 2.5 实现 Skill 信息查询 API（`backend/src/api/skills.py`）：`GET /api/skills/field-types`、`GET /api/skills/templates`、`GET /api/skills/schemas`、`GET /api/skills/guide`
- [ ] 2.6 编写 Skill 消费模块单元测试（pytest）

## 3. 后端核心：LangGraph 工作流引擎

- [ ] 3.1 实现 LangGraph State 定义（`backend/src/graph/state.py`）：包含 `skill_name`、`skill_content`、`rules_content`、`conversation_history`、`current_config`、`validation_errors`、`retry_count`
- [ ] 3.2 实现 OpenAI 客户端封装（`backend/src/llm/client.py`）：支持配置 `base_url`、`api_key`、`model`，兼容 OpenAI 协议
- [ ] 3.3 实现 Prompt 组装器（`backend/src/llm/prompt_builder.py`）：4 层组装（RULES.md + SKILL.md + 动态数据 + 输出约束）
- [ ] 3.4 实现 LangGraph Node 1 - load_skill：加载 SKILL.md 和 RULES.md 到 State
- [ ] 3.5 实现 LangGraph Node 2 - classify_intent：调用 LLM 分类用户意图（create/update/get/clone/image）
- [ ] 3.6 实现 LangGraph Node 3 - prepare_data：从 Skill 缓存获取 guide、schema、template
- [ ] 3.7 实现 LangGraph Node 4 - generate_config：调用 LLM，使用 Structured Output 生成 FormConfig JSON
- [ ] 3.8 实现 LangGraph Node 5 - validate_config：使用 jsonschema 校验输出，失败则返回 generate_config 节点（最多 3 次）
- [ ] 3.9 实现 LangGraph Node 6 - user_confirm：展示配置摘要，等待用户确认
- [ ] 3.10 实现 LangGraph Node 7 - submit：返回最终 JSON
- [ ] 3.11 实现 LangGraph 工作流编排（`backend/src/graph/graph.py`）：定义 StateGraph，连接节点和边，配置条件分支
- [ ] 3.12 实现配置修正功能：接收现有配置 + 自然语言修正指令，增量修改
- [ ] 3.13 编写 LangGraph 工作流单元测试（pytest）

## 4. 后端核心：API 层 + SSE

- [ ] 4.1 创建 FastAPI 应用骨架（`backend/src/main.py`）：路由配置、CORS、异常处理
- [ ] 4.2 实现配置生成 API（`backend/src/api/config.py`）：`POST /api/config/generate`（SSE）、`POST /api/config/modify`（SSE）、`POST /api/config/validate`
- [ ] 4.3 实现 SSE 流式输出（`backend/src/api/sse.py`）：每个节点执行完推送进度事件（stage、retry、result、confirm、done）
- [ ] 4.4 实现统一错误处理中间件：标准错误格式 `{code, message, details}`、HTTP 状态码映射
- [ ] 4.5 实现健康检查端点：`GET /health`
- [ ] 4.6 编写 API 层单元测试（pytest）和集成测试

## 5. 后端核心：MCP 协议层

- [ ] 5.1 实现 MCP Server（`backend/src/mcp/server.py`）：使用 Python mcp SDK，JSON-RPC 2.0 over HTTP
- [ ] 5.2 实现 MCP Tools（`backend/src/mcp/tools.py`）：`get_form_config`、`validate_form`、`list_templates`、`get_template`、`get_schema`、`list_field_types`
- [ ] 5.3 实现 MCP Resources（`backend/src/mcp/resources.py`）：`njmind://schemas/form-config`、`njmind://schemas/form-field-config`、`njmind://guide`、`njmind://field-types`
- [ ] 5.4 编写 MCP 协议层单元测试（pytest）

## 6. 后端核心：用户身份 & 对话历史

- [ ] 6.1 实现用户身份中间件：从 Header / Query / postMessage 提取 userId、userName、tenantId，注入 request.state.user
- [ ] 6.2 初始化 SQLite 数据库（`backend/data/conversations.db`）：创建 conversations 表和 messages 表
- [ ] 6.3 实现对话历史服务（`backend/src/services/conversation.py`）：创建会话、查询会话列表、获取会话详情、删除会话
- [ ] 6.4 实现对话历史 API（`backend/src/api/conversations.py`）：`POST /api/conversations`、`GET /api/conversations`、`GET /api/conversations/{id}`、`DELETE /api/conversations/{id}`
- [ ] 6.5 集成 LangGraph Checkpoint（SQLite）：使用 SqliteSaver 保存工作流状态，通过 thread_id（= conversation_id）关联
- [ ] 6.6 实现自动保存逻辑：生成配置时自动保存 user message 和 assistant message，更新 conversation.currentConfig
- [ ] 6.7 实现用户隔离：所有数据操作按 userId 隔离，校验会话归属
- [ ] 6.8 编写对话历史单元测试（pytest）

## 7. 前端：独立模式（全页面）

- [ ] 7.1 创建 Vue 应用入口（`frontend/src/main.ts`、`frontend/src/App.vue`）
- [ ] 7.2 实现独立模式布局（`frontend/src/layouts/StandaloneLayout.vue`）：三栏布局（侧边栏 + 对话区 + JSON 面板）
- [ ] 7.3 实现会话侧边栏（`frontend/src/components/conversation/ConversationSider.vue`）：会话列表、新建会话、切换会话
- [ ] 7.4 实现核心对话组件（`frontend/src/components/chat/ChatPanel.vue`）：消息列表、用户消息、AI 消息
- [ ] 7.5 实现对话输入组件（`frontend/src/components/chat/ChatInput.vue`）：输入框 + 发送按钮 + 示例提示
- [ ] 7.6 实现 SSE 消费 hook（`frontend/src/composables/useSSE.ts`）：消费后端 SSE 流，实时更新消息
- [ ] 7.7 实现 JSON 展示面板（`frontend/src/components/json/JsonPanel.vue`）：Monaco Editor 只读模式，语法高亮
- [ ] 7.8 实现 JSON 导出功能：下载 JSON 文件 + 复制到剪贴板
- [ ] 7.9 实现 Pinia 状态管理（`frontend/src/stores/conversation.store.ts`、`frontend/src/stores/config.store.ts`）
- [ ] 7.10 实现 API 调用服务（`frontend/src/services/config.api.ts`、`frontend/src/services/conversation.api.ts`）
- [ ] 7.11 编写前端组件测试（vitest + @vue/test-utils）

## 8. 前端：嵌入模式（IM 聊天窗口）

- [ ] 8.1 实现嵌入模式布局（`frontend/src/layouts/EmbeddedLayout.vue`）：单栏布局（纯对话 + JSON 抽屉）
- [ ] 8.2 实现嵌入模式检测逻辑：通过 URL 参数 `?embed=true` 或 `window.parent !== window` 判断
- [ ] 8.3 实现 postMessage 通信桥（`frontend/src/embed/bridge.ts`）：接收主系统消息（MODELER_INIT、MODELER_TOGGLE），发送消息（MODELER_CONFIG_GENERATED、MODELER_CONFIG_APPLY、MODELER_CLOSE）
- [ ] 8.4 实现嵌入模式通信 hook（`frontend/src/composables/useEmbedBridge.ts`）：封装 postMessage 通信逻辑
- [ ] 8.5 实现配置摘要卡片（`frontend/src/components/chat/ConfigSummaryCard.vue`）：展示配置摘要，提供"查看 JSON"和"应用配置"按钮
- [ ] 8.6 实现 JSON 抽屉（`frontend/src/components/json/JsonDrawer.vue`）：嵌入模式下查看 JSON
- [ ] 8.7 实现 embed SDK 入口（`frontend/src/embed.ts`）：编译为独立的 `embed.js`，供主系统引入
- [ ] 8.8 实现浮动按钮组件（`frontend/src/embed/FloatingButton.vue`）：右下角浮动按钮，点击打开聊天窗口
- [ ] 8.9 配置 Vite 多入口打包：`main.ts`（独立模式）和 `embed.ts`（嵌入 SDK）分别打包
- [ ] 8.10 编写嵌入模式组件测试

## 9. 集成与部署

- [ ] 9.1 编写 Docker Compose 配置（`docker-compose.yml`）：nginx + python backend
- [ ] 9.2 编写后端 Dockerfile（`backend/Dockerfile`）：python:3.11-slim
- [ ] 9.3 编写前端 Dockerfile（`frontend/Dockerfile`）：node:20-alpine 构建 + nginx:alpine 运行
- [ ] 9.4 配置 nginx 反向代理：`/api/*` → backend:8000，`/mcp` → backend:8000，`/embed.js` → 静态资源
- [ ] 9.5 配置 CORS Headers：支持跨域 iframe 嵌入
- [ ] 9.6 配置环境变量：LLM_BASE_URL、LLM_API_KEY、LLM_MODEL、SKILLS_DIR
- [ ] 9.7 编写启动脚本：`docker-compose up`（生产模式）、本地开发模式（uvicorn + vite dev）
- [ ] 9.8 端到端集成测试：从自然语言输入到配置导出的完整流程验证
- [ ] 9.9 编写部署文档：环境要求、配置说明、启动步骤、常见问题

## 10. 文档与验收

- [ ] 10.1 更新 README.md：项目介绍、快速开始、架构说明
- [ ] 10.2 编写 API 文档（OpenAPI/Swagger）
- [ ] 10.3 编写 embed SDK 使用文档：主系统如何引入和使用
- [ ] 10.4 里程碑验收 M1：Skill 消费 + 引擎 MVP（Week 3 末）
- [ ] 10.5 里程碑验收 M2：API + MCP 可用（Week 4 末）
- [ ] 10.6 里程碑验收 M3：前端可用（Week 5 末）
- [ ] 10.7 里程碑验收 M4：部署完成（Week 6 末）
