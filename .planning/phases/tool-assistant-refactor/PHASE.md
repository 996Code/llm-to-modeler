# Phase: tool-assistant-refactor

## 概述

把 njmind 表单专属的管线框架重构为「工具助手 + 可插拔工具包」六边形架构。

## 关联

- **OpenSpec Change**: `openspec/changes/tool-assistant-refactor/`
- **权威设计**: `docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md`（v4）
- **状态**: 规格已定义，待规划

## 迁移阶段（绞杀者模式）

| 阶段 | 内容 | 状态 |
|------|------|------|
| 0 | 骨架搭建（目录 + ABC + 空实现委托） | 待开始 |
| 1 | 抽 AssetClient + Unicode 清洗 | 待开始 |
| 2 | 抽 Prompt + C.2-C 缓存 + C.2-E override/append | 待开始 |
| 3 | 管线搬进工具 + C.2-A 追问 + C.2-B 并发 | 待开始 |
| 4 | 存储重建 + 日志安全 + C.2-D 压缩 sidechain + 清理 + 全回归 | 待开始 |

## 验收标准

- `grep -rE "form|formCode|template|field" engine/` 无结果（架构试金石）
- DummyTool + DummyPack 端到端跑通
- 三意图无功能回归
- C.2 五项全部可用
- 接口后端重构 + 前端同步改达成
