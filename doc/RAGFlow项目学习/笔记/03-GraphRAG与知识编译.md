# 03 · GraphRAG 与知识编译

> RAGFlow 的图谱构建、消歧、社区报告，以及"知识编译"统一体系。

## 1. 三档抽取
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（extract-path 视图）

- general（MS GraphRAG 风）/ light（LightRAG，默认）/ ner（spaCy 免 LLM），_select_extractor 分发（general/index.py:122-138）
- 两档 LLM 输出同构分隔符协议 `entity<|>名<|>类型<|>描述`，解析在 utils.py:344-387
- light prompt 要求 content_keywords 并禁止幻觉描述（light/graph_prompt.py:17-48）
- gleaning 追问循环（light/graph_extractor.py:88-104）

## 2. 合并与消歧
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（resolve-community 视图）

- 同名节点 description/source_id 以 `<SEP>` 追加、边 weight 累加（utils.py:306-336）；整图 networkx 存成一个 chunk（knowledge_graph_kwd="graph"，:564-574）
- **消歧省钱技巧**：同类型分桶 → Levenshtein/字符重合预筛（is_similarity，entity_resolution.py:275-289）→ 100 对/批 LLM 判同（:111,152）→ 连通分量合并（:166-187）→ 重算 pagerank
- 关系清洗：负向判断正则+主语错配剔除（index.py:650-728）

## 3. 社区报告
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（resolve-community 视图）

- leiden 分层 → LLM 产 {title,summary,findings,rating,rating_explanation}（community_reports_extractor.py:70-160，校验 :144-153）→ community chunk 写入（index.py:937-967）
- 检索按命中实体反查报告 top1（graphrag/search.py:277-295）

## 4. 降维索引与血缘
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（index-search 视图）

- 实体/关系/社区报告全部 chunk 化，source_id 回指 doc_id（utils.py:414-446）
- available_int=0 使图谱块不进普通检索，检索请求强制放开（search.py:643）
- KGSearch：query_rewrite 抽类型/实体 → 实体向量+类型 pagerank 排序+关系向量+n-hop 路径，P(E|Q)=pagerank×sim（search.py:139-221）

## 5. 增量与删除
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（全图）

- 每 doc 子图 checkpoint（index.py:209-253）+ KB 级 Redis 锁+阶段 marker 跳过已完成阶段（:461-474,601,630）
- 删文档：软标记 removed_kwd="Y"（document_service.py:549-570）→ 按子图 rebuild_graph（utils.py:869-903），不全量重抽
- v0.26 社区抽取/实体消歧支持 checkpoint+resume（release notes："最贵最耗时环节"）

## 6. 知识编译统一体系（v0.27 的归宿）
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（全图）

官方定义（docs/guides/knowledge_compilation/overview.md）：把非结构化文档按"编译模板"生成结构化知识工件——图谱/树/页索引/思维导图/时间线/知识页。

- wiki：MAP（逐 doc 抽行）→ KB 级 REDUCE → PLAN → REFINE，产出 wiki_page/wiki_entity/wiki_relation（dataset_wiki_generator.py:17-41）
- structure_graph/mindmap/timeline/session_graph：run_structure_merge 产出带 compile_kwd+from/to_entity_kwd 的实体/关系行（dataset_structure_merger.py:83-96）
- RAPTOR：**相邻余弦相似度一维分水岭聚类**（非 k-means，阈值百分位+ratio 上限，raptor.py:120），逐层聚类→LLM 摘要递归，≤8 节点塌缩防退化（:369），防死循环保护（:411）
- 产品化：CompilationTemplate+Group（内置模板+用户模板组，db_models.py:1603 起）
- **演进教训**：GraphRAG/RAPTOR 独立开关三度摇摆，v0.27 收编进编译体系并从 UI 移除旧入口（release_notes.md:95）——"模板+开关"是这类能力的最终形态

## 7. Go 主干的知识编译
> 🗺 对应链路图：[../图/ragflow-graphrag.html]（全图）

C++ ThincNER 做 NER/关系抽取（CGO+onnx，ner_extractor.go:17-24）；knowledge_compiler 产超图（tree/structure/wiki/mindmap 变体，scheduler.go:61）；MySQL 行锁+claim token 竞争消费（:103,295）。

## 对我们的启示

- 我们图质量管控（本体约束/原文锚定/三重接地/别名独占）RAGFlow 全无——护城河保持
- 可抄：消歧预筛（省钱）、删文档子图重建（图谱瘦身）、社区报告（全局问题能力，做成**可插拔编译通道**）
- 不抄：图谱降维 chunk 化（丧失图查询，我们场景是关系链问题）
