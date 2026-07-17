## ADDED Requirements

### Requirement: 配置生成 API
系统 SHALL 提供 REST API 接口，接收自然语言描述并返回生成的表单配置。

#### Scenario: POST 生成配置
- **WHEN** 客户端发送 `POST /api/config/generate`，body 为 `{"description": "创建包含姓名和手机号的表单"}`
- **THEN** 系统返回 `{"requestId": "...", "status": "processing"}`，并通过 SSE 推送生成结果

#### Scenario: GET 查询生成结果
- **WHEN** 客户端发送 `GET /api/config/result/{requestId}`
- **THEN** 系统返回该请求的当前状态和结果（如已完成则包含完整 FormConfig JSON）

### Requirement: 配置校验 API
系统 SHALL 提供 REST API 接口，校验给定的表单配置是否符合 JSON Schema。

#### Scenario: 校验有效配置
- **WHEN** 客户端发送 `POST /api/config/validate`，body 为有效的 FormConfig JSON
- **THEN** 系统返回 `{"valid": true, "errors": []}`

#### Scenario: 校验无效配置
- **WHEN** 客户端发送 `POST /api/config/validate`，body 为无效的 FormConfig JSON
- **THEN** 系统返回 `{"valid": false, "errors": [{"path": "...", "message": "..."}]}`

### Requirement: 配置修正 API
系统 SHALL 提供 REST API 接口，支持基于现有配置进行自然语言修正。

#### Scenario: 增量修正配置
- **WHEN** 客户端发送 `POST /api/config/modify`，body 为 `{"currentConfig": {...}, "instruction": "把手机号改为必填"}`
- **THEN** 系统返回修正后的完整 FormConfig JSON

### Requirement: 模板查询 API
系统 SHALL 提供 REST API 接口，查询可用的表单模板。

#### Scenario: 获取模板列表
- **WHEN** 客户端发送 `GET /api/templates`
- **THEN** 系统返回所有可用模板列表，包含名称、分类、描述

#### Scenario: 获取模板详情
- **WHEN** 客户端发送 `GET /api/templates/{templateName}`
- **THEN** 系统返回指定模板的完整 JSON 内容

### Requirement: Schema 查询 API
系统 SHALL 提供 REST API 接口，查询 JSON Schema 定义。

#### Scenario: 获取 Schema 列表
- **WHEN** 客户端发送 `GET /api/schemas`
- **THEN** 系统返回所有可用 Schema 列表

#### Scenario: 获取指定 Schema
- **WHEN** 客户端发送 `GET /api/schemas/{schemaName}`
- **THEN** 系统返回指定 Schema 的完整 JSON 定义

### Requirement: API 错误处理
系统 SHALL 对所有 API 请求返回统一的错误格式。

#### Scenario: 参数错误
- **WHEN** 请求参数不符合要求
- **THEN** 系统返回 HTTP 400，body 为 `{"code": "INVALID_PARAMS", "message": "...", "details": [...]}`

#### Scenario: 内部错误
- **WHEN** 系统内部发生未处理异常
- **THEN** 系统返回 HTTP 500，body 为 `{"code": "INTERNAL_ERROR", "message": "服务器内部错误"}`，不暴露内部细节

#### Scenario: 请求限流
- **WHEN** 客户端请求频率超过限制
- **THEN** 系统返回 HTTP 429，body 为 `{"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试"}`
