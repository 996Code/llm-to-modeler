# Tool Assistant Refactor — 主索引

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 njmind 表单专属管线重构为「工具助手 + 可插拔工具包」六边形架构，Engine 零领域知识

**Architecture:** 三层（engine/sdk/domains）+ 5 项 Claude Code 工程增强（C.2-A/B/C/D/E）+ 绞杀者模式 5 阶段迁移

**Tech Stack:** Python + FastAPI + Jinja2 + Pydantic + SQLite（后端）/ Vue 3 + Ant Design Vue（前端）

**权威来源:**
- 设计文档：`docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`（v4，commit eae49ca）
- OpenSpec：`openspec/changes/tool-assistant-refactor/`
- 任务清单：`openspec/changes/tool-assistant-refactor/tasks.md`（78 任务）

---

## 5 份阶段计划（按依赖顺序）

| 阶段 | 计划文档 | 前置 | 核心交付 |
|------|---------|------|---------|
| 0 | [00-skeleton.md](./00-skeleton.md) | 无 | 目录骨架 + SDK ABC + Engine 空实现委托旧代码 |
| 1 | [01-asset-client.md](./01-asset-client.md) | 阶段 0 | HttpAssetClient + 路径表外置 + Unicode 清洗 |
| 2 | [02-prompt-loader.md](./02-prompt-loader.md) | 阶段 1 | PromptLoader + section 缓存（C.2-C）+ override/append（C.2-E） |
| 3 | [03-tools-and-dispatcher.md](./03-tools-and-dispatcher.md) | 阶段 2 | CreateFormTool/ModifyFormTool/ChatTool + Dispatcher 单轮多工具（C.2-A 追问 + C.2-B 并发） |
| 4 | [04-storage-and-compression.md](./04-storage-and-compression.md) | 阶段 3 | append-only 重建（老数据不迁移）+ RedactFilter + 压缩 sidechain（C.2-D）+ 全回归 |

**前端配套**（阶段 3-4 期间穿插）：见各阶段计划的"前端配套"小节，约束是"一个版本内同步发"。

---

## 执行规则

1. **严格顺序**：阶段必须按 0→1→2→3→4 执行，因为后阶段依赖前阶段建立的抽象
2. **每阶段可回滚**：阶段 0-3 失败时删除新目录即可恢复；阶段 4 是终态
3. **每阶段独立提交**：完成一阶段的全套测试后 commit，不跨阶段积压
4. **架构试金石贯穿**：每阶段结束时跑 `grep -rE "form|formCode|template|field" engine/`，结果应一直为空
5. **TDD**：每个任务"写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交"

---

## 全局约束（5 阶段共用）

- **Python**: 用 `pytest`，测试放 `backend/tests/` 对应子目录
- **命名规范**: 类用 PascalCase，函数/变量用 snake_case，常量用 UPPER_SNAKE
- **Jinja2 变量命名**: 统一用 `user_input` / `history` / `artifact` / `guide` / `compressed_history`
- **错误处理**: 工具内异常不向上抛，包装成 `ToolResult.error_for_llm` 回流
- **Fail-Closed**: 所有安全相关默认值保守（is_destructive=True、is_concurrency_safe=False）
- **注释密度**: 对标 Claude Code——复杂逻辑写 why，简单代码不写注释

---

## 验收（全部阶段完成后）

- [ ] `grep -rE "form|formCode|template|field" engine/` 无结果
- [ ] DummyTool + DummyPack 端到端跑通
- [ ] 三意图（create/modify/chat）无功能回归
- [ ] C.2 五项全部可用：追问重跑 / 工具并发 / section 缓存 / 压缩 sidechain / override-append
- [ ] 接口约束达成：SSE/请求体重构为 {type,tool,payload,summary}，前端配套同步改
- [ ] 老数据不迁移：旧表重命名 _legacy_，新表从零开始
- [ ] 安全防护：Unicode 清洗 + 日志 redact + 落库确认全部生效
