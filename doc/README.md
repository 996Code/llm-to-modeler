# 文档目录（书的目录指南）

> 全项目 Markdown 文档的总索引，按阅读路径组织。链路图/架构图（46 张 HTML）另有
> [doc/index.html](index.html) 总入口。内部文档专区 `doc/private/` 不入库不外发。

## 第一部分 · 上手（按此顺序读）

| 文档 | 内容 |
|---|---|
| [README.md](../README.md) | **权威总览**：技术栈、三层六边形架构、核心机制、启动部署 |
| [AGENTS.md](../AGENTS.md) | AI agent / 开发者操作手册入口（规约/测试/发布/部署索引） |
| [插件开发与嵌入指南.md](插件开发与嵌入指南.md) | 从零写一个插件（请假为例）：manifest 逐项、交互模式、嵌入契约、FAQ |
| [系统架构与学习指南.md](系统架构与学习指南.md) | 系统全景学习路径 |

## 第二部分 · 架构深读

| 文档 | 内容 |
|---|---|
| [backend/src/domains/ARCHITECTURE.md](../backend/src/domains/ARCHITECTURE.md) | pack 全部钩子（工具/HTTP API/后台任务/依赖门控/管理页）+ SDK 存储设施 |
| [technical-reference.md](technical-reference.md) | **README 的深度展开**：架构大图 / LangGraph 流程与 GraphState / Tool 协议 / 插件全能力表 / 环境变量全集 / 目录树 / 设计亮点 |
| [backend/src/domains/README.md](../backend/src/domains/README.md) | 域目录导览 |
| [backend/src/domains/chatbi/README.md](../backend/src/domains/chatbi/README.md) | ChatBI 插件结构 |
| [嵌入模式总体设计.md](嵌入模式总体设计.md) | 嵌入模式（IM 聊天窗 + SDK）总体设计 |
| [共享内核API契约.md](共享内核API契约.md) | 编排插件消费的两个底层薄入口：KG 证据服务 retrieve_evidence + 问数受控执行 execute_controlled（信封分态/错误类型保留/插件间依赖声明） |

### 链路走读（代码级数据流，doc/flows/）

一句话/一份文档进来后数据在代码里的完整路径——向量化的调用点、匹配策略、LLM 提示词的拼装：

| 文档 | 链路 |
|---|---|
| [flows/engine.md](flows/engine.md) | 对话引擎：SSE 入口→归属校验→意图路由→LangGraph 工具调度→追问中断/恢复（question_id 令牌）→SSE 事件流 |
| [flows/knowledge-graph.md](flows/knowledge-graph.md) | 导入向：文档→解析分段→bge-m3 向量化(pgvector)→LLM 图谱抽取(Neo4j,断点续跑)；检索向：意图解析→图谱种子三层匹配→文档实体收敛→向量/全文路→D15/D17→answer.j2 提示词→引用校验 |
| [flows/chatbi.md](flows/chatbi.md) | 智能问数：两阶段选表→SQL/MCP 工具调用生成(提示词 sections)→三层校验/受控执行(自愈门)→图表→记忆/反馈回流 |
| [flows/customer-service.md](flows/customer-service.md) | 智能客服：路由表合成(server::tool 身份/声明门禁)→两段式提取→追问令牌→prepare_plan→受控执行→结论(引用校验) |

## 第三部分 · 开发与运维手册（doc/agents/）

| 文档 | 内容 |
|---|---|
| [agents/requirements.md](agents/requirements.md) | **交付硬标准**：测试/验证/提交/汇报/文档要求，不满足不算完成 |
| [agents/collaboration-rules.md](agents/collaboration-rules.md) | 协作规约 16 条（用户裁决沉淀） |
| [agents/testing.md](agents/testing.md) | 测试环境与纪律（.venv/Docker/命令） |
| [agents/branch-and-release.md](agents/branch-and-release.md) | 分支模型、双远程、worktree 出包纪律 |
| [agents/deploy-99-22.md](agents/deploy-99-22.md) | 99.22 部署手册：形态/守卫/内存红线/排查 |
| [agents/work-in-progress.md](agents/work-in-progress.md) | 在途工作快照（带日期，接手先核对） |
| [agents/pitfalls.md](agents/pitfalls.md) | 惯发坑清单（踩过的别再踩） |

## 第四部分 · 专项设计

| 文档 | 内容 |
|---|---|
| [mcp-integration-design.md](mcp-integration-design.md) | MCP 双向接入设计（一+二期总纲） |
| [mcp-phase1-plan.md](mcp-phase1-plan.md) / [mcp-phase2-plan.md](mcp-phase2-plan.md) | MCP 分期实施计划 |
| [知识库用户角色同步API.md](知识库用户角色同步API.md) | 知识库用户/角色同步对接指南（面向源系统：全量/增量/CRUD/状态 + 运行时身份传递，只写对方要做什么） |
| [chatbi-kg-linkage-idea.md](chatbi-kg-linkage-idea.md) | ChatBI 与 KG 联动构想 |
| [deploy/single/README.md](../deploy/single/README.md) | 单容器部署说明 |

## 第五部分 · 对标学习资料

| 文档 | 内容 |
|---|---|
| [RAGFlow项目学习/](RAGFlow项目学习/) | 16 篇笔记（架构/检索/GraphRAG/DeepDoc/Agent…）+ 对标蓝图 + 升级计划 |
| [Dify项目学习/](Dify项目学习/) | Dify 对标蓝图 |
| [简历技术点与面试准备.md](简历技术点与面试准备.md) | 个人向：技术点提炼与面试准备 |

## 第六部分 · 业务与工具

智能客服链路图（代码级走读、插件总览）归入 [总索引 · 智能客服](index.html#metrology-assistant)。投标架构图为**独立单文件** [customer-service-bid.html](customer-service-bid.html)，不进入总索引；图源 `diagram-src/customer-service-bid.workflow.json`，与成品同名。

| 文档 | 内容 |
|---|---|
| [njmind_form使用手册.md](njmind_form使用手册.md) | njmind 表单使用手册 |
| [tools/bid-demo/](../tools/bid-demo/) | 竞标 demo（强检目录/检定规程演示） |
| [tools/mcp-e2e/](../tools/mcp-e2e/) | MCP 双向 e2e 测试项目 |
| [test-evidence/](test-evidence/) | 界面截图库（按日期归档，线上实测） |

## 附录 · 历史档案（不推荐阅读，仅溯源）

- [TECH-ROADMAP.md](../TECH-ROADMAP.md) — 演进路径历史记录，文中旧机制已废弃（文首有对照表）
- `.planning/`、`.superpowers/sdd/`、`.zcode/plans/` — 过程工件（各期计划/任务简报/进度）
