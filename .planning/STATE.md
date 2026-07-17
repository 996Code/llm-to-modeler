# 项目状态

## 当前位置

- **阶段**：llm-form-modeler
- **状态**：规格已定义，待规划（技术栈已细化）

## OpenSpec 关联

- **Change**：llm-form-modeler
- **路径**：openspec/changes/llm-form-modeler/
- **产出文件**：
  - proposal.md ✓
  - design.md ✓（已细化：LangGraph + 具体依赖 + 架构图）
  - specs/ ✓（5 个 capability，web-frontend 已简化为对话窗口）
  - tasks.md ✓（已细化：6 大任务组，45+ 具体任务）

## 技术栈决策

### LLM 编排框架
- **LangGraph**：状态图工作流引擎，固定流程 + 动态 Skill 规则注入
- 工作流：parse_intent → build_prompt → generate_config → validate_config（重试循环）

### LLM 调用
- **OpenAI 兼容接口**：使用 `openai` Python SDK，支持配置 `base_url` 切换不同模型
- **Structured Output**：`response_format: json_schema` 强制输出符合 Schema

### 后端架构
- **Node.js (Express)**：REST API + MCP Server + Skill Consumer
- **Python (FastAPI)**：LangGraph 工作流 + LLM 调用
- **通信**：HTTP REST + SSE 透传

### 前端
- **React + Ant Design + @ant-design/x**：对话窗口（Bubble.List）
- **Monaco Editor**：JSON 语法高亮展示
- **轻量定位**：对话 + JSON 展示，不做表单渲染（底码已有）

### 关键依赖
- Node.js: express, @modelcontextprotocol/sdk, ajv, chokidar
- Python: fastapi, langgraph, langchain-openai, openai, pydantic, sse-starlette
- React: antd, @ant-design/x, @monaco-editor/react, ahooks

## 活动日志

- 2026-07-16：/ai:spec 完成，生成完整的 OpenSpec 规格文件
- 2026-07-16：GSD 轻量初始化完成
- 2026-07-16：技术栈细化，更新 design.md、specs、tasks.md

## 下一步

运行 `/ai-plan llm-form-modeler` 将规格转为执行计划
