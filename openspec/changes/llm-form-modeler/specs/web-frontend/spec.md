## ADDED Requirements

### Requirement: 对话窗口界面
系统 SHALL 提供 Web 对话界面，用户通过自然语言与系统交互生成表单配置。

#### Scenario: 输入自然语言描述
- **WHEN** 用户在对话输入框中输入"创建一个员工信息表单，包含姓名、工号、部门"并点击发送
- **THEN** 系统显示用户消息气泡，并调用配置生成引擎，显示 AI 思考中状态

#### Scenario: 显示 AI 回复
- **WHEN** 配置生成完成
- **THEN** 系统显示 AI 回复气泡，包含生成结果摘要（如"已为您生成员工表单配置，包含 3 个字段"）

#### Scenario: 多轮对话修正
- **WHEN** 用户继续输入"把工号改成必填"
- **THEN** 系统基于现有配置进行增量修改，显示更新后的结果

### Requirement: JSON 输出展示面板
系统 SHALL 提供 JSON 输出展示面板，实时展示生成的表单配置 JSON。

#### Scenario: JSON 语法高亮展示
- **WHEN** 配置生成完成
- **THEN** 右侧面板以 Monaco Editor 语法高亮方式展示完整的 FormConfig JSON

#### Scenario: JSON 只读模式
- **WHEN** 用户查看 JSON 输出
- **THEN** JSON 编辑器默认为只读模式，用户可复制但不可直接编辑（修改通过对话完成）

### Requirement: JSON 导出功能
系统 SHALL 支持将配置导出为 JSON 文件或复制到剪贴板。

#### Scenario: 导出 JSON 文件
- **WHEN** 用户点击"导出"按钮
- **THEN** 系统下载当前配置的 JSON 文件，文件名为 `form-config-{timestamp}.json`

#### Scenario: 复制到剪贴板
- **WHEN** 用户点击"复制 JSON"按钮
- **THEN** 系统将配置 JSON 复制到系统剪贴板，显示"已复制"提示

### Requirement: 对话历史管理
系统 SHALL 支持查看当前会话的对话历史，支持新建会话。

#### Scenario: 新建会话
- **WHEN** 用户点击"新建会话"按钮
- **THEN** 系统清空当前对话和 JSON 输出，开始新的配置生成会话

#### Scenario: 会话列表
- **WHEN** 页面加载时存在多个会话
- **THEN** 左侧边栏显示会话列表，用户可切换查看历史会话

### Requirement: 响应式布局
系统 SHALL 支持响应式布局，在桌面端和移动端均可正常使用。

#### Scenario: 桌面端布局
- **WHEN** 屏幕宽度 ≥ 1024px
- **THEN** 界面采用左右布局：左侧对话区域（60%）、右侧 JSON 展示区域（40%）

#### Scenario: 移动端布局
- **WHEN** 屏幕宽度 < 1024px
- **THEN** 界面采用上下布局：上方对话区域、下方 JSON 展示区域（可折叠）
