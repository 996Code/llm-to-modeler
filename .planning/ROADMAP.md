# 路线图

## 里程碑 1：LLM Form Modeler MVP

| 阶段 | 名称 | 状态 | 描述 |
|------|------|------|------|
| 1 | llm-form-modeler | 已完成 | 自然语言生成表单配置的核心功能（MVP 已上线，commit c30a97f） |

### 阶段详情

#### 阶段 1：llm-form-modeler

**目标**：实现自然语言 → 表单配置的完整流程，提供 MCP 接口和 Web 界面

**状态**：✅ 已完成（MVP 上线，采用 Python + LangGraph + FastAPI + Vue 实现）

---

## 里程碑 2：工具助手架构改造

| 阶段 | 名称 | 状态 | 描述 |
|------|------|------|------|
| 2 | tool-assistant-refactor | 规格已定义，待规划 | 把 njmind 表单专属管线重构为「工具助手 + 可插拔工具包」六边形架构，Engine 零领域知识 |

### 阶段详情

#### 阶段 2：tool-assistant-refactor

**目标**：把 njmind 业务知识收口到 `domains/njmind_form/` 一个 pack，Engine 零领域知识；引入 5 项 Claude Code 验证过的工程增强

**核心改动**：
- 三层六边形分层（engine/sdk/domains），依赖反转
- 5 项增强：追问内置 AskTool / Tool 并行 / prompt section 缓存 / 压缩 sidechain / override-append
- 存储重建 append-only（老数据不迁移）
- 接口后端重构 + 前端同步改（SSE 统一为 {type,tool,payload,summary}）
- 绞杀者模式 5 阶段迁移

**OpenSpec Change**：`openspec/changes/tool-assistant-refactor/`

**权威设计来源**：`docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`（v4，commit eae49ca，已评审通过）
