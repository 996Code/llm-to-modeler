# 路线图

## 里程碑 1：LLM Form Modeler MVP

| 阶段 | 名称 | 状态 | 描述 |
|------|------|------|------|
| 1 | llm-form-modeler | 待规划 | 自然语言生成表单配置的核心功能，包含 Skill 消费、配置生成引擎、MCP Server、REST API、Web 前端 |

### 阶段详情

#### 阶段 1：llm-form-modeler

**目标**：实现自然语言 → 表单配置的完整流程，提供 MCP 接口和 Web 界面

**核心功能**：
- 消费外部 Skill 文件（JSON Schema、模板、规则）
- 自然语言转表单配置（支持流式输出）
- MCP 协议服务（tools + resources）
- REST API（生成、校验、修正、查询）
- Web 前端（输入、预览、编辑、导出）

**OpenSpec Change**：`openspec/changes/llm-form-modeler/`
