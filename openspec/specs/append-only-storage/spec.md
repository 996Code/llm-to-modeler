# append-only-storage Specification

## Purpose
TBD - created by archiving change tool-assistant-refactor. Update Purpose after archive.
## Requirements
### Requirement: append-only 事件流存储

存储 SHALL 采用 append-only 事件流模型（对标 Claude Code `sessionStorage.ts` 的 JSONL）：只追加不覆盖，压缩也不删旧行而是写一条 compacted 条目。

`events` 表 SHALL 包含 `kind` 列，取值范围：
- `user`: 用户输入
- `assistant`: 助手回复
- `tool_result`: 工具产出（summary 标准化后）
- `compacted`: 压缩点标记（其后为 keep-recent，其前为已压缩）
- `compact_trace`: 压缩轨迹（审计用，不进 messages 重建）
- `checkpoint`: artifact 快照、active_tool 等持久化
- `ask`: pending_ask 现场（C.2-A 追问恢复）

#### Scenario: 只追加不覆盖
- **WHEN** 同一会话多次轮次交互
- **THEN** 每轮都 INSERT 新行，从不 UPDATE 旧行

#### Scenario: 压缩保留原始
- **WHEN** 触发压缩
- **THEN** 不删除旧行，而是写一条 `kind=compacted` 条目标记压缩点，原始消息完整保留

#### Scenario: 崩溃重放恢复
- **WHEN** 写 100 轮后进程崩溃重启
- **THEN** 读取 events 表重放，状态完整恢复（按 kind 分流重建 messages + checkpoint + pending_ask）

### Requirement: 老数据不迁移

存储改造时，旧的 `conversations` + `messages` 表 SHALL 重命名为 `_legacy_` 前缀留档备查，**不导入数据到新表**。新会话从空表开始。

开发期无生产数据需要保护，避免迁移脚本、schema 兼容、灰度切换的风险。

#### Scenario: 旧表保留不导入
- **WHEN** 阶段 4 存储重建
- **THEN** 旧表 `ALTER RENAME TO _legacy_conversations`，新 `events` 表为空，旧数据不导入

#### Scenario: 新会话从空表开始
- **WHEN** 存储改造后首次创建会话
- **THEN** 新会话 ID 在 `events` 表无历史记录，从零开始

### Requirement: SessionMeta 列表轻量查询

列表页 SHALL 通过单独的 `session_meta` 表查询（title/summary/updated_at），不 JOIN events 表（对标 CC lite reader 只读头尾）。

#### Scenario: 列表页性能
- **WHEN** 用户打开会话列表页
- **THEN** 只查 `session_meta` 表，O(N) 复杂度（N=会话数），不扫描 events 表的全部消息

#### Scenario: 元数据同步
- **WHEN** 某会话有新消息写入 events
- **THEN** `session_meta` 表对应行的 title/summary/updated_at 同步更新（由 ConversationManager.save 维护）

### Requirement: pending_ask 持久化与恢复（C.2-A）

追问现场 SHALL 持久化到 events 表（`kind=ask`），包含：tool 名、AskSpec 内容、当前 round。进程崩溃重启后能恢复追问状态。

#### Scenario: 追问现场持久化
- **WHEN** 工具产出 `ToolResult.ask`，dispatcher 保存 `state["pending_ask"]`
- **THEN** 写一条 `kind=ask` 的 events 条目，含 tool 名、AskSpec、round

#### Scenario: 崩溃后恢复追问
- **WHEN** 追问发出后进程崩溃，用户重新连接
- **THEN** `load_state` 检测到 `kind=ask` 条目，重建 pending_ask，前端能继续展示追问 UI

### Requirement: 日志凭证 redact

Engine 启动时 SHALL 挂载 RedactFilter（对标 CC `secretScanner.ts`），对 `Bearer`/`sk-`/`cookie` 等凭证模式做正则 redact，防止任何 `logger.info` 或 traceback 泄漏。

#### Scenario: 日志 redact 生效
- **WHEN** 代码执行 `logger.info(forward_headers)` 且 headers 含 `Authorization: Bearer xxx`
- **THEN** 日志输出显示 `Authorization: ***REDACTED***`

#### Scenario: 异常栈不泄漏
- **WHEN** 工具执行抛异常，异常栈包含 cookie 值
- **THEN** 异常日志中的 cookie 值被 redact

