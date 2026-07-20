## ADDED Requirements

### Requirement: Tool / CompositeTool 契约

SDK SHALL 提供 `Tool` 抽象基类（对标 Claude Code `src/Tool.ts`），包含：能力描述（name/description/when）、安全声明（Fail-Closed 默认值）、`input_schema`/`execute`/`validate_input` 方法、可选 hooks（`summarize_artifact`/`title_for`/`requires_follow_up`）。

SDK SHALL 提供 `CompositeTool` 子类（对标 CC Skill），封装多步 pipeline：声明 `steps` 列表，`run_pipeline` 顺序执行 `_step_<name>` 方法，每步自动 emit stage 事件，步内可抛 `ClarificationRaised` 短路或重跑前序 step 实现 retry。

#### Scenario: CompositeTool step 编排
- **WHEN** `CreateFormTool` 声明 `steps=["fetch_guide","list_assets","parse_fields","fetch_templates","generate","validate"]`
- **THEN** `run_pipeline` 按序调用每个 `_step_<name>`，每步 emit 一个 stage SSE 事件

#### Scenario: step 内 retry
- **WHEN** `_step_validate` 校验失败且 retry_count < MAX_RETRIES
- **THEN** 重跑 `_step_generate` 后递归重校验；超限则把错误写进 `ToolResult.extra`，Engine 照常发 result

#### Scenario: step 内追问短路
- **WHEN** `_step_parse_fields` 发现信息不足
- **THEN** 产出 `ToolResult.ask`（v4）或抛 `ClarificationRaised`（兼容），`run_pipeline` 立即上抛

### Requirement: ToolContext 依赖注入

SDK SHALL 提供 `ToolContext`，由 Engine 注入工具执行时所需依赖：`llm_client`（chat/chat_json）、`asset_client`（取模板/schema/guide）、`conversation`（读写历史）、`emit`（发 SSE）、`forward_headers`（转发上游的请求头）。

#### Scenario: 工具通过 ctx 调 LLM
- **WHEN** 工具执行中需要调 LLM
- **THEN** 通过 `ctx.llm_client.chat_json(...)` 调用，client 实例与降级策略由 Engine 管

#### Scenario: 工具通过 ctx emit 进度
- **WHEN** 工具执行到子步骤
- **THEN** 通过 `ctx.emit("stage", "parse_fields", message="解析字段")` 发 SSE 进度事件

### Requirement: ToolResult 三层结构

SDK SHALL 提供 `ToolResult` 数据类，三层设计：
- `artifact`: 不透明制品（dict），Engine 从不读内部结构，只做传递/存储/让 pack 格式化
- `summary`: 标准化摘要，进 ConversationManager 历史，是压缩器处理的唯一对象
- `extra`: 领域自由扩展，不进历史

CompositeTool 中间 step 产出（parsed_fields、fetched_templates 等）SHALL 只活在 state 内，绝不进 ConversationManager——只有最终 summary 入历史。

#### Scenario: 制品不透明传递
- **WHEN** CreateFormTool 产出 formConfig 制品
- **THEN** Engine 只做传递/存储/调 `tool.format_result()`，从不访问 `formCode`/`formFieldConfigVos` 等字段名

#### Scenario: 中间产出不进历史
- **WHEN** `parse_fields` step 解析出字段列表
- **THEN** 该列表只写进 state，不写进 ConversationManager（避免对话历史膨胀）

### Requirement: AssetClient 抽象与路径表外置

SDK SHALL 提供 `AssetClient` 抽象基类，pack 通过它取模板/schema/guide/校验/持久化，不关心是 HTTP 还是本地。

通用实现 `HttpAssetClient`（在 `adapters/`）SHALL 配置 `base_url + path_map`，路径表从 pack 的 `config.yaml` 加载。

HttpAssetClient 所有 `get_*` 方法返回前 SHALL 调用 `sanitize_obj` 清除零宽字符/方向反转字符/PUA（Unicode 隐写注入防护）。

#### Scenario: 路径表外置
- **WHEN** njmind_form pack 配置路径表 `/api/mcp/templates/list-templates` 等
- **THEN** 换部署环境只需改 config.yaml，不改代码

#### Scenario: Unicode 清洗
- **WHEN** 上游返回的模板含零宽字符 `\u200B` 或方向反转 `\u202A`
- **THEN** HttpAssetClient 返回前经 `sanitize_obj` 清除，prompt 注入面封堵

### Requirement: ToolRegistry 与 LLM 选择

SDK SHALL 提供 `ToolRegistry`，pack 启动时静态注册工具。`describe_for_llm(state)` 方法生成给 LLM 看的工具清单（name/description/when + 当前 state 下哪些可用）。

#### Scenario: 工具注册与发现
- **WHEN** njmind_form pack 注册 CreateFormTool/ModifyFormTool/ChatTool
- **THEN** ToolRegistry.all() 返回这 3 个工具，dispatcher 能按名字查找

#### Scenario: 按状态过滤工具
- **WHEN** 当前 state 无 artifact，但用户输入"加字段"
- **THEN** `describe_for_llm` 标注 ModifyFormTool 不可用（无现成表单可改）
