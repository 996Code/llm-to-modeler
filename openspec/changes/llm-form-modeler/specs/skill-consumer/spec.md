## ADDED Requirements

### Requirement: Skill 文件目录加载
系统 SHALL 在启动时从配置的目录路径加载所有 Skill 文件（JSON Schema、模板、规则、指南），并解析到内存缓存中。

#### Scenario: 正常加载 Skill 文件
- **WHEN** 服务启动且 Skill 目录存在且包含有效文件
- **THEN** 系统解析所有 `.schema.json`、`.template.json`、`guide.json`、`RULES.md` 文件到内存，并记录加载数量和耗时

#### Scenario: Skill 目录不存在
- **WHEN** 服务启动但配置的 Skill 目录不存在
- **THEN** 系统 SHALL 记录错误日志并以非零退出码退出

#### Scenario: Skill 文件格式无效
- **WHEN** Skill 目录中存在格式无效的 JSON 文件
- **THEN** 系统 SHALL 跳过该文件，记录警告日志，继续加载其他文件

### Requirement: Skill 文件热更新
系统 SHALL 监听 Skill 目录的文件变化（新增、修改、删除），并在检测到变化时自动重新加载受影响的文件。

#### Scenario: 文件修改触发热更新
- **WHEN** Skill 目录中的某个 `.schema.json` 文件被修改
- **THEN** 系统在 2 秒内重新加载该文件，更新内存缓存，并记录更新日志

#### Scenario: 新文件添加
- **WHEN** Skill 目录中新增一个模板文件
- **THEN** 系统自动加载新文件并更新缓存

### Requirement: Skill 信息查询 API
系统 SHALL 提供 API 接口，返回当前已加载的 Skill 信息，包括字段类型列表、模板列表、Schema 列表、指南信息。

#### Scenario: 查询字段类型列表
- **WHEN** 客户端请求 `GET /api/skills/field-types`
- **THEN** 系统返回所有已加载的字段类型定义，包含类型名称、描述、配置属性

#### Scenario: 查询模板列表
- **WHEN** 客户端请求 `GET /api/skills/templates`
- **THEN** 系统返回所有已加载的模板信息，包含模板名称、分类、适用场景

### Requirement: JSON Schema 校验
系统 SHALL 使用加载的 JSON Schema 对生成的配置进行校验，确保输出符合规范。

#### Scenario: 校验通过
- **WHEN** 生成的配置 JSON 符合对应的 JSON Schema
- **THEN** 系统返回校验通过状态

#### Scenario: 校验失败
- **WHEN** 生成的配置 JSON 不符合 JSON Schema
- **THEN** 系统返回校验失败，包含具体的错误路径和错误信息
