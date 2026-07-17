# 阶段：llm-form-modeler

## 状态

- **当前状态**：待规划
- **开始日期**：未开始
- **完成日期**：未开始

## 目标

实现自然语言 → 表单配置的完整流程，包含：
1. Skill 文件消费模块
2. Python 配置生成引擎
3. Node.js API 层 + MCP Server
4. React Web 前端
5. Docker 容器化部署

## 关联

- **OpenSpec Change**：openspec/changes/llm-form-modeler/
- **任务清单**：openspec/changes/llm-form-modeler/tasks.md

## 验收标准

- [ ] 能够通过自然语言生成符合 JSON Schema 的表单配置
- [ ] MCP Server 能够被 AI 工具正常调用
- [ ] Web 前端能够完成从输入到导出的完整流程
- [ ] 所有 API 接口正常工作
- [ ] Docker Compose 一键启动所有服务
