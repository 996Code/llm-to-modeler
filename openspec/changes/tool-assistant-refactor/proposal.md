## Why

当前代码把 njmind 表单业务知识（`formFieldConfigVos`、`TYPE_TO_TEMPLATE`、4 套 prompt、REST 路径表）硬编码散落在 graph/nodes/sse/compressor 等 6 个文件里，与通用编排骨架（LangGraph 拓扑、SSE 桥接、压缩机制）耦合。这违反了项目作为"工具助手"的本质——**助手不该知道工具内部怎么工作**。随着要接入更多上层能力（流程、报表、BI），现有架构无法复用，每次都要重写管线。

本次改造把系统重构为**六边形分层**（Engine / SDK / Domain Pack），让 njmind 知识完全收口到一个 pack，Engine 零领域知识。同时引入借鉴 Claude Code 验证过的 5 项工程增强（追问工具化、工具并行、prompt section 缓存、压缩 sidechain、override/append 合并）。

> **权威来源**:本 proposal 及后续 design/specs/tasks 均基于已评审通过的 v4 设计文档 `docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`（commit eae49ca，1182 行）。如与本目录文件冲突，以 v4 设计文档为准。

## What Changes

### 架构重构（绞杀者模式 5 阶段）

- **新增三层目录结构**: `engine/`（通用内核）、`sdk/`（契约层 ABC）、`domains/njmind_form/`（领域工具包）
- **新增 Tool 协议**: `Tool` / `CompositeTool` 基类，Fail-Closed 默认值（`is_destructive=True`、`is_concurrency_safe=False`）
- **新增 ToolDispatcher**: 单轮多工具选择（LLM 返回 1..N 工具）+ `_partition_tool_calls` 按 `is_concurrency_safe` 分批并发执行
- **新增 AssetClient 抽象**: 上游 REST 调用收口，路径表进 pack 的 `config.yaml`，返回前 Unicode 清洗
- **新增 PromptLoader**: section 级缓存 + override/append 优先级装配
- **把 6 步 CREATE 管线搬进 `CreateFormTool`**: 固定拓扑活在复合工具内部，不污染 Engine
- **把 3 步 MODIFY 管线搬进 `ModifyFormTool`**

### 5 项工程增强（C.2 全部纳入）

- **C.2-A 追问内置 AskTool**: `ToolResult.ask` + `AskSpec`/`AskQuestion`/`AskOption`；SSE 推 `type=ask` → 用户带 `answers` 重发 → dispatcher 检测 `pending_ask` 重跑工具（上限 3 轮）。`ClarificationRaised` 异常保留作向后兼容
- **C.2-B 工具并行**: `is_concurrency_safe` + `_partition_tool_calls`（连续 safe 并发、unsafe 串行）+ context 修改延迟 apply
- **C.2-C Prompt section 缓存**: 静态 section 渲染后缓存、动态段每次重算，frontmatter `cacheable: false` 强制重算
- **C.2-D 压缩 forked sidechain**: 压缩在独立线程执行不阻塞主对话流，独立超时/重试，`compact_trace` 条目记录轨迹
- **C.2-E override/append**: `assemble()` 按 override→静态→动态→append 顺序拼装，override 不走 Jinja2 防注入

### 存储与接口

- **存储重建 append-only**: 新建 `events` 表（kind ∈ user/assistant/tool_result/compacted/compact_trace/checkpoint/ask）+ `session_meta` 表。**BREAKING**: 旧 conversations/messages 表重命名 `_legacy_` 留档，**老数据不迁移**，新会话从空表开始
- **接口重构**: **BREAKING** SSE result 的 data 重构为 `{type, tool, payload, summary}`（type ∈ config/ask/reply/error）；`/api/chat` 新增 `answers` 字段。前端配套同步改，一个版本内发齐，不写适配层

### 安全增强

- HttpAssetClient 返回前 Unicode 清洗（零宽字符/方向反转字符/PUA）
- Engine 启动挂 RedactFilter（正则 redact Bearer/sk-/cookie）
- 落库前 emit `confirm` SSE，用户确认才 persist

## Capabilities

### New Capabilities

- `tool-engine`: 通用编排内核——ToolDispatcher（单轮多工具选择+并发分批）、ConversationManager（append-only+压缩 sidechain）、StreamBridge（SSE 桥接）、PromptLoader（section 缓存+override/append）。零领域知识
- `tool-sdk`: 契约层——Tool/CompositeTool ABC（Fail-Closed 默认值）、ToolContext/ToolResult/AskSpec、AssetClient ABC、ToolRegistry。Tool 协议对标 Claude Code `src/Tool.ts`
- `njmind-form-pack`: njmind 表单工具包——CreateFormTool（6 步复合工具）、ModifyFormTool（3 步复合工具）、ChatTool、prompts/*.j2（section 装配）、HttpAssetClient（配 njmind 路径表）、config.yaml（TYPE_TO_TEMPLATE/路径）
- `context-compression`: 压缩系统——三级保护（70%阈值+熔断器+PTL防御）、forked sidechain 隔离、状态重启补偿（summarize_artifact+能力复灌）、compact_trace 轨迹
- `append-only-storage`: 事件流存储——events 表（7 种 kind）、session_meta 表、崩溃重放恢复、老数据不迁移

### Modified Capabilities

（无。本次是全新架构，旧 change `llm-form-modeler` 的 specs 尚未归档到 `openspec/specs/`，故全部作为 New Capabilities 处理。旧 change 保留不动。）

## Impact

### 受影响代码

- **新增**: `engine/`（dispatcher/conversation/stream/prompt_loader/logging_filter）、`sdk/`（tool/asset_client/registry/sanitize）、`domains/njmind_form/`（tools/prompts/adapters/models/config）、`adapters/http_asset_client.py`
- **删除**: `backend/src/graph/`（graph.py/nodes.py）、`backend/src/llm/prompt_builder.py`、`backend/src/services/upstream_client.py`（逻辑迁入 pack）
- **重构**: `backend/src/api/config.py`（走 ToolDispatcher）、`backend/src/api/sse.py`（result 钩子化）、`backend/src/ai/compressor.py`（机制留下、内容钩子化）、`backend/src/services/conversation_store.py`（改 append-only）

### 受影响接口

- `POST /api/chat`: 新增 `answers` 字段（C.2-A 追问恢复）
- SSE result 事件: data schema 重构为 `{type, tool, payload, summary}`（**BREAKING**）
- SSE 新增 `type=ask`（追问）、`type=error`（工具失败）两种 result 子类型

### 依赖

- 新增: Jinja2（prompt 模板渲染，已是 Python 生态常用）
- 保留: LangGraph（仅在 pack 工具内部使用，不再是 Engine 核心）、httpx、FastAPI、Pydantic、SQLite

### 验证标准（架构试金石）

- `grep -rE "form|formCode|template|field" engine/` 无结果
- 写一个 `DummyTool` + `DummyPack` 能跑通端到端（证明 Engine 不绑 njmind）
