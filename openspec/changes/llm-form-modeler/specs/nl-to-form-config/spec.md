## ADDED Requirements

### Requirement: 自然语言输入接收
系统 SHALL 接收用户的自然语言文本输入，支持中文和英文描述表单需求。

#### Scenario: 中文自然语言输入
- **WHEN** 用户输入"创建一个包含姓名、手机号、性别的员工信息表单"
- **THEN** 系统接收输入并返回一个 requestId 用于跟踪

#### Scenario: 英文自然语言输入
- **WHEN** 用户输入"Create a form with name, email, and age fields"
- **THEN** 系统接收输入并正常处理

### Requirement: 配置生成引擎
系统 SHALL 将自然语言描述转换为符合 JSON Schema 的表单配置 JSON，包含 FormConfig 和 FormFieldConfig 列表。

#### Scenario: 简单表单生成
- **WHEN** 用户描述"创建一个联系表单，包含姓名（文本）、电话（数字）、留言（多行文本）"
- **THEN** 系统生成包含 3 个字段配置的 FormConfig JSON，字段类型分别为 text、number、text（multiline），且符合 form-config.schema.json

#### Scenario: 复杂表单生成
- **WHEN** 用户描述"创建一个请假申请表，包含请假类型（下拉选择：年假/事假/病假）、开始日期、结束日期、请假原因（富文本）、审批人（人员选择）"
- **THEN** 系统生成包含 5 个字段配置的 FormConfig JSON，字段类型和选项值符合描述，且符合 JSON Schema

#### Scenario: 生成结果包含字段校验规则
- **WHEN** 用户描述中包含隐含的校验需求（如"手机号"字段）
- **THEN** 系统 SHALL 自动为该字段添加合理的校验规则（如手机号格式校验）

### Requirement: 流式输出
系统 SHALL 通过 SSE（Server-Sent Events）流式返回生成结果，让用户实时看到生成进度。

#### Scenario: 流式返回生成过程
- **WHEN** 客户端通过 SSE 订阅配置生成结果
- **THEN** 系统依次推送：解析中 → 字段识别 → 配置生成 → 校验中 → 完成，每个阶段包含中间结果

#### Scenario: 生成完成
- **WHEN** 配置生成和校验完成
- **THEN** 系统推送最终事件，包含完整的 FormConfig JSON

### Requirement: 生成结果校验
系统 SHALL 在返回结果前使用 JSON Schema 对生成的配置进行自动校验，确保格式正确。

#### Scenario: 校验通过后返回
- **WHEN** LLM 生成的配置通过 JSON Schema 校验
- **THEN** 系统将配置作为最终结果返回

#### Scenario: 校验失败自动重试
- **WHEN** LLM 生成的配置未通过 JSON Schema 校验
- **THEN** 系统 SHALL 将校验错误信息反馈给 LLM 重新生成，最多重试 3 次

#### Scenario: 重试仍失败
- **WHEN** 重试 3 次后仍未通过校验
- **THEN** 系统返回最后一次生成结果，附带校验错误信息，标记为"需要手动调整"

### Requirement: 配置修正
系统 SHALL 支持用户对已生成的配置进行自然语言修正，在现有配置基础上增量修改。

#### Scenario: 增量修改字段
- **WHEN** 用户已有配置并输入"把手机号字段改成必填"
- **THEN** 系统基于现有配置，仅修改手机号字段的 required 属性为 true，其他字段保持不变

#### Scenario: 添加新字段
- **WHEN** 用户输入"再加一个邮箱字段"
- **THEN** 系统在现有配置中追加一个 email 字段配置
