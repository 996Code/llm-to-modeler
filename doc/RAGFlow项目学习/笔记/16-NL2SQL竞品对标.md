# 16 · NL2SQL 竞品对标（Vanna / DB-GPT / WrenAI）

> G 轮确认的最大研究盲区（问数侧零竞品研究）补齐。Vanna 源码级验证（PyPI wheel v2.0.2），DB-GPT/WrenAI 文档交叉验证（GitHub 直连超时已注明）。

## 1. Vanna（~21k star，源码级）

- **三类语料 RAG 化**：三个独立 collection——`sql_collection`（question+sql 对）/`ddl_collection`/`documentation_collection`；**内容哈希生成确定性 ID**（-sql/-ddl/-doc 后缀）天然去重（chromadb_vector.py L67-100）
- **检索拼装**：generate_sql 时三路并发取回，DDL/doc 按 **token 预算（默认 14000，len/4 估算）贪心填入** system prompt；question-SQL 对以 few-shot 对话消息注入；三路 top-k 各自可配（默认各 10）
- **无 schema 图扩展、无 JOIN 推理**——全靠 DDL 全文（我们的 SchemaGraph+JOIN 路径是结构侧领先）
- **train() 形态**：四选一路由（question/sql/ddl/documentation/plan）；无参调用可从 INFORMATION_SCHEMA 自动生成 TrainingPlan
- **反馈闭环祛魅**：OSS 版 `ask()` 执行后**无自动回流**——闭环=Flask UI「Train」按钮**人工一键回流**+`remove_training_data(id)` 可删可治理。所谓"自学习"实为人审回流（对我们 P2#14 的设计印证：人审闸门是正确路线，我们的"成功即回流无审核"反而是错的）

## 2. DB-GPT（文档级）

- **Spider 82.5% 真相**：是 **13B 开源模型 SFT 微调后在 Spider dev 的执行准确率（EX，结果集比对）**——属 DB-GPT-Hub 微调框架成绩，**非产品化管线成绩**（引用勿混淆）
- 可借鉴口径：**EX（而非字符串匹配）+ 难度分层（easy/medium/hard/extra）+ 语义解析子指标**——Spider 英文学术集，我们建内部中文集沿用框架
- 语义层非其强项（RAG 知识库+prompt 管理，无显式指标建模）

## 3. WrenAI（文档级）——口径层行业标杆

- **Open Context Layer 三件套**：①MDL 语义模型（模型/列/关系/**计算列/指标**，口径编码为表达式，join 路径人工声明后冻结）；②**instructions.md**（枚举映射/单位换算/业务黑话，每次查询前必读）；③LanceDB 记忆（只存"过去有效"查询）
- **口径痛点解法**：指标定义一次全局复用（口径漂移从根上消除）；LLM 写"面向模型"的 SQL，Rust+DataFusion 编译为物理 SQL，**先 dry-plan 校验再执行，歧义返回结构化原因而非硬猜**；RLAC 行列级权限随编译注入
- 同义/方言=集中受治理的版本化术语表（Git 友好）

## 4. 六维对照（竞品 vs 我们 ChatBI）

| 维度 | 判断 | 依据 |
|---|---|---|
| 语料检索 | **结构侧领先 / 语料侧落后** | SchemaGraph+JOIN 路径三家均无；但我们仅 schema 单路，Vanna 有 doc 路补口径 |
| 反馈学习 | **落后** | 断头+成功即无审回流双重问题；Vanna 人审回流可删可治 |
| 口径层 | **明显落后** | 无显式口径层；WrenAI 是标杆 |
| 评测 | **落后→已在补** | P0#1 评测集已立（护栏口径）；DB-GPT EX+分层框架可套用于 P1#5 扩容 |
| 多轮 | **领先** | 结构化继承（prev_sql/tables/filters）胜 Vanna followup 列表 |
| 安全 | 执行安全持平偏领先（三态熔断独有）；行列级权限落后于 WrenAI |

## 5. 可落地 TOP3（ROI 序，已入蓝图）

1. **反馈闭环补 API+回流审核闸门**（2-3 人天）——现有"成功即回流无审核"会持续污染 fewshot 库，是定时炸弹；参照 Vanna 确定性 ID 去重+remove 可治理+人审闸门（与 P2#14 合并设计）
2. **轻量口径层**（1-2 人周）——先做"指标口径表+同名词典"注入 prompt（不必上编译引擎），直击中文问数"口径而非语法"痛点；参照 WrenAI MDL 计算列+instructions 必读
3. **评测口径基准化**（3-5 人天）——EX 执行准确率+难度分层，建 100-200 题内部集（P1#5 扩容时直接套用此口径）

## 对我们路线的校准

- G 轮"学习对象 60%"判断被证实：知识库侧学 RAGFlow 对了，问数侧该学 Vanna/WrenAI 也对了
- P2#14 反馈闭环的设计依据从 RAGFlow chunk_feedback 升级为**双参照**（RAGFlow 加权调权 + Vanna 人审回流闸门）——后者解决我们 fewshot 污染问题
- 新增路线候选：轻量口径层（P1 候选，与 P1#6 检索混合化正交——混合化管"找到表"，口径层管"算对数"）
