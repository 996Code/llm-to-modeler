# AGENTS.md — AI agent / 开发者操作手册（入口）

> 全项目文档总目录见 [doc/README.md](doc/README.md)（书的目录指南）。
> 架构与业务文档见 [README.md](README.md)（权威）。

## 必读（按顺序）

1. [doc/agents/requirements.md](doc/agents/requirements.md) — **交付硬标准**：什么才算完成
2. [doc/agents/collaboration-rules.md](doc/agents/collaboration-rules.md) — 协作规约 16 条
3. [doc/agents/pitfalls.md](doc/agents/pitfalls.md) — 惯发坑（踩过的别再踩）
4. [doc/agents/work-in-progress.md](doc/agents/work-in-progress.md) — 在途工作（接手先核对）

## 速查

- 测试：`cd backend && .venv/bin/python -m pytest tests/ -q`（必须 .venv；需本机 Docker）
- 前端：`cd frontend && npx vue-tsc --noEmit`；本地起服 `bash start_dev.sh --backend-only` + `npm run dev`(13080)
- 推送：**origin（GitHub）**；分支策略（2026-10-27 起）：GitHub 与本地**只保留 main** 直接提交，不再开 feat 分支
- 部署：**99.22 已停更**（2026-10-31 起停在 10252d3，不再部署）；**测试一律本地栈**（`bash start_dev.sh --backend-only` + `npm run dev`，端口见 doc/agents；历史部署手册 [deploy-99-22.md](doc/agents/deploy-99-22.md) 仅存档）
- 红线：28080 不动；共享宿主机不加内存；不碰用户在途未提交文件；凭证不入库
