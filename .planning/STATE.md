# 项目状态

## 当前位置

- **阶段**：tool-assistant-refactor（里程碑 2）
- **状态**：规格已定义，待规划
- **上一阶段**：llm-form-modeler（里程碑 1，✅ 已完成，MVP 上线 commit c30a97f）

## OpenSpec 关联

- **Change**：tool-assistant-refactor
- **路径**：openspec/changes/tool-assistant-refactor/
- **产出文件**：
  - proposal.md ✓（引用 v4 设计文档作为权威来源）
  - design.md ✓（7 项关键决策 D1-D7 + 风险表 + 5 阶段迁移计划）
  - specs/ ✓（5 个 capability：tool-engine / tool-sdk / njmind-form-pack / context-compression / append-only-storage）
  - tasks.md ✓（7 大任务组，覆盖 5 阶段迁移 + 前端配套 + 验收）
- **validate**：✅ `openspec validate tool-assistant-refactor` 通过

## 权威设计来源

- **文档**：`docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`
- **版本**：v4（commit eae49ca，1182 行，已评审通过）
- **关系**：OpenSpec 产出基于并引用 v4 设计文档，如冲突以 v4 为准

## 核心架构决策（详见 design.md）

1. **六边形分层**（D1）：engine/sdk/domains 三层，依赖反转，Engine 零领域知识
2. **固定拓扑活在复合工具内**（D2）：6 步 CREATE / 3 步 MODIFY 管线搬进 CompositeTool
3. **单轮多工具选择**（D3）：ToolDispatcher 一次 LLM 调用选 1..N 工具，is_concurrency_safe 分批并发
4. **追问异步重跑**（D4）：ToolResult.ask + pending_ask + answers 重发（上限 3 轮）
5. **压缩 forked sidechain**（D5）：独立线程不阻塞主对话流
6. **老数据不迁移**（D6）：旧表重命名 _legacy_ 留档，新表从零开始
7. **接口后端重构 + 前端同步改**（D7）：SSE 统一 {type,tool,payload,summary}，不写适配层

## 实际技术栈（MVP 已落地）

> 注：旧 STATE.md 记载的 Node.js + React 是早期规划，实际 MVP 用以下技术栈实现

- **后端**：Python + FastAPI + LangGraph（port 18080）
- **前端**：Vue 3 + Ant Design Vue + Vite（port 13080）
- **存储**：SQLite（conversations + messages 两表）
- **LLM**：OpenAI 兼容接口（Qwen3，port 1234）
- **上游**：njmind-modeler REST API

## 活动日志

- 2026-07-16：/ai:spec 完成（旧 MVP 规格 llm-form-modeler）
- 2026-07-16：GSD 轻量初始化完成
- 2026-07-16：MVP 实现并上线（commit c30a97f，实际用 Python+Vue，偏离早期 Node.js+React 规划）
- 2026-07-18：架构改造设计文档 v1→v4 迭代完成（commit eae49ca，融入 Claude Code 设计借鉴）
- 2026-07-18：/ai:spec 完成（新 change tool-assistant-refactor，基于 v4 设计文档）
- 2026-07-18：GSD 桥接更新（ROADMAP 加里程碑 2，STATE 切换到新 phase）

## 下一步

运行 `/ai-plan tool-assistant-refactor` 将规格转为执行计划
