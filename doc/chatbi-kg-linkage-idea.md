# ChatBI × 知识库 联动构想（未立项）

> 状态：构想存档（2026-10-21 基于代码链路分析得出，未排期）。
> 结论一句话：**两包不合并代码；值得做的是"联动"——智能问数生成 SQL 时把知识库检索结果当背景资料。**

## 为什么不合并（形态 a 否决）

| 障碍 | 证据 |
|---|---|
| 一级路由语义冲突：两包 domain.description 分别服务"查数据画图"与"知识库事实问答"，合并后单描述覆盖两类意图，路由歧义面扩大 | chatbi config.yaml:6-10 vs knowledge_graph config.yaml:4-7；engine/nodes.py:931-962 |
| 依赖连累：KG 的 neo4j 是 `required: true` 硬依赖（fail-closed 闸门），合并后 Neo4j 挂掉连累 BI 查数 | knowledge_graph config.yaml:32-39；services/pack_dependency.py |
| 前端融合成本：两套管理页共 18 个 Vue 组件、两套 settings.schema.yaml、6 种任务类型、两套 artifact 渲染卡（BiChartCard vs KgGraphCard）、两个 state_scope 记忆域 | frontend/src/admin/packPages/ |

## 两包"长得像"其实不是一回事（重叠面盘点）

| 维度 | chatbi | knowledge_graph | 判定 |
|---|---|---|---|
| 图谱 | NetworkX 内存图（表=节点/FK=边，算 JOIN 路径，不持久化）graph_core.py:101-120 | Neo4j 持久图（LLM 抽取的文档实体三元组） | 完全不同的两种图，名字撞车 |
| 向量检索 | stores.py:38 前缀 chatbi，检索目标是**表**（schema linking） | stores.py:21 前缀 kg，检索目标是**文档片段** | 底座同构（同一 sdk/pg_vector_store.py 双前缀），上层形似实异 |
| 切块 | 无文档切块（embed 语义模型序列化文本 indexing.py:44-80） | doc_parser.chunk_text + 子切块 | 无重叠 |
| 记忆 | 重型跨会话记忆表 memory.py:90 | 会话级库绑定 state_scope | 形似实异 |

该共享的底层（向量库/关系存储/文档解析/LLM client/任务框架）分层治理已下沉 SDK 完毕。剩余同构小件（2-gram 分词、JSON 容错解析、rerank 调用、embedder 缓存，合计 <300 行）低优先级顺手做。

## 联动方案（形态 c，两步）

现状：两包零相互 import（grep 证实），ChatBI 写 SQL 时完全不知道知识库里有什么。

### 第一步：ask_data 注入"知识库背景"（推荐先做）

- 落点：`domains/chatbi/tools/ask_data.py` generate_sql 的注入段（:497-559）——该段本就是插件式列表（Skills/fewshot/JOIN 路径/指标/记忆/历史都往里塞），加一段"知识库背景"。
- 做法：调 KG 检索取相关 chunks 喂给 NL2SQL。例：用户问"上季度华东区毛利率"，语义层没配过"毛利率"指标时，知识库里的财务制度文档能兜住。
- 三个前提约束：
  1. **软依赖**：KG 未启用/检索失败 → 静默跳过该段（现架构零 inter-pack import，需 import 失败降级模式）；
  2. **延迟预算**：只取向量/全文路的 chunks，不走 KG 完整回答链（省多阶段 LLM 调用）；
  3. **ACL**：尊重 KG 库级权限（domains/knowledge_graph/acl.py）。

### 第二步（视第一步效果）：语义层文档反向导入 KG

- 语义层文档/指标说明经 KG 现有 `kg.import_document` 任务回流入库（复用断点续跑），让 kb_search 也能答指标定义类问题。
- 也可走管理端上传或 MCP 通道（暴露 ingest 工具）。

## 定位原则

两包保持独立 pack 身份，联动走"读对方检索结果"，不合并代码。
