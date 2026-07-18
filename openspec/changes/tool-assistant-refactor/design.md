## Context

本项目 `llm-to-modler` 是一个 LLM 驱动的工具助手，通过自然语言调用上层 njmind-modeler 的能力生成低代码表单配置。MVP（commit c30a97f）已上线运行，采用 LangGraph 意图驱动管线 + SSE 流式 + 多轮压缩。

**当前问题**：njmind 业务知识（`formFieldConfigVos` 字段名、`TYPE_TO_TEMPLATE` 映射、4 套 prompt、REST 路径表）硬编码散落在 graph/nodes/sse/compressor 等 6 个文件，与通用编排骨架深度耦合。这违反"工具助手不该知道工具内部"的本质，且无法复用到其他领域（流程、报表、BI）。

**权威设计来源**：本 change 的完整技术设计在 `docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`（v4，commit eae49ca，1182 行，已评审通过）。本 design.md 浓缩关键决策，细节引用该文档对应章节。

## Goals / Non-Goals

**Goals:**
- 把 njmind 业务知识完全收口到 `domains/njmind_form/` 一个 pack
- `engine/` 零领域知识（架构试金石：`grep -rE "form|formCode|template|field" engine/` 无结果）
- 引入 5 项 Claude Code 验证过的工程增强（追问工具化、工具并行、prompt section 缓存、压缩 sidechain、override/append）
- 用绞杀者模式 5 阶段迁移，每阶段可运行可测试可回滚

**Non-Goals:**
- 多步 Agent Loop（LLM 看完结果再决定下一步；当前单轮多工具不循环）
- MCP 客户端能力（上游是 REST 不是 MCP）
- 多 pack 共存路由（目前只一个 pack；ToolRegistry 已支持但无跨 pack 选择策略）
- 动态工具发现、Tool 并行的跨工具状态依赖
- 老数据迁移（开发期无生产数据）

## Decisions

### D1: 六边形分层（依赖反转）

**选择**: Engine / SDK / Domain Pack 三层，依赖方向永远向内。Engine 只依赖 SDK 抽象，绝不 import 具体 pack。

**否决方案**:
- 简单分层（无依赖反转）→ 换领域时 Engine 要改
- 纯 JSON/YAML 配置 → 表达不了复杂逻辑（拼音转 key、类型推断）
- MCP-First → 上游是 REST 不是 MCP，绑 MCP 要造适配层

**详见**: v4 §3.1 三层架构、附录 A 决策记录

### D2: 固定拓扑活在复合工具内

**选择**: CREATE 的 6 步管线搬进 `CreateFormTool.steps` + `run_pipeline`，MODIFY 的 3 步搬进 `ModifyFormTool`。Engine 不感知管线，只调 `tool.execute()`。

**否决方案**:
- 全声明式管线（每领域自己声明拓扑）→ 重复劳动
- Engine 拥有管线 → Engine 污染领域知识

**详见**: v4 §4.1 CompositeTool、§6.2 CreateFormTool

### D3: 单轮多工具选择（v4 升级自单步）

**选择**: `ToolDispatcher._select_tools` 调一次 LLM 返回 1..N 个工具，`_partition_tool_calls` 按 `is_concurrency_safe` 分批（连续 safe 并发、unsafe 串行）。不进入多步 loop。

**否决方案**:
- 单步单工具（v3）→ "加字段A 和 删字段B"需求要两轮
- 多步 Agent Loop → 不可控、成本高

**详见**: v4 §5.1 ToolDispatcher、附录 C.3 第 2 条

### D4: 追问异步重跑（C.2-A）

**选择**: 工具产出 `ToolResult.ask` → SSE 推 `type=ask` → 用户回答带 `answers` 重发新请求 → dispatcher 检测 `state["pending_ask"]` 重跑同一工具（上限 3 轮）。

**关键约束**: SSE 是单向推送，不能在同一连接里等回答，所以追问必须跨请求。

**否决方案**:
- 异常短路（v3 `ClarificationRaised`）→ 追问答案不进历史，不可多轮
- WebSocket 双向 → 改动太大

**详见**: v4 §4.1 AskSpec、§5.1 `_run_single`/`_resume_ask`

### D5: 压缩 forked sidechain（C.2-D）

**选择**: 压缩在独立线程执行，主对话流不等待。失败由三级保护兜底（熔断器 3 次失败停 120s / PTL 剥洋葱重试 / 降级截断）。`compact_trace` 条目记录轨迹供审计。

**详见**: v4 §5.2 ConversationManager.compress

### D6: 老数据不迁移

**选择**: 存储改造时旧 conversations/messages 表重命名 `_legacy_` 留档，新 events 表从零开始。

**理由**: 开发期无生产数据，避免迁移脚本、schema 兼容、灰度切换的风险。

**详见**: v4 §5.2"与现有 SQLite 的关系"

### D7: 接口后端重构 + 前端同步改

**选择**: SSE result 重构为 `{type, tool, payload, summary}`，`/api/chat` 加 `answers` 字段。前端配套同步改，一个版本内发齐。

**否决方案**:
- 适配层逐字段不变 → 技术债
- 新旧双接口 → 长期双轨

**详见**: v4 §8.3 SSE 协议

## Risks / Trade-offs

- **[迁移期双套代码混乱]** → 绞杀者模式：每阶段旧代码先委托、后删除，从不同时存在两套实现（阶段验收见 v4 §7）
- **[上游 Unicode 隐写注入]** → HttpAssetClient 返回前 `sanitize_obj`（NFKC + 删零宽/方向反转字符/PUA）
- **[日志泄漏 Authorization/cookie]** → Engine 启动挂 RedactFilter
- **[LLM 选工具准确率]** → `when` 字段约束 + 兜底（选错时 `validate_input` 失败走 `error_for_llm` 回流让下轮自纠）
- **[追问重跑死循环]** → `_max_clarify_rounds` 上限 3 轮，超限 emit error 并清除 pending_ask
- **[Tool 并发竞态]** → context 修改延迟到批次全部完成后统一 apply（借鉴 CC 延迟 contextModifier）
- **[压缩 sidechain 主从不一致]** → 主对话流先返回 keep-recent，压缩结果异步写回；compact_trace 记录供对账
- **[前后端接口同步改协调]** → 约束"一个版本内同步发"，CI 加前后端契约测试

## Migration Plan

绞杀者模式 5 阶段，每阶段应用都能跑、能测、能回滚（详见 v4 §7）：

| 阶段 | 内容 | 可回滚 |
|------|------|--------|
| 0 | 骨架搭建：engine/sdk/domains 目录 + ABC + 空实现委托旧代码 | 删目录 |
| 1 | 抽 AssetClient + Unicode 清洗 + 连接复用 | 删目录 |
| 2 | 抽 Prompt + PromptLoader（C.2-C 缓存 + C.2-E override/append）+ 注入防护 | 删目录 |
| 3 | 管线搬进工具 + ToolDispatcher（C.2-A 追问 + C.2-B 并发）+ 落库确认 | 切回旧 graph |
| 4 | 存储重建 append-only + 日志安全 + C.2-D 压缩 sidechain + 清理 + 全回归 | — |

**关键里程碑**:
- 阶段 3 结束：`/api/chat` 走 ToolDispatcher，三意图（create/modify/general）端到端通
- 阶段 4 结束：架构试金石 grep 通过，Engine 零领域知识，C.2 五项全部落地

## Open Questions

（无。v4 设计文档已评审通过，所有方向性问题已对齐：多领域复用框架定位、Python 插件包接入、固定骨架、统一 artifact 容器、plugin 内 Jinja2 模板、单步选择[升级为单轮多工具]、C.2 五项全纳入、老数据不迁移、后端重构前端同步改。）
