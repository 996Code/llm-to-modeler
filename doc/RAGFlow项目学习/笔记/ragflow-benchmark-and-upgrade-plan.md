# RAGFlow 对标与三层升级蓝图

> 2026-10-13 · 基于 RAGFlow v0.27.2 全量源码（Python 稳定版 + Go 主干）与本项目全链路审计
> 证据标准：所有结论有 file:line；RAGFlow 关键结论已经独立复核（10/10 成立，1 处权重方向已修正）

## 0. 产出物索引

| 产出 | 位置 |
|---|---|
| 组件架构图 | ../图/ragflow-system.html |
| 文档导入工作流 | ../图/ragflow-doc-ingestion.html |
| 对话检索时序 | ../图/ragflow-chat-retrieval.html |
| GraphRAG 数据流 | ../图/ragflow-graphrag.html |
| Agent 画布执行 | ../图/ragflow-agent-canvas.html |
| 连接器同步 | ../图/ragflow-connector-sync.html |
| 会话记忆数据流 | ../图/ragflow-memory.html |
| 角度笔记 15 篇 | 笔记/ 目录（01-15 + 本蓝图，index.html 总览导航） |
| RAGFlow 源码 | ~/cyble-code/ragflow-v0272（Python）/ ~/cyble-code/ragflow（Go main） |

## 0.5 本仓库代码地图与术语表（F 轮 onboarding 审查补）

**两套源码根（材料中行号分属两处，勿混淆）**：
- `~/cyble-code/ragflow-v0272` = RAGFlow 源码（本材料绝大多数 file:line 指此）
- 本仓库 = `/Users/xiaotaotao/IDEA/njmind/middle/llm-to-modler`（蓝图 P0 项里的 retrieval.py/api.py/ask_data.py 等行号指此）

**P0 涉及文件（本仓库）**：
| 文件 | 作用 |
|---|---|
| backend/src/domains/knowledge_graph/api.py | KG REST（/search 在 :484-533） |
| backend/src/domains/knowledge_graph/retrieval.py | 混合检索编排（answer_question :386-393） |
| frontend/src/admin/packPages/knowledge_graph/DocManager.vue | KG 管理页（加"检索测试"标签处，现无 tab 结构） |
| frontend/src/components/chat/KgGraphCard.vue | 对话数据卡（片段手风琴，citedChunks prop） |
| frontend/src/components/chat/ChatPanel.vue | 对话面板（:211 已接 KgGraphCard） |
| backend/src/llm/client.py | LLM 客户端（rerank 接口加在此） |

**启动**：`./start_dev.sh`（后端 :18080 + mock :19999）+ frontend 下 `npm run dev`。

**术语速查（本仓库概念，RAGFlow 无）**：pack=插件（声明制路由，引擎零领域知识）；call_logs=逐环节调用观测（stage 标记）；宿主=njmind 低代码平台；AssetClient=宿主数据读写契约；artifact+APPLY=AI 产物确认门（AI 不落库）；fail-closed=宁可明确失败不猜测。

## 1. 对标一句话结论

- **平台底座**：我们是"单进程精工"（事务性装配、call_logs 观测做工细），RAGFlow 是"分布式工厂"（任务队列/水平扩展/多引擎）。差距集中在：分布式任务、多 provider 网关、rerank、开放协议。
- **knowledge_graph**：我们图质量管控深（本体约束/原文锚定/三重接地），RAGFlow 工程闭环全（增量 checkpoint/删除重建/消歧预筛/社区报告）。差距集中在：检索质量（rerank/全文路）、全局能力（社区报告）、产品化（检索测试/引用回跳）。
- **ChatBI**：主管线成熟（八步：七步可见+finalize 收尾，ask_data.py:106-116；自愈/三层校验/query_stats），但检索是单路向量+手调阈值、无评测集、无引用、反馈闭环有断头（点赞/纠正无入口）。

## 2. 差距总表（F 轮魔鬼代言人审查修正：拆为"真差距"与"场景差异"两张——后者是设计选择非差距，不构成补齐方向）

**真差距（对标确认值得补）**：

| 维度 | 我们现状 | RAGFlow v0.27.2 | 差距评级 |
|---|---|---|---|
| LLM 网关 rerank | 无 rerank 接口 | rerank 接口+外置模型 | **高**（管线缺口） |
| 检索融合 | ChatBI 单路向量 0.35 手调阈值 | 向量0.3/全文0.7+空结果复查+rerank | **高** |
| 引用可信 | KG 来源列表；ChatBI sql 制品 | [ID:n] 句级插入+坐标回跳 | **高**（体验差距） |
| 评测 | 无评测集无准确率回归 | benchmark+公开基准+langfuse | **高** |
| 知识形态 | 图谱单一产物 | 编译家族（图谱/树/wiki/时间线） | 中（方向参考） |

**场景差异·不补齐（插件 vs 平台的设计选择）**：

| 维度 | 说明 |
|---|---|
| 任务体系 | 单进程契约是明示决策（deploy/single/README.md:4），等真实多副本需求 |
| 多租户 | 宿主管权限（X-User-Id 透传），插件不做租户体系 |
| 开放协议 | MCP/OpenAI 出口按需（终局项），非当前差距 |
| 数据源接入 | 走宿主 AssetClient 契约（终局项） |
| 记忆复杂度 | 三类记忆+遗忘策略，我们 compact_focus 规模暂不需要 |

**原总表（保留供追溯）**：

| 维度 | 我们现状 | RAGFlow v0.27.2 | 差距评级 |
|---|---|---|---|
| 任务体系 | 线程池+queue_key+续跑，单进程（task_manager.py:9-18） | Redis Streams 消费组+unacked 恢复+心跳+worker 回收 | 中（单实例契约是明示决策） |
| LLM 网关 | 单 OpenAI 兼容+限速+降级；无 rerank/缓存/费用 | 40+ provider、encode/encode_queries、rerank 接口、24h 调用缓存 | **高**（rerank 是管线缺口） |
| 检索融合 | KG 双路（图谱+向量）；ChatBI 单路向量 0.35 阈值 | 向量0.3/全文0.7 融合+空结果放宽复查+rerank 精排 | **高** |
| 引用可信 | KG 来源列表；ChatBI sql/tables 制品 | [ID:n] 句级自动插入+positions 坐标回跳 | **高**（产品体验级差距） |
| 评测 | ChatBI 无评测集；无准确率回归 | benchmark（首token/P50/QPS）+ langfuse 集成 | **高** |
| 记忆 | ChatBI 词重叠召回+LLM 整理；会话 compact_focus | 三类记忆（semantic/episodic/procedural）+FIFO 遗忘 | 低（我们规模暂不需要） |
| 数据源接入 | 无（手动上传） | 35+ 连接器+fingerprint 增量+删除对账 | 中（走宿主 AssetClient 约束） |
| 多租户 | X-User-Id 透传+会话归属 | tenant/role 四档+三层防护 | 低（宿主管权限） |
| 开放协议 | 无 MCP/OpenAI 兼容出口 | MCP+OpenAI+Dify 三协议 | 低（按需） |
| 知识形态 | 图谱单一产物 | 知识编译家族（图谱/树/wiki/时间线/页索引） | 中（方向参考） |

## 3. 近期落地路线（3-6 个月，按性价比排序）

### P0（纯产品化，本周级；**评测集先行**——F 轮答辩审查修正：它是 #4 rerank 与 #6 检索混合化的验收前提，没有尺子一切检索改动都是盲改）
1. **ChatBI 最小评测集（先行项，P1#5 的前置子集）**：先建 20 条最小用例（evalset.json 形态见 P1#5）——不追求 50-100 条全量，先让 rerank/混合化有基线可比。约 1-2 人天。
   验收：20 条 query→期望表断言可跑；后续检索改动有 recall 基线数字。
   **✅ 已完成（2026-10-13，merge ae2eadc）**：基线 **recall@5=1.000**（n=20，正交基假 embedder **管线护栏口径**——embed→索引→召回→阈值任一环断裂即掉 0，非检索质量度量；真实质量度量需真 embed+噪声负例，P1#5 扩容时建立）。SQL 骨架命中 10/10（含 3 条 medium 多表 JOIN 护栏）。全量 1322 passed。**P0#4 rerank 验收注意：护栏口径下 rerank 后应仍=1.000（证链路未断）；质量改善证明需第二口径**。
2. **KG 检索测试页**：DocManager 加"检索测试"标签。**工作量修正**（二轮审查）：并非纯前端——/search 返回（retrieval.py:386-393）不含 seeds 种子命中与截断水位（nodesTruncated/maxNodes，只进 call_logs，retrieval.py:192-198），需后端小改：answer_question 返回体补 seeds/水位字段（或加 retrieve-only 参数跳过 LLM 回答，省时省钱）。鉴权口径：测试页挂 DocManager（admin_required 门内）；选库走 /kbs 列表（/search 是用户级端点且多库时不回显库名防枚举，api.py:507-509）。
   验收：任一 query 可视化两路召回明细（意图/种子命中表/子图+截断水位/向量分数）；能回答"为什么没召回 X"；retrieve-only 模式不触发 LLM 回答调用。
   **✅ 已完成（2026-10-14）**：hybrid_retrieve 返回体纯增量加 diagnostics（termDetail 逐词命中/截断水位/向量降级——此前只进 call_logs）；新管理端 `POST /kbs/{kb_id}/retrieve-test`（admin 门内，直接调 hybrid_retrieve 跳过 kg.answer——意图解析仍走）；前端 KG 管理页第四 tab「检索测试」四段可视化（意图/种子命中表含红「未命中」/子图计数+水位 tag/向量片段带分数）。浏览器实测两用例（命中:张伟 1/1 命中+未截断+5 片段；未命中:赵六 0/1 红 tag+子图 0/0——"为什么没召回 X"直接可答）。用户级 /search 零改动（防枚举口径不变）。测试 11 用例（含 retrieve-only chat_calls==[] 断言/top_k 端到端锚定/admin 门 401）。**蓝图原文说"DocManager 加标签"实际落 index.vue 第四 tab**（DocManager 只是三 tab 之一，同构等价，终审裁决接受）。**P0 剩余：#4 rerank 接口**。
3. **KG 答案句尾引用编号**：answer.j2 已有 [片段:N]，约束 LLM 句尾标号；KgGraphCard 片段展开联动（组件已存在且 citedChunks prop 已接线，ChatPanel.vue:211）。**编号权威源**：sources.chunks 顺序为唯一权威，answer.j2 的 [片段:N] 必须与该顺序对齐（现状靠 loop.index 隐式对齐，改为 prompt 显式注入编号列表）；**与 #4 的交互**：rerank 上线后 chunks 顺序以 rerank 后为准，编号在 rerank 之后生成，保证稳定。**点击滚动**：回答文本里 [片段:N] 现为纯文本，需改前端 Markdown 渲染（把 [片段:N] 转为可点击锚点，滚动到 KgGraphCard 对应片段手风琴并展开）。**LLM 不守约束**：不重试（烧钱），放行并在 sources.chunks 顺序对齐上兜底——编号错乱只影响体验不影响事实。
   验收：答案每个结论句带 [片段:N]，点击滚动到对应片段并展开。
   **✅ 已完成（2026-10-14，merge e382858）**：answer.j2 显式注入「可用引用编号」总表行；后端 parse_cited_chunks 解析回传（去重升序、越界不滤——前端双界 n>=1 && n<=len 兜底，T2 裁决）；kb_search artifact 透传；前端徽标 data-chunk-idx+事件委托点击滚动+手风琴展开；KgGraphCard「引用了 N/M 个」双界高亮。浏览器实测 DOM 取证通过（徽标渲染/滚动 inViewport/展开原文/1-of-5 高亮）。全量 1330 passed。**附带修复**：vector_store 单槽缓存改按指纹多槽（chatbi/kg 双 pack 前缀互踢连接根因，实测中发现）。**遗留跟进（终审 minor）**：多槽缓存旧指纹连接不淘汰（热改低频可接受）；点击先滚动后展开时序（居中定位下长面板可能溢出下缘）。**P0 剩余：#2 检索测试页、#4 rerank 接口**。
4. **平台 rerank 接口**：llm client 增 rerank(query, texts)——**只接外部 API，不绑本地模型**（RAGFlow v0.18 移除内置 rerank："对检索率影响极小但显著增加检索时间"）。**协议参照**：/rerank 并非 OpenAI 官方端点，参照 Cohere/Jina 的 rerank API 形态（POST {query, documents, top_n} → results[{index, relevance_score}]），配置 base_url+model+api_key。**配置落点**：走 env（LLMConfig 同款，与 llm client 现有配置体系一致）；**排序对象**：KG 侧排 chunks+node_details（linearize 前的资料段），ChatBI 侧排候选表——不排 BFS 子图本身（图结构保持，只排进 prompt 的资料顺序）。**开关粒度**：KG/ChatBI 各自 settings.schema.yaml 独立开关（默认关），平台层无全局开关。**日志**：走 _log_retrieval_call（call_type='rerank'，与 graph/vector 同表）。
   **失败降级**：rerank 是排序质量增强项，不是数据正确性项——调用失败/超时（默认 3s）时**降级为原召回序**继续流程，不阻断检索、不重试烧钱；call_logs 记 rerank stage 且 error 字段留痕（与 KG 向量路降级同款语义，retrieval.py:268-282 先例）。与 fail-closed 的边界：fail-closed 管"数据对不对"（SQL/上游写入），rerank 失败只影响"排序好不好"，降级放行不违反原则。
   验收：call_logs 出现 rerank stage；KG 进 prompt 的资料段按相关度排序；**人为断掉 rerank 端点，检索链路仍完整返回且日志有 degraded 记录**；排序质量收益用 P0#1 最小评测集基线对比（20 条 recall@5）。
   **✅ 已完成（2026-10-14，merge 403baa1）——P0 四件套收官**：平台 `LLMClient.rerank()`（Cohere/Jina 协议 `POST {base}/rerank`、独立 httpx、env RERANK_BASE_URL/MODEL/API_KEY/TIMEOUT 默认 3s、未配置抛 RuntimeError、观测入 call_logs endpoint=`rerank:{model}`+stage、索引完整性校验、close() 收口）；KG 侧 answer_question linearize 前重排 chunks（重排在 sources.chunks 构造前=[片段:N] 编号权威源自动对齐，P0#3 遗产兼容）；ChatBI 侧 retrieve() top_k 截断前精排候选（成功跳过 sort；签名加参由 ask_data 传=移植契约）。双插件 rerank_enabled 开关默认关（KG retrieval 组/ChatBI retrieve 组）。**验收对照**：观测✓（15 用例锚定）；KG 资料段重排✓；断端点降级完整返回✓（三层降级测试）；护栏口径 recall@5=1.000 不动✓（评测集回归）=链路未断；质量改善需第二口径（P0#1 注记）。**终审裁决**：平台观测落 call_type='llm' 而非蓝图原文 'rerank'——_log_retrieval_call 是 KG 插件设施平台不可反向依赖，且 admin 聚合只收 'llm'，endpoint+stage 已保过滤性。**遗留 nano**：诊断页（P0#2）序与真实回答序在开关开时不同（retrieve-test 不走 answer_question，设计如此）；KG/ChatBI 降级日志风格不一致。**P0 全部完成：#1 评测集 / #2 检索测试页 / #3 引用编号 / #4 rerank。下一步 P1（评测集扩容→混合化→…）**。

### P1（检索质量与评测，1-2 个月；**评测集先行**——检索改进的验收都依赖它）
5. **ChatBI 评测集（P0#1 最小集扩容）**：50-100 条 query→期望表/SQL 形态用例（参照 RAGFlow"内置公开基准 ms_marco/miracl + 自建用例"双轨，我们以自建业务用例为主），离线回归（不依赖外部服务，断言检索命中与 SQL 骨架）进 pytest。**照抄形态**：evalset.json{query,expected_tables,expected_sql,difficulty} + ranx 算 recall@k/MRR + SQL 比对执行结果集（终局裁决 A5：**骨架比对离线跑，结果集比对连评测集内置 SQLite fixture 库**，不依赖外部服务承诺保持）+ per-query 坏例排序报告。
   验收：改动检索/生成逻辑后跑评测集，准确率有数字可跟踪；#4 rerank/#6 混合化的效果以本评测集为准绳。
   **✅ 已完成（2026-10-15，merge 6adb771）**：60 条（easy25/medium26/hard9——h007 审查降级）+ `fixture_seed.sql`（六表 SQLite 种子 12/4/24/8/8/30 行）+ `expected_sql` 全量（SQLite 方言，PoC 验证 PG 形态兼容）+ EX 双口径（**投影类 29 条**：期望列按名投影+子多重集比对，27/29 断言≥checked-3；**整行类 31 条**：0/31 只打印——替身无聚合生成能力=口径边界非链路问题，真生成质量留真 LLM 评测）+ MRR@5 指标 + 难度×类别 16 格矩阵 + 坏例降序报告（`-s` 可见）。**三尺基线：recall@5=1.000 / mrr@5=1.000（n=60，护栏口径）/ EX-proj 27/29（链路连通口径）**。全量 1482 passed。**口径裁决（T1 修复环）**：expected_columns 双口径——非空=投影比对（列名与 cursor 一致），[]=整行比对（聚合/重名列）；分布 29/31 有固化断言防漂移。skeleton=expected_sql 全等有断言。**P1#6 对比工作流就绪**：改检索后跑 `pytest tests/domains/test_chatbi_evalset_ex.py -s` 看矩阵 diff。**留 P1#6 顺手项**：重名列比对器（m014 投影错位根因）/MRR 空集口径对称/坏例截断放开/骨架交集弱口径/EX 严格多重集（真 LLM 时）。
6. **ChatBI 检索混合化**：schema 检索加关键词路（终局裁决 A1：①融合公式默认 **RRF**（倒数排名融合，免量纲问题），权重待评测集校准；②包含方向=查询词与表名/列名/注释**双向包含命中**；③RAGFlow 双层权重先例裁决——采用其本地重排层语义（cosine 为主），引擎层配比不抄）（依据：02 号 §1 RAGFlow 混检经验 + ChatBI 审计『单路向量手调阈值』结论）（表名/列名/注释归一化精确+包含匹配）与向量路融合；rerank 前置精排让 LLM 精筛拿到更准的候选（精筛保留"宁缺毋滥"语义，不删除），阈值问题随融合降权（现状锚点：手调 0.35 在 chatbi/retrieval.py:59）。
   验收：**#1/#5 评测集上**表召回率提升（recall@5 基线对比）；LLM 调用次数不增。
   **✅ 已完成（2026-10-15，merge 53dadc4）**：新模块 `chatbi/hybrid.py`（tokenize_query 中文 2-gram+英文数字词+停用词 / keyword_scores 双向包含计分——正向每词+1、反向点名+3 **词边界判定**（审查 major 修复：裸子串会让 order 命中 order_status）/ rrf_fuse Σ1/(60+rank)）；retrieve() 身份过滤后融合候选（关键词新命中从 content 构造 SearchResult，score=匹配分；hybrid_fused 跳过 score sort 防量纲混排）；开关 `hybrid_search_enabled` 默认关（retrieve 组）。**关键裁决（T2 现场反转）**：融合段从计划"身份过滤前"移到"后"——探针+集成红证实 per-scope dict 重建会丢关键词命中；关键词 record 无 # 键 metadata 已设，移后真等价。**双基线：关=1.000/开=1.000（n=60，只补不删锚定）**；rescue 用例证关键词路独立价值（向量漏召回靠表名/列中文名点名补进）；4 开关态（hybrid×rerank）矩阵全覆盖；融合观测 `hybrid: kw_hits=… vec_top5=… fused_top5=…` INFO 日志。全量 **1498 passed**（+16）。**真实收益验证路径**：管理端开 hybrid 后看 kw_hits 分布与 vec_top5/fused_top5 差异（rescue 实际发生即收益证据）。**留 minor**：refine prompt score 量纲混杂（规则4 缓解）/多 scope 同名覆盖（ask_data 恒单 scope 不可达）/2-gram 跨词噪声（RRF 秩融合稀释）。
7. **KG 社区报告**：做成**可插拔编译通道**（KG pack 新可选任务类型，参照 RAGFlow Knowledge Compilation 的"模板+开关"形态，不做成固定特性——其 GraphRAG 三度摇摆后正是收编进编译体系）。**启动门槛（F 轮魔鬼代言人审查补）**：①血统风险——GraphRAG 谱系是 RAGFlow 摇摆最烈+Issue 热点（卡 80-90%）所在，抄的是未稳定解；②需求证据——用户月均提出"这个库整体讲什么"类问题 ≥5 次才启用（当前无此数据，默认不启动）；③评测盲区——现有评测集是 ChatBI 的，KG 侧无量化验收，先补"整体性问题"用例集再动工。**G 轮 PM 建议砍出排期；决策：保留门槛制——默认不启动即不占排期，需求证据出现再议**；收尾五步顺路加 leiden（networkx 内置）+ LLM 社区摘要 + 向量化入库；检索时命中实体反查所属社区。
   **成本估算**（按实测 563 实体库推算）：leiden 纯代码秒级；社区数约为实体数的 5-10%（~30-60 个），每社区一次 LLM 摘要调用（输入≈社区成员实体名+描述，约 1-2k token，输出 ~300 token）——**一次全量导入增加 30-60 次 LLM 调用、约 5-15 万 token**，与现有抽取批次（276 块 × 每批一次）相比增量 <10%；社区摘要走收尾阶段的限速令牌桶，不新增并发压力。
   验收："这个库整体讲什么"类问题可答；导入耗时增幅 <20%（LLM 调用增量 <10%）；不启用该通道时行为与现在完全一致。
8. **平台网关增强**：embeddings 拆 encode/encode_queries（bge-m3 本地后端两模式）；LLM/embedding 结果进程内+Redis 两级缓存（同参 24h）。
   验收：重复 query 检索链路 LLM 调用数下降可观测。
   **✅ 已完成（2026-10-15，merge 7ce38c7）——P1 实质收官**：embeddings 拆 `encode/encode_queries`（mode 参数缺省 document 零变化；查询语义调用点迁移 KG 两处）——**审查 critical 事实纠错**：查询前缀"为这个句子生成表示…"属 bge-*-zh-v1.5 系列 instruction，**bge-m3 官方模型卡明确"不再需要查询前缀"**——默认无前缀（与存量库口径一致零风险），`EMBED_QUERY_PREFIX=1` 开关留 v1.5 系模型用；平台嵌入缓存 `llm/embed_cache.py`（LRU+TTL 24h，键=实际出站 model+mode+texts——chat 模型名不冲嵌入缓存），命中零出站+`cache_hit` 观测（验收锚点：二次同参零出站断言）；chat 缓存 `LLM_CACHE_ENABLED` 默认关（全同 messages+model+temperature+max_tokens 命中，查询在限速前=命中不占配额）。全量 **1508 passed**（+10）。**遗留**：Redis 层未实现（依赖未装+单进程部署进程内已覆盖主收益，蓝图两级降一级+记录）；T1 审查抓的 bge-m3 前缀事实错误是"外部技术论断必须查官方文档"的标杆案例。**P1 终态：#5 扩容✅ / #6 混合化✅ / #8 网关✅ / #7 社区报告=门槛制默认不启动（不占排期）。剩余 P2（#9-#15 条件启动项）。**

### P2（工程闭环，2-6 个月）
9. **KG 消歧预筛**：全库名录先按类型分桶+Levenshtein/字符重合预筛，仅可疑对送 LLM 身份复合。
   验收：千级实体库身份复合 LLM 调用量降一个数量级。

   **✅ 已完成（2026-10-16，commit b685806）**：`dedup_prescreen.py` 纯函数（类型分桶→首字符分桶——5000 同前缀实体 81s/485 万对→1.71s/2 万对→Levenshtein≤2/字符重合≥0.5/别名桥接→单实体 8 对上限）+接线 `_run_consolidation`。**关键裁决：规模门槛**——名录>单批 120 实体才启用预筛，小库全量送审：预筛字符串阈值滤得掉「孙悟空/六耳猕猴」式近似对，滤不掉「齐天大圣/孙悟空」式仅语义可判同的核心对（身份复合的价值恰恰在此），无门槛全启用首版把 m3 六用例全弄挂。质量报告透出 prescreened/prescreenCandidates。测试：预筛 9 用例+接线 2 锚定（小库全量/大库砍名单）。
10. **KG 删文档图谱瘦身**：删文档时软标记+按受影响子图重建（RAGFlow utils.py:869-903 模式），不全量重抽。
   验收：删文档后图谱无孤儿实体，耗时与文档规模成正比。

   **✅ 已完成（2026-10-16，commit e30d9ae）**：审查裁决现状满足——核心语义（删文档时共享实体去引用保留+孤立实体清理）`delete_document` 已内建，RAGFlow 软标记重建模式不适用于真图库；补两验收锚定用例固化。
11. **ChatBI 证据卡升级**：制品补 per-table 行数/最近值样例；前端图表卡"数据来源"可点开 schema 卡。**G 轮 PM 审查：优先级分歧**——PM 判此为 P0 级信任刚需（业务用户第一恐惧="数字哪来的"），工程判 P2；折中判据：P0#1-#4 验收全过且评测基线建立后，升 P1 尾段——与引用编号（#3）构成"可信度双件套"。

    验收：答案可追溯到"用了哪些表哪些字段多少行"。

   **✅ 已完成（2026-10-15，commit 19da224，P0 验收全过+基线建立后按折中判据升档）**：artifact 新增 `table_provenance`（current_tables 每表的 schema 卡——列名/中文名/类型/表描述/指标清单，纯语义层组装零查询成本；表级行数不实时 COUNT（大表慢+估算误导），溯源数字面用制品 rowcount）；前端 BiChartCard 新增"**数据来源**"折叠区（表卡+列 chip+JOIN 路径+可展开执行 SQL）。浏览器实测：查询"各城市订单数量"→折叠区 5 张表卡/34 列 chip/SQL 可溯，"数字哪来的"完全可答。全量 1516 passed。与引用编号（#3）构成**可信度双件套**收官。
12. **KG 全文第三路**：kg_chunks 加 tsvector 列+GIN 索引，检索意图解析失败降级整句时走 PG 全文而非纯包含匹配（锚点：降级点 kg retrieval.py:84-99；kg_chunks DDL 在 store.py:80）。**选型依据**（RAGFlow 三引擎调研）：坚持 Milvus+PG 双路，抄其"表达式对象+每引擎翻译器+权重归一化"抽象，不引第三引擎；中文分词前置应用层（终局裁决 A2：**KG 归一化仅做预处理，不可当分词器**——仓库无分词词典；自建最小词典=实体别名表+表名列名，或引入 jieba，二选一在实施时按 #1 评测集效果定）。已知短板：ts_rank 粗糙、短语检索弱——按"够用"标准验收而非对标 ES。
    **量化门槛**：构造 20 条"意图解析失败降级"回归 case（含实体名部分匹配/别名/错别字三类），全文路 top-10 命中率 **≥0.6 且比纯包含匹配基线高 ≥15 个百分点**；单查询全文检索延迟 P95 ≤50ms（GIN 索引下）。不达标则回退纯包含匹配并记录差距原因。
    验收：上述 20 条 case 达标；call_logs 记 fulltext 路径命中明细。

   **✅ 已完成（2026-10-16，merge ace9372）**：kg_chunks 加 `search_text` tsvector 生成列+GIN（幂等迁移）；中文分词前置落成 IMMUTABLE SQL 函数 `kg_cjk_ngrams`（CJK 双字滑窗+拉丁原词）——PG 'simple' 不切 CJK，蓝图字面 to_tsvector('simple',text) 整句成单 lexeme 永不命中，偏差已记录；查询侧 `query_grams` 2-gram+最小停用词典（不引 jieba）；`search_chunks_fulltext`（ts_rank 排序）。触发条件重设计（蓝图注记的 5d9812c 裁决后「降级整句」已不存在）：**图谱零命中兜底**——seeds 空时全文补充召回，开关 `fulltext_fallback_enabled` 默认开；命中标注 source='fulltext'，与向量路同父块去重，向量降级早退不丢兜底命中。doc_parser 出口 NFKC 归一化（民法典 PDF ⼀/⼱ 兼容字符根因）。**验收**：20 case（部分匹配/别名/错别字）top-10 命中率 **1.00 vs 纯包含基线 0.50（+50pp，门槛 ≥0.6/+15pp）**；P95 1.74ms（≤50ms）；376 测试过。**后记（2026-10-16 b491193）**：蓝图原文"坚持 Milvus+PG 双路"已失效——Milvus 全量移除，向量现为 pgvector 单后端（与平台同库），全文路不受影响。
13. **平台费用观测**（依据：09 号 RAGFlow『token/耗时留痕但无统一费用落库』缺口反推）：call_logs 已有 token 数，加模型单价配置，管理端出按 pack/stage/天的费用聚合。

   **❌ 已剔除（2026-10-16 用户裁决"没有任何意义"）**：曾于 9cfb4f5 落地（单价配置+聚合+卡片），同日整体移除——端点/聚合/前端卡片/测试全删，数据库 model_pricing 键清理。
14. **ChatBI 反馈闭环接通**（**G 轮 PM 审查：建议提前至 P1**——点赞点踩是业务用户唯一发声渠道+需求证据来源；**决策：P1 末段条件启动，P0#1-#4 验收全过后升档**）:抄 RAGFlow chunk_feedback 模式（chat_api thumbup/down → ChunkFeedbackService 加权调 pagerank_fea，chunk_feedback_service.py:115-141）——给 `apply_feedback_signals` 补 API 入口（对话卡点赞/纠正按钮），按检索分加权微调表/字段置信度，配置开关默认关闭。
    验收：点赞/点踩后对应表/字段置信度可观测变化；关闭开关时行为与现在完全一致。

   **✅ 已完成（2026-10-16，merge 40aed76）**：`chatbi/feedback.py`——点赞/点踩 API（用户级+会话归属校验）调 `apply_feedback_signals`（语义零改动，含下调显式纠正）+前端按钮（ChatPanel 对话卡）+置信度微调接线；开关默认关，关=零行为变化。11 测试过。

15. **对话失败诊断卡（G 轮 PM 审查新增缺口）**：对话失败无诊断指引（RAGFlow 同病：失败仅弹错误无路）；解析进度无全局视图（我们 SSE 任务中心有基础但对话侧空白）。做法：对话失败时附 call_logs trace 链接的诊断卡（复用现有会话审计 ConversationTrace），指明失败环节与重试入口。
    验收：对话失败返回带 trace 链接的诊断卡；用户能从失败信息定位到具体环节。

   **✅ 已完成（2026-10-15，commit 9c4eaee+d0f216d）**：错误消息升级为诊断卡——失败环节分类（LLM 上游/检索/执行，错误文本关键词推断）+「查看本轮链路」（复用 TurnTraceModal 与 trace 端点，零新后端）+「重试」（重发该轮 user 问题）；**两条失败通道全覆盖**（SSE error 事件 + 工具 result.error——实测发现后者是主路径后补齐）。实测：坏 LLM 地址触发 502 → 红卡"LLM 上游服务故障"+链路弹窗可开。

## 4. 终局蓝图（6-18 个月，方向性）

- **协作差异化机会**：RAGFlow team 域文档量第一（21 篇）但实现最浅（me/team 两档），用户因协作弱迁出至 WeKnora——njmind 宿主自带组织/权限体系，双插件借宿主权限做"天然协作"是零成本差异化。
- **平台多副本化**：单实例是明示契约（deploy/single/README.md:4）。扩展路径：PG 任务表+SKIP LOCKED 竞争消费（不引 Redis Streams，维持存储栈单一；**租约心跳必须小于重试退避首值**——RAGFlow Go 版 InProgress 2s<BackOff 5s 的时序教训；无死信队列、超限丢弃靠 DB 状态兜底的取舍可鉴）；SSE 订阅外置为 PG LISTEN/NOTIFY 或轮询（轮询降级照抄 RAGFlow 条件式 refetchInterval：仅活跃任务时 5s，否则停）。前提是出现真实多副本需求，不为架构而架构。
- **LLM 网关多 provider**：OpenAI 兼容多基地址 + 模型路由表（哪个 stage 用哪个模型/价格档）；模型 key 加密列（pgcrypto）。
- **KG 知识编译家族**：图谱之外增加"大纲层"（文档目录+摘要 chunk）与"FAQ 层"产物，本体模板组产品化（参照 RAGFlow CompilationTemplate 的"内置模板+用户组"）。
- **文档理解升级走外部引擎**：接 MinerU/Docling 库获得版面识别+表格还原（RAGFlow 自己也把 8 个外部解析引擎做成可插拔），不自研视觉管线；优先级：表格还原 > 版面重排 > OCR。
- **ChatBI 记忆升级**：fewshot 库从"成功即回流"改为"回流+人工确认门"（复用确认门模式）；长期记忆加向量召回替代词重叠。
- **宿主数据源接入**：njmind 业务库表结构/数据经 AssetClient 契约同步进 ChatBI 数据源与知识库（同步任务用 pack 任务框架，fingerprint 增量模式照抄 RAGFlow）。
- **NL2SQL 竞品对标（G 轮竞品分析师审查新增；H 轮终审修正：从终局提为近期研究项，文献级 1-2 人天，与 P0 实施并行）**：问数侧零竞品研究是当前最大盲区——补对标 Vanna（DDL/文档/历史 SQL 三类语料 RAG 化+反馈自学习，与 ChatBI 架构一一对应）、DB-GPT（语义层+Spider 评测口径）、WrenAI（open context layer 业务口径层，中文企业问数最大痛点是口径非语法）；插件化侧补学 Dify provider/tool 插件协议。
- **开放出口（按需）**：MCP server 暴露 kb_search/ask_data 两工具，供外部 Agent 框架调用；OpenAI 兼容对话端点。
- **检索充分性护栏（SCA）**：参照 Go 主干四件套——存储/评审分离（评审只看 ranked view 不读原始块）、view-hash NoProgress（两轮视图相同即停）、三层时间预算（总 deadline/轮 headroom/枚举买断加时）、双轨记账进答案（模型 slot 表+框架探针台账）；缺口改写规则直接抄 sca_query_rewrite.md 八军规。详见《05-Agent画布与Agentic检索.md》与《11-Prompt工程.md》。
  **前置依赖（诚实标注）**：SCA 的本质是"多轮检索循环里的停机判定"，依赖检索层支持多轮往返（检索→评审→改写→再检索）。当前 KG/ChatBI 均为单轮检索架构，ChatBI 自愈回环在 SQL 层不在检索层。**启动条件**（满足其一）：① ChatBI 自愈轮次扩展到检索层（检索失败也触发回环）；② 出现 agentic 检索产品需求（用户要"多角度查证"式问答）。在此之前 SCA 仅为技术储备，不进近期路线——避免"方向正确、落不了地"。

## 5. 宿主集成约束（新增能力必须遵守）

1. 数据源接入：config.yaml 声明 services+paths；读写走 AssetClient.query_data/submit_data（sdk/asset_client.py:70-121）；preflight fail-fast。
2. 结果回写宿主：只走 artifact+前端 APPLY 确认门（ChatPanel.vue:164-167 双判定），落库归宿主；persist_artifact 是预留接口。
3. AI 产物两相式：AI 出 ops 指令，代码确定性合并（_incremental_ops.py 模式），未提及字段逐字节不变。
4. 异常约定：fail-closed（不猜测、不静默回退、半成品不过门）；不自造异常体系。
5. iframe 嵌入 token 传递：可参照 RAGFlow root loader 的 ?auth= 参数写 token 模式，但须改为**一次性短时效 token**（防 URL 留痕泄漏）。

## 6. 避坑清单（RAGFlow 的教训，我们直接绕开）

1. 模型 key 明文落库（db_models.py:1232）——我们若做模型管理，key 用加密列。
2. CORS `*` 与 Bearer 共存（apps/__init__.py:62）——开放出口时 CORS 白名单。
3. 检索内容无消毒直接拼 prompt——我们已有 defang（retrieval.py:293-296），保持并扩展到新检索路。
4. 沙箱 docker.sock 直挂+privileged——若做代码执行沙箱，选 rootless/代理模式。
5. 混合检索默认权重：向量 0.3/全文 0.7（search.py:602，本次复核修正），别照抄网上"0.7 向量"的说法。

## 7. 已领先项（明确不抄）

- 图谱质量管控：本体约束/原文锚定/别名独占/三重接地身份复合——RAGFlow 全无。
- call_logs 逐环节观测（stage/限速等待/检索词命中明细）——RAGFlow 检索级观测弱于我们。
- pack 声明制路由+事务装配+引擎零领域知识——RAGFlow 无插件抽象。
- 单进程内任务续跑/幂等/checkpoint——同等能力不需要分布式代价。
- 启动收敛僵死 importing（tasks.py:73-97）——RAGFlow 的永久 RUNNING 无后台清扫器，我们已有该防线。


## 8. 对标保质期与价值度量（H 轮终审补）

**重对标触发条款**：本体系锚定 2026-10 快照（Python v0.27.2 a024bea / Go main deb1235），以下事件触发局部重对标——①RAGFlow Go 1.0 正式发布（重审 01/02/14 号的 Go 侧结论）；②Vanna/WeKnora 大版本（若已做 NL2SQL 对标）；③6 个月定时复核（2027-04，跑 20 行脚本抽验 file:line 锚文本）。升级时并排克隆新版本，绝不覆盖旧快照。

**业务价值 KPI 映射**（技术验收之上的价值口径，18 个月后答"价值兑现没有"）：
- 引用编号（#3）→ 引用点击率（用户是否真的在看来源）
- 证据卡（#11）→ 问数采纳率（业务用户是否敢用 AI 给的数字）
- 反馈闭环（#14）→ 反馈率（点赞点踩占对话比）
- 评测集（#1/#5）→ 准确率趋势（回归曲线是否随迭代上升）
- 检索测试页（#2）→ admin 抱怨工单量（"为什么没召回"类是否下降）
- 失败诊断卡（#15）→ 失败后重试成功率（用户能否从诊断信息自救）
