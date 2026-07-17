## ADDED Requirements

### Requirement: MCP 协议初始化
系统 SHALL 实现 MCP 协议的初始化握手流程，支持 JSON-RPC 2.0 over HTTP。

#### Scenario: 客户端初始化连接
- **WHEN** 客户端发送 `initialize` 请求，包含 protocolVersion 和 capabilities
- **THEN** 系统返回服务端 capabilities（tools、resources）和支持的 protocolVersion

#### Scenario: 心跳检测
- **WHEN** 客户端发送 `ping` 请求
- **THEN** 系统返回空响应表示存活

### Requirement: MCP Tools 暴露
系统 SHALL 通过 MCP 协议暴露以下 tools，供 AI 工具调用：

- `get_form_config`：根据自然语言描述生成表单配置
- `create_form`：创建完整的表单配置
- `validate_form`：校验表单配置是否符合 Schema
- `list_templates`：列出可用的表单模板
- `get_template`：获取指定模板的详细内容
- `get_schema`：获取指定 JSON Schema
- `list_field_types`：列出所有支持的字段类型

#### Scenario: AI 工具调用 get_form_config
- **WHEN** AI 工具通过 MCP 调用 `get_form_config`，传入 `{"description": "创建一个包含姓名和年龄的表单"}`
- **THEN** 系统返回符合 JSON Schema 的 FormConfig JSON

#### Scenario: AI 工具调用 validate_form
- **WHEN** AI 工具通过 MCP 调用 `validate_form`，传入一个 FormConfig JSON
- **THEN** 系统返回校验结果（通过/失败 + 错误详情）

#### Scenario: AI 工具调用 list_templates
- **WHEN** AI 工具通过 MCP 调用 `list_templates`
- **THEN** 系统返回所有可用模板的列表，包含名称、分类、描述

### Requirement: MCP Resources 暴露
系统 SHALL 通过 MCP 协议暴露以下 resources：

- `njmind://schemas/form-config`：表单配置 Schema
- `njmind://schemas/form-field-config`：字段配置 Schema
- `njmind://guide`：配置指南
- `njmind://field-types`：字段类型定义

#### Scenario: AI 工具读取 resource
- **WHEN** AI 工具通过 MCP 读取 `njmind://guide`
- **THEN** 系统返回 guide.json 的内容

### Requirement: MCP 错误处理
系统 SHALL 按照 JSON-RPC 2.0 规范返回错误码和错误信息。

#### Scenario: 工具调用参数错误
- **WHEN** AI 工具调用 tool 时参数不符合要求
- **THEN** 系统返回 JSON-RPC 错误码 -32602（Invalid params）和具体错误描述

#### Scenario: 内部错误
- **WHEN** 系统内部发生异常
- **THEN** 系统返回 JSON-RPC 错误码 -32603（Internal error）和错误摘要
