# 11 · Prompt 工程

> rag/prompts/ 47 个模板的措辞技巧——可直接迁移进我们的 j2 模板。

## 1. 引用生成教科书：citation_prompt.md

- **8 类必引**：数字统计 / 时间 / 因果 / 比较 / 技术定义 / 直接引述 / 预测 / 争议观点
- **明确不引**：常识、过渡句、LLM 自己的综合推理（"除非直接来自来源"）
- 格式铁律：`[ID:i][ID:j]` 分开不合并、每句最多 4 个、放标点前、不改原文空白
- 6 个示例含反例（"巴黎是首都"不引，"1889 建成"要引）与 RTL 处理
- 判据口诀：**"fact-checker 需要核实吗？"**
- 双路径并存：代码后处理（insert_citations 句级相似度）+ 本 LLM pass——前者可靠、后者语义准

## 2. 多轮问句合成：full_question_prompt.md

- 已完整则**原样返回**（不做无谓改写）
- **相对日期锚定**：注入 {{today}}/{{yesterday}}/{{tomorrow}}，"上个月"→绝对日期——检索器拿到可直接命中的时间词
- 输出语言跟随原问题（可强制 language）

## 3. SCA 缺口→查询改写八军规：sca_query_rewrite.md

1. 查询必须点名缺失实体+所需关系/属性
2. **多跳锚定 bridge value**（"MASH/Cheers 是收视率最高的两集"已解析 → 查"runtime"必须带锚，不能裸查）
3. 实体+缺失属性直接组合
4. 消歧限定词仅在证据支持时加；**绝不从问题臆断实体类别**（"Ron Hutchinson 可能是冰球运动员，除非证据确认别写 footballer"）
5. 查询自包含，**禁代词**（he/it/this）
6. 一缺口一查询，近重复合并
7. 不可检索化的缺口丢弃
8. **多样性**：对照研究史，死角度不 paraphrase 重试；`A|B|C` 交替查询覆盖一词多实体

## 4. question/query 双轨：multi_queries_gen.md

"给 LLM 的问题（5-200 字）"与"给检索器的查询（1-5 关键词）"**分开生成**——语义完整性与检索命中率各取所需；失败驱动（原查询没召回才生成 2-3 个互补查询）。

## 5. 反思分级：reflect.md

- 复杂度评分矩阵：范围/数据依赖/决策点/风险各 1-3 分（共 4-12）
- 反思长度三档：简单 50-100 字 / 中等 100-200 / 复杂 200-300——**防反思膨胀**
- 任务交接段（状态/关键数据/未决项/用户口径状态）不受字数限制

## 6. 其他值得记住的

- **sufficiency_check.md**：极简 JSON 契约 {is_sufficient, reasoning, missing_information[]}——充分性判定最小实现
- **sca_select.md**（11.5K 最长）：Sufficient-Context 论文 autorater 判据+三段式评审+Q-CARE 子查询分解
- **related_question.md**：相关问题推荐（产品功能，我们暂无）
- **rank_memory / summary4memory**：记忆排序与整理
- **toc_relevance**：LLM 挑相关章节（TOC 检索增强的 prompt 侧）
- **vision_llm_describe**：图表描述（多模态）

## 迁移清单（对应蓝图项）

| 技巧 | 去处 |
|---|---|
| citation 8 类必引+反例 | P0#3 KG answer.j2 引用编号 |
| 相对日期锚定 | ChatBI 多轮（"查上月"→日期区间） |
| question/query 双轨 | KG query.j2 升级候选 |
| 八军规 | 终局 SCA 检索护栏 |
| 反思分级 | Agent 循环防膨胀（未来） |

## 7. 入库增强 prompt 细节（第六轮考古）

- **auto_keywords**（keyword_prompt.md）："text analyzer，提取 top {{topn}} 重要关键词/短语，与原文同语言，英文逗号分隔，只输出关键词"——temperature=0.2，user 消息就是 "Output: "
- **auto_questions**（question_prompt.md）："提出 {{topn}} 个问题，语义不重叠、尽量覆盖主要内容、同语言、一行一个、只输出问题"
- 后处理：剥 ``` 围栏、含 **ERROR** 返回空；关键词按 `[,，;；、\r\n]+` 切分进 important_kwd；问题按行切进 question_kwd
- 限流：chat_limiter 并发 10；缓存 key 带用途标签（见 02 笔记 §5.6）
