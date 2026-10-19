# Dify 对标蓝图与差距分析

> 源码快照：~/cyble-code/dify（main 7057546，2026-10-16 clone）
> 研究方法：七视角并行 agent 源码深挖（插件/工作流/RAG/Agent工具/产品形态/平台工程/知识库产品专项）+ 与 RAGFlow 既有研究（~/cyble-code/ragflow）三方交叉
> 定位对照：Dify = SaaS 化应用构建平台（多租户/插件市场/画布编排）；我们 = 内网低代码平台 AI 插件系统（宿主集成/进程内 pack/代码定义流程）

## 0. 一句话总结论

**核心链路（检索质量/引用可信/分层架构）我们无需任何改动——三路检索与强引用全面领先 Dify；Dify 的真增量全部在「产品化治理层」与「契约工程」**：权限声明/错误信封/凭证代管（插件契约三件套）、metadata 过滤、HITL 超时与结构化表单、SSE 事件协议丰富度、MCP 薄壳模式。

## 1. 六视角核心结论速览

| 视角 | Dify 路线 | 我们现状 | 判定 |
|---|---|---|---|
| 插件体系 | 独立容器 daemon+每插件子进程+市场签名分发（daemon 在独立仓库） | 进程内 pack+声明制+依赖探针 | 场景差异为主；**契约字段设计值得抄**，进程模型不抄 |
| 工作流引擎 | graphon 自研引擎：数据定义图+事件即真相+worker 并行+pause 快照 | LangGraph：代码定义图+state 传递+interrupt 每步 checkpoint | 各有胜场；**HITL 六项增强**值得抄 |
| RAG | 33 向量后端插件矩阵+父子上双层+摘要索引+metadata 过滤 | 图谱+向量+全文三路+结构感知切块+强引用 | **检索算法我们领先**（Dify 中文全文形同虚设）；产品化治理层落后 |
| Agent+工具 | CoT/FC 双 runner+四类工具（builtin/custom/mcp/plugin）+agent 策略插件化 | LLM 二级路由+CompositeTool 固定管线 | 场景差异；**工具输出类型系统+MCP 薄壳**值得抄 |
| 产品形态 | 五种 App 类型+SSE 两层事件协议+画布 UX+知识库管理 UX | 对话+制品卡+SSE stage 事件 | **SSE 事件类型+断线重放**最值得抄 |
| 平台工程 | （补派中：RBAC/模型矩阵/celery） | — | 见补充节 |

## 2. 与 RAGFlow 三方交叉（检索维度定论）

| 维度 | Dify | RAGFlow | 我们 |
|---|---|---|---|
| 中文全文 | 默认英文 config，jieba 命中计数排序（弱） | ES+虎切 55 万词典（强） | tsvector 2-gram+GIN（中强） |
| 引用 | 弱引用（前端气泡事后挂靠） | 强引用（句级 insert_citations） | 强引用（[片段:N] prompt 约束） |
| 混合融合 | weighted_score 或外部 rerank | 引擎层 0.3/0.7 | RRF+三路编排 |
| 切块 | 分隔符递归+父子双档 UI 可预览 | DeepDoc 版面感知 | 结构感知（章整块+子切） |
| **行业共识**（三方都做） | 父子双层（大块上下文+小块检索）、rerank 外置化、hybrid 检索 |||

## 3. 升级路线（P0/P1/P2 汇总六视角，合并既有演进待办编号续排）

### P0（直接解决现有痛点，低成本高确定性）

| # | 项 | 来源视角 | 要点 | 量级 |
|---|---|---|---|---|
| D1 | clarify 挂起超时双轨 | workflow | pending_questions 加 expires_at（默认 24h），过期走超时话术分支；现在挂起可永久悬挂 | 1 天 |
| D2 | 挂起原因独立落表 | workflow | clarify_pauses 表（可查"所有待澄清对话"，现埋 checkpoint 不可查） | 1 天 |
| D3 | interrupt payload 结构化 schema | workflow | ClarifyQuestion Pydantic 模型（select/text/options），前端按 schema 渲染 | 1-2 天 |
| D4 | 工具输出类型补齐 | agent-tools | ToolResult 加 link+suppress_output（JSON 不进 LLM 上下文省 token） | 半天 |
| D5 | manifest 权限/资源/兼容三段声明 | plugin | config.yaml 加 permissions+min_engine_version；装配期只注入声明允许的句柄 | 2-3 天 |
| D6 | 错误契约信封 | plugin | PackInvokeError{error_type} 统一映射可重试/不可重试/凭证错 | 半天 |

### P1（有明确价值，需排期）

| # | 项 | 来源 | 要点 |
|---|---|---|---|
| D7 | metadata 过滤（manual 档） | RAG | kg_documents 加 JSONB doc_metadata+检索条件下推；**njmind 多部门文档场景天然需要**（3-5 人天） |
| D8 | 切块参数 UI 化+预览 | RAG | 设置页暴露三旋钮+保存前预览（我们算法强于 Dify 但只能改 yaml）（2-3 人天） |
| D9 | CompositeTool 步骤游标+步骤级重试 | workflow | _step_cursor 持久化，resume 从游标继续（替代整工具重跑+守卫键跳 LLM） |
| D10 | SSE 事件带 step 元数据+断线重放端点 | product | emit 结构化(tool/step/duration)+按 conversation_id+seq 重放（事件已落库，只差端点） |
| D11 | MCP server 薄壳 | agent-tools | 每 pack 暴露 1-2 工具（kb_search/ask_data），inputSchema 复用 Tool.input_schema，call_tool 转调引擎 |
| D12 | 凭证代管模式 | plugin | settings_store 按 pack+namespace 代管加解密，第三方 pack 不自存 secret |
| D13 | 检索去重 score 语义+hybrid 延迟阈值 | RAG | 重复保留高分者；混合场景阈值延迟到融合后（#35233 教训） |

### P2（触发制战略储备）

| # | 项 | 触发条件 |
|---|---|---|
| D14 | 摘要索引 | 实测出现"抽象问法零召回"（如"劳动法主要讲什么"） |
| D15 | reorder 奇偶重排（lost-in-middle） | 评测集显示长上下文中段证据被忽略 |
| D16 | 父段 full-doc 档 | 法律长文档跨章节综合问答需求 |
| D17 | Agent 循环+SCA 落地 | 检索测试页显示"向量命中但引用不足"比例持续超阈值 |
| D18 | 独立进程 daemon 化 | 出现不可信第三方 pack 或非 Python 栈需求 |
| D19 | 变量 selector 寻址 | 走向可编排工具管线时 |
| D20 | 建议问题/消息操作条/会话软删 | 产品 polish 阶段 |

### 明确不抄（场景差异，抄了是负担）

- 33 向量后端插件矩阵（已裁决单 pgvector）；jieba keyword 路；Dify 弱引用；LLM 路由选库（我们库数不需要）；QA 模式（与图谱抽取重叠）；React Flow 画布（我们不是用户编排产品）；多租户 passport（宿主管权限）；TTS/语音（无场景）；租户级 provider 缓存治理（多租户 SaaS 专属性能补偿）；CoT 文本解析状态机（我们模型支持 FC+chat_json）。

## 4. 我们已领先项（对 Dify，防误伤）

1. 中文检索三路（图谱真查询+pgvector+2-gram）vs Dify 中文全文形同虚设
2. [片段:N] 强引用（prompt 约束+编号权威源）vs 前端气泡弱引用
3. append-only events 统一审计 vs Dify 分散存储
4. 追问原生 interrupt 多轮澄清（ChatBI 场景②③）vs Dify 澄清表单较新
5. 依赖探针运行时真实探测 vs Dify 静态声明
6. 结构感知切块（章整块+万级上限）vs 分隔符递归
7. 进程内热启停的轻量（管理端秒级切换）
8. 与 RAGFlow 共同领先：图谱质量管控（本体约束/三重接地）

## 5. 平台工程视角补充（RBAC/模型矩阵/celery，窄版重派完成）

- **RBAC**：社区版 5 级角色硬编码谓词+**每查询手动加 tenant 条件（无基类强制，存在双入口风险——教训值得记）**；企业版鉴权外移外部服务。只有 workspace 一层无项目层。
- **模型供应商**：契约已外移 graphon 包，全部 provider 走插件进程 RPC（invoke_llm/embedding/rerank/tts 按类型分方法+credentials 按调用传递）；**每租户 RSA+AES 混合加密，私钥存对象存储与 DB 分离**；租户级默认模型表（首用自动落库）。
- **celery**：~80 任务模块分 12 队列；状态回写三通道（DB 状态机分阶段提交/Redis pubsub 流式/租户公平调度队列）；**任务显式传身份参数不依赖请求上下文**；流式消费保证终端事件兜底。
- **内网过滤后的借鉴**：数据归属查询收敛为唯一入口（P0）、LLM 凭证不落明文+展示脱敏（P0）、后台任务身份参数化（P1）、异步终端事件保证（P1）——已并入下方路线 D21-D24。

## 5b. 知识库产品专项（第七视角，用户点名补充）

RAG 视角覆盖检索算法，本节是**知识库作为产品**的管理/治理/扩展面（api/services/knowledge/ 4262 行+21 表模型+前端）。

**核心发现**：
- **外部知识库适配层**（P0 级）：模板表（endpoint+凭证+探活）+绑定表（本地库↔外部库 id）+固定 `/retrieval` 契约+Dataset 伪装（provider=external 挂进同一产品面）——**njmind 宿主已有 ES/文档资产接入 KG 的直接参考**；prepare/execute 会话分离（HTTP 在 DB session 外）值得照抄
- **Segment 手动编辑+增量重索引**：改块=diff content→重算 hash/tokens→只重嵌该块（非全文档重来）——修正 LLM 抽取错误的人工出口
- **文档暂停/恢复**：is_paused+Redis 标记+recover 任务——我们已有断点续跑，缺"用户主动暂停"
- **元数据治理**：双表（字段+绑定）+内置五字段+改名级联迁移+批量打标 UI——比检索下推更完整的产品面；Dify 自己缺值类型校验（可超越点）
- **架构模式**：读服务=响应物化层（防 N+1 批量预取）；绞杀者分层（application Protocol 端口+adapters 持 session）；Ref 归属链值对象（每变更事务内重验 owner 链防越权）
- **我方领先项确认**：层级树/移动（Dify 是平铺+标签）、Schema 诱导+图视图（Dify 无）

**并入路线（D25-D30）**：
- P0 追加：D25 外部知识库适配层（三件套+kb_search 按 provider 分流，3-5 人天）
- P1 追加：D26 文档暂停恢复（1 天）；D27 Segment 编辑+增量重索引（2-3 天）；D28 元数据字段治理（与 D7 检索下推配套，3 人天）
- P2 追加：D29 查询审计+hit_count 块命中统计（"哪些知识真被用"）；D30 批量文档操作/检索测试查询历史/使用检查（app_dataset_joins 式引用图谱）
- 顺手超越点：元数据值类型校验（Dify 缺）；归属链复验（Ref 模式用于 chunk/extraction 端点）

## 6. 最终路线（D1-D30 合并全部视角）

P0 追加：| D21 | 数据归属查询收敛 | platform | 唯一 get_x_for_owner 入口，清旁路查询 | 1 天 |
| D22 | LLM 凭证加密+脱敏 | platform | 对称加密存储+obfuscated_token 展示 | 1 天 |

P1 追加：| D23 | 后台任务身份参数化 | platform | user_id 打包进任务参数，X-User-Id 链不在异步边界断 | 半天 |
| D24 | 异步终端事件保证 | platform | SSE/长轮询异常时补发失败终端事件 | 半天 |

## 8. 三重审查修正记录（事实核验/覆盖审查/文档交叉，2026-10-16）

**事实核验（20+ 锚点抽查）**——核心机制全部属实可放心对标；修正项：
- 【较重】`error_handler.py` 不存在——节点错误三策略实际分散于 graphon 包（ErrorStrategy/RetryConfig）+ agent_config_entities.py + agent_v2/output_failure_orchestrator.py。**D 系路线对标锚点已更正**
- 【中】ensure_form_active 在 services/human_input_service.py:230（4 重校验），非 callback.py（callback.py 是超时双轨判断）——D1 参照物更正
- 【中】App 类型实际 8 种（新增 agent/channel/rag-pipeline）非 5 种；backwards invocation 16 端点非 14；向量后端 30/32 非 33；celery 68 文件/16 队列；dataset 表 26（含 pipeline 系 5 新表）
- 断线重放按 run/task id（支持 include_state_snapshot），非"按事件 id"

**文档交叉（main vs 稳定版 1.17.1）**——关键契约修正：
- 【D25 必读】**外部知识库 API 契约以 1.17.x 为准**：无 retrieval_method 字段；响应是 `{"records":[...]}` 平铺（旧 keyed-dict 已废）；records 元素 metadata 必须是 object{}（null 报错）；空结果 `{"records":[]}`
- metadata Automatic 档是 GA（非 preview）；但外部库场景 metadata_condition 只透传无 UI——适配层需自带过滤实现
- MCP OAuth 表述应为 "OAuth + Dynamic Client Registration"；Dify Agent 是 beta（沙箱限制）
- main 领先稳定版 5 周——源码结论按"1.17.1 已验证/main-only 待 GA"两栏理解

**覆盖审查补研究（2 项）**：
- 运行可观测闭环（dashboard/反馈/标注回复/ops tracing）——进行中
- 触发器体系——**已完成**：判定 **P1 窄切口**（"定时摘要/报告"场景：scheduled_jobs 表+cron 到期轮询+跑完记录+末端推送，复用 chatbi scheduler 模式）；通用 webhook 入口不照抄（Dify 零鉴权无幂等，我们要做必须 HMAC+幂等键）；plugin/SaaS 触发 Provider 不做（生态位不匹配）。→ 并入路线 **D31（P1）：定时摘要/主动服务最小实现**
- dify-agent 沙箱（Landlock+shellctl）补入"明确不抄"：自治执行与我们受控生成场景冲突

**可观测闭环（第八补研，收窄重派完成）**：
- Dify 统计=纯 SQL 实时 GROUP BY（无预聚合表，messages 表直接扛）→ 我们若做驾驶舱同样不建累计表，补 2-3 个聚合端点即可（P2）
- 反馈=单表三索引+满意度曲线+全量导出 API——与我们 P2#14 结构等价，无增量
- **标注回复（改答案 vs 我们调权重）=互补不互斥**：→ 并入路线 **D32（P1）：KG QA 标注回复**——高频错答（实体消歧/关系错配）是知识缺口，调权重治标标注治本；实现三块（标注 CRUD+问题 embedding+前置短路查询，向量设施现成）；dislike 数据作为标注候选队列；阈值默认高（≈精确匹配）起步。**ChatBI 不抄**（查询结果时效性强，固定答案有陈旧误导风险）

## 9. 后续动作

- 六视角完整报告存档：本文件为汇总层，各视角细节见 agent 报告原文（对话记录）
- P0 九项（D1-D6+D21-D22+D25）建议下一迭代立项；与既有演进待办（evolution-backlog 8 项）合并评审排期
- 源码保留 ~/cyble-code/dify 供后续精查（重对标触发：Dify 大版本或对应功能立项时）
