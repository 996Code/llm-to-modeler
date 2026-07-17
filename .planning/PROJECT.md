# 项目：LLM Form Modeler

## 概述

通过自然语言描述生成表单配置底码的独立项目。消费外部动态生成的 Skill 文件（由 njmind-modeler 项目维护），提供 MCP 协议接口和 Web 前端界面，让用户通过自然语言交互生成符合 JSON Schema 的表单配置。

## 技术栈

- **后端 API + MCP Server**：Node.js + TypeScript + Express/Fastify
- **配置生成引擎**：Python + FastAPI + LangChain/OpenAI SDK
- **前端**：React + TypeScript + Vite + Tailwind CSS
- **通信协议**：REST API + SSE（流式输出）+ MCP（JSON-RPC 2.0）
- **部署**：Docker Compose 容器化编排

## 核心能力

1. **Skill 消费**：加载外部 Skill 文件（JSON Schema、模板、规则），作为配置生成的规则源
2. **自然语言转换**：将自然语言描述转换为符合 JSON Schema 的表单配置 JSON
3. **MCP 协议服务**：暴露 tools 和 resources，供 AI 工具直接调用
4. **Web 前端界面**：自然语言输入、配置预览、字段编辑、JSON 导出
5. **REST API**：配置生成、校验、修正、查询等 HTTP 接口

## 项目结构

```
llm-to-modler/
├── packages/
│   ├── api/          # Node.js 后端（REST API + MCP Server + Skill 消费）
│   ├── engine/       # Python 配置生成引擎（LLM 调用）
│   ├── web/          # React 前端界面
│   └── shared/       # 共享类型定义和工具函数
├── openspec/         # OpenSpec 规格文件
└── .planning/        # GSD 规划文件
```

## 依赖关系

- **外部依赖**：njmind-modeler 项目动态生成并同步 Skill 文件到约定目录
- **LLM 服务**：OpenAI API 或兼容接口（用于自然语言处理）
