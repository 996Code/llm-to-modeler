## Why

当前 njmind-modeler-mcp-generator 通过 Java 编译时生成表单配置的 JSON Schema、模板和 Skill 文件，耦合在 Maven 构建流程中，更新迭代需要重新编译和部署。需要一个独立的项目，让用户通过自然语言描述直接生成表单配置底码，同时保持 Skill 驱动的动态更新能力——Skill 文件由 njmind-modeler 项目动态生成和推送，而非硬编码在本项目中。

## What Changes

- 新建独立项目 `llm-to-modler`，采用 Node.js + Python 技术栈
- 实现自然语言 → 表单配置（FormFieldConfig / FormConfig）的转换引擎
- 基于 Skill 驱动的配置生成：Skill 文件由外部 njmind-modeler 项目动态生成并同步，本项目消费 Skill 作为配置规则源
- 提供 MCP 协议接口，供 AI 工具（Claude Code、OpenCode 等）调用
- 提供 Web 前端界面，支持自然语言输入、配置预览、手动调整
- 第一期聚焦表单配置，架构预留列表配置、流程配置的扩展点

## Capabilities

### New Capabilities

- `skill-consumer`: 消费外部 Skill 文件（由 njmind-modeler 动态生成），解析表单字段类型定义、JSON Schema、模板和规则，作为配置生成的规则源
- `nl-to-form-config`: 自然语言到表单配置的转换引擎。接收用户自然语言描述，结合 Skill 规则，生成符合 JSON Schema 的 FormConfig / FormFieldConfig JSON
- `mcp-server`: MCP 协议服务端，暴露 tools（get_form_config、create_form、validate_form 等）和 resources（schema、template、guide），供 AI 工具直接调用
- `web-frontend`: Web 前端界面，提供自然语言输入框、配置实时预览、字段拖拽编辑、JSON 导出功能
- `config-api`: REST API 层，提供配置生成、校验、CRUD 等 HTTP 接口，供前端和其他系统集成

### Modified Capabilities

（无已有 capability 需要修改，这是全新项目）

## Impact

- **新项目**：在 `/Users/xiaotaotao/IDEA/njmind/middle/llm-to-modler/` 下从零搭建
- **依赖外部**：需要 njmind-modeler 项目动态生成并同步 Skill 文件到约定目录
- **技术栈**：Node.js（后端 API + MCP Server）+ Python（LLM 调用 + NLP 处理）+ React/Vue（前端）
- **数据流**：自然语言输入 → Python LLM 处理 → 生成配置 JSON → Node.js API 层 → 前端展示 / MCP 输出
- **部署**：需要同时运行 Node.js 和 Python 服务，考虑 Docker 容器化
