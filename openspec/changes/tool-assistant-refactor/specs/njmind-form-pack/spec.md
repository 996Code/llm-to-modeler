## ADDED Requirements

### Requirement: CreateFormTool 复合工具

njmind_form pack SHALL 提供 `CreateFormTool`（继承 CompositeTool），封装当前的 6 步 CREATE 管线：`fetch_guide → list_assets → parse_fields → fetch_templates → generate → validate`。

所有 njmind 业务字段名（`formCode`/`formName`/`formFieldConfigVos`/`fieldTitleKey`/`fieldTitleText`/`formFieldType` 等）SHALL 只出现在 pack 内部，绝不出现在 engine/。

`TYPE_TO_TEMPLATE`/`TYPE_NAMES` 映射表 SHALL 放在 pack 的 `config.yaml`。

#### Scenario: 6 步管线生成表单
- **WHEN** 用户输入"创建请假申请表，包含申请人、请假类型、开始日期、结束日期"
- **THEN** CreateFormTool 依次执行 6 个 step，每步 emit stage 事件，最终产出 formConfig 制品

#### Scenario: 校验失败 retry
- **WHEN** `_step_validate` 调上游 `/forms/validate` 返回不通过
- **THEN** 重跑 `_step_generate`（带上 validation_errors 反馈），retry_count 递增，上限 3 次

#### Scenario: 落库前确认
- **WHEN** validate 通过，准备调上游 `/forms/create` 持久化
- **THEN** persist 前 emit `confirm` SSE 事件，用户确认才继续；ToolContext 支持 `dry_run` 跳过 persist 返回预览

### Requirement: ModifyFormTool 复合工具

njmind_form pack SHALL 提供 `ModifyFormTool`（继承 CompositeTool），封装 3 步 MODIFY 管线：`fetch_guide → modify → validate`。从 `state["source_artifact"]`（已有配置）出发，保留原有字段，按用户指令增删改。

#### Scenario: 加字段
- **WHEN** state 有已有 formConfig，用户输入"加一个请假原因字段"
- **THEN** ModifyFormTool 执行 modify step，在现有字段基础上追加，保留原有字段不丢失

#### Scenario: 无 artifact 不可修改
- **WHEN** state 无 source_artifact，用户输入"加字段"
- **THEN** `_select_tools` 阶段 ToolRegistry 标注 ModifyFormTool 不可用，LLM 选不到它

### Requirement: ChatTool 闲聊工具

njmind_form pack SHALL 提供 `ChatTool`（继承 Tool），处理与表单无关的闲聊/打招呼/解释性问题。声明 `is_concurrency_safe=True`、`is_read_only=True`。

#### Scenario: 闲聊回复
- **WHEN** 用户输入"你好"或"你是谁"
- **THEN** ChatTool 调 LLM 返回文本回复，`ToolResult.reply` 带文本，`ToolResult.summary` 带标准化摘要

### Requirement: pack 内 Jinja2 prompt 模板与 section 装配

njmind_form pack 的 prompts SHALL 放在 `prompts/` 目录，按 section 模式组织：
- 静态片段放 `prompts/_sections/`（intro/field_types/output_rules/safety）
- 工具 prompt 用 Jinja2 `{% include %}` 引用片段
- 动态内容（当前 artifact、压缩历史）作为独立 context 注入，不塞进模板变量

pack 渲染的领域 prompt 视为 trusted；AssetClient 返回的模板/数据 SHALL 作为 user-role 或独立 section 注入，**绝不进 Jinja2 变量渲染**（注入防护）。

#### Scenario: section 复用
- **WHEN** generate.j2 和 modify.j2 都需要字段类型说明
- **THEN** 两者都 `{% include '_sections/field_types.j2' %}`，避免重复

#### Scenario: 注入防护
- **WHEN** 上游返回的模板含 `{{ user_input }}` 这类 Jinja2 语法
- **THEN** 作为 user-role 纯文本注入，不被 Jinja2 渲染执行

### Requirement: njmind 路径表与 config.yaml

njmind_form pack 的 `config.yaml` SHALL 承载所有 njmind 专属的静态映射：
- 上游 REST 路径表（templates/schemas/guides/forms 等 9 个端点）
- `TYPE_TO_TEMPLATE`（字段类型→模板文件名）
- `TYPE_NAMES`（字段类型→中文名）

#### Scenario: 路径表集中管理
- **WHEN** 上游 njmind-modeler 升级 API 路径
- **THEN** 只改 config.yaml 对应条目，不改代码
