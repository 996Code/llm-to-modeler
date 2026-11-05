# 在途工作快照（2026-11-05）

> 会过时。接手先核对实际状态（git log / 服务器任务队列），再决定本页还剩多少有效。

## 联网搜索全链路（2026-11-05，94919d4..d3f1e89 已推 origin，3.110 未部署）

- **web_search 独立公用 pack**（用户方案 A）：自有设置页(searxng_url/mihomo_api_url)+
  联网调研管理页；KG 摘除内嵌工具；客服 other 分支直连 websearch 底层(可选依赖)。
- **能力**：LLM 总结式回答(DeepSeek 式[来源:N]角标+来源卡纵列)、限流自动切节点
  (跨区域轮换+single-flight 防击穿)、zh-CN 结果本地化、指令前缀剥离、
  关键字核对诚实规则(问陈蔓答陈曼→开头声明不一致)、空结果不缓存。
- **对话开关**：输入区胶囊按钮(嵌入/独立共用)，pack_params.web_search.enabled 通路；
  插件禁用自动隐藏。对话链路/管理端/客服三入口全通。
- **本地测试栈常驻**：searxng-test(8888)+searxng-mihomo(19090 发布 controller)双容器。
- **3.110 部署清单**（网络恢复后）：①PACKS_ENABLED 加 web_search ②/data/base/searxng
  两个配置模板换新(三层代理组+短挂起+default_lang，见 doc/agents/deploy-searxng-3110.*）
  ③KG 设置页删旧 searxng_url、联网搜索设置页配 searxng_url=http://searxng:8080 +
  mihomo_api_url=http://searxng-mihomo:9090 ④deploy.sh --skip-pull。
- 质量门：全量 1893+447 绿、e2e 回归 9/9、CI #88 绿、代码审查两轮(1C/3I/8M 全整改)。

## 审查整改 + 客服窗口联网按钮（2026-11-05 晚，72be5c6 已推 origin 并部署 3.110）

- **终态审查 12 项缺陷(1C/3I/8M)全部修复**：C1=web_answer.j2 补 {{ query }}(答非所问
  根因)；I=service.py 共用编排层(工具/客服/KG 三入口收拢)+失败如实报错不静默回落
  KB+KG 委托(联网话术不落 KB 检索)；M=admin 端点停用守卫/synth 502+落库失败 logError/
  历史回看不重搜/citations.ts 文本节点级引用转换/能力声明(capabilities)/required_only
  语义/对话 store 竞态/日志 answer 列。独立复审 12/12 ✓ + 2 Minor(跟随者 cached 误标、
  trace 明细裁剪)已修。
- **客服窗口联网按钮缺失根因**：ChatPanel 显隐判据原是"窗口 packs 含 web_search"
  (嵌入窗口 ?packs= 过滤后永远拿不到)→ 改为 manifest capabilities 广告(kg/客服
  uses_capabilities 声明,底座禁用自动消失)。浏览器实测两测试窗口按钮均出现。
- **验收**：后端全量 2447 绿×2 轮；前端 vue-tsc 0 错+vitest 256 绿；本地真链路三入口
  (管理端/首页/客服直连/KG 委托)+开关关指引+trace 四打点(web_search.request/query/
  synthesize/search)+GUI 闭环(开关→联网话术→总结角标+来源卡)全过；CI #93 绿。
- **3.110 已部署 72be5c6 并验收**（2026-11-05 22:20）：原清单①②实测免做(.env 未配
  PACKS_ENABLED=全量候选;searxng 配置模板 11-04 已是新版)；③设置页已配
  searxng_url=http://searxng:8080 + mihomo_api_url=http://searxng-mihomo:9090
  (PUT /api/admin/packs/web_search/settings)；④deploy.sh --skip-pull(服务器 compose
  有 34 行部署侧覆盖——searxng 网络+equipment-mcp,reset 前备份 /tmp/compose.keep-1105
  后还原)。线上验收：管理端调研明德案例诚实规则 ✓、对话链路总结卡 ✓、浏览器两测试
  窗口联网按钮+客服 GUI 闭环(开关→计量新闻→总结+角标+来源卡)✓。


## 全站视觉精装 + LLM 降频（2026-11-03/04，均已推 origin）

- **UI 精装（4ab721a，已部署 3.110）**：设计令牌唯一源
  `frontend/src/shared/design-tokens.css`（唯一品牌蓝 #3370FF/文字深阶 #2B5FE0），
  admin.css/App.vue 聊天端变量整体重映射，管理端+聊天端+auth.html 统一；
  管理端无退出按钮（用户裁决）、知识库-测试去 ACL 化还原。
- **LLM 降频两批（7740fa9 已部署；817d5d2 起未部署——用户裁决 3.110 暂不更新）**：
  客服首问 6→4 次真出站（关键路径 3 次）、同题复访仅 1 次。机制：标题只生成一次/
  chatbi 关键词确定性路由（含首查防误切红线单测）/记忆提炼后台化/chat 缓存 stage
  白名单（默认开，LLM_CACHE_ENABLED=0 逃生门）/客服检索意图由槽位确定性构造/
  首轮标题并行化。质量回归脚本 `backend/tools/e2e_quality_regression.py`（S1-S4
  真链路验证答案正确性，动链路必跑）。D17 补检设置页开关已按用户裁决撤回
  （默认开不可见，4724ed1）。
- **请假申请插件已删（5a38d8c）**：演示 Demo 下线，六个 pack；CI pg-matrix 的
  兜底断言包同步换 njmind_list（#77 红过一次已修复回绿）。
- 线上 3.110 跑 7740fa9-11040051；4ab721a 之后共 6 个提交待下轮出包。

## 配置清理与代码对账（2026-11-02）

当前模型能力固定本地 bge-m3（1024 维），向量存储固定 pgvector；
LLM 主备供应商由数据库维护，非端点参数保留环境配置。配置消费者、
当前文档、模板和部署接线的审计见 [configuration-audit.md](../configuration-audit.md)。
KG 历史模型不兼容保护覆盖停用库再次导入、摘要向量写入、检索及块编辑。

## 图存储切换 Neo4j → PostgreSQL（2026-11-02）

图数据已全量迁入平台 PG（knowledge_graph schema 的 graph_nodes/
graph_entity_refs/graph_edges 三表；SDK 唯一实现 `PgGraphStore`，
边物理键补 doc_id 修复旧空 chunk 跨文档覆盖）。存量经仓库外一次性
导出器产中立快照（/tmp/neo4j-export-20261102，2476 节点/5250 原始边/
4018 去重三元组，0 异常），`backend/scripts/import_graph_snapshot.py`
导入并逐 scope 对账一致；幂等重跑 no-op，非空冲突整 scope 拒绝。
Neo4j 驱动/依赖/探针/设置字段/部署服务/env 键全部移除（真实 .env 的
NEO4J_* 已删）；旧 kg-neo4j 容器与数据卷保留作回退备份，未删卷。
嵌入测试页（知识库-测试/智能客服-测试）iframe ✕ 关闭按钮补宿主 CLOSE
监听：收起预览+可重新打开，浏览器双页实测通过。
运行栈已切 PG 图：图谱浏览 37 节点/42 边、检索 200 与 Neo4j 时期一致。
四项清理冻结版本全量 pytest 2379 例通过（749.21s），前端 240 例及生产构建通过。
真实明文数据源创建、PG 连接健康（6ms）及不回显密码验收通过；合成连接已清理。
新代码检索原合成文档 HTTP 200，正确引用数字温度计校准 12 个月、维护 6 个月。
计量相关 196 例含真实 MCP HTTP 测试通过；运行注册仍有 pending、现存数据源停用，
未据此声称运行计量业务已贯通，也未擅自启用。
数据库路径只接受显式迁移参数；KG 不再提供模型覆盖；ChatBI 密码直接使用 password，
旧密文不读取、不回退；计量环境绑定已从执行、诊断、页面和存量场景撤除。
当前 18080 已切回新代码，源码快照已停，不保留旧逻辑运行服务。
正常本地栈已恢复原配置：七插件仍在，chatbi/knowledge_graph/metrology_assistant 启用。
真实验收库「配置清理验收-20261102」（c51818a7-838a-4b65-8d07-07f39ef0c8b9）
保留供复查：1 份合成 Markdown，导入 71.3s 成功、8 节点/8 关系、2 条 1024 维向量；
检索召回 5 实体/4 关系/2 片段，答案正确引用新文档的 12 个月校准、6 个月维护。
已有强检库六页签真实浏览器检查，图谱 37 节点/42 边，检索正常。
本轮曾以隔离配置占用 18080 导致仅显示知识库及依赖错误，已停止隔离进程并恢复正常服务；
后续隔离验收必须用独立端口，不覆盖用户正在使用的标准本地栈。

## 知识库用户/角色同步 API（2026-10-29）

> **已整体下线（2026-11-03）**：知识库不再做数据隔离，用户/角色目录同步、库级 ACL、AclEditor 管理页、嵌入测试页与相关测试全部删除（用户裁决）。以下为历史记录。

源系统经 HTTP 同步用户/角色进知识库目录（ACL 候选字典）：全量（整体
替换+diff 计数+幂等）/增量（upserts 合并+deletes 如实报 missing）/单条
CRUD（PUT 建改合一）/`GET /sync/status` 对账；kg_subjects 扩 status/
created_at 列+kg_subject_sync_logs 流水表（留 200 条）。**字段精简裁决
（用户）**：只留平台自己消费的 id/name/status——attrs 扩展属性曾设计
后砍掉（当时零消费方，为不存在的场景预留）。文档
`doc/知识库用户角色同步API.md` 已按**对接方视角**重写（用户裁决：只写
对方要做什么，不写我们内部怎么做——含运行时身份传递章节：直调 API
与浮窗 SDK 两种带法）。**鉴权三选一**
（用户裁决方案 B）：管理凭证（Bearer/X-Admin-Token）或 `X-Api-Key`
（平台 MCP 密钥，与 /api/mcp 同一把、轮换 5s 同源生效，仅目录/同步
端点接受）——sdk.pack_api 新增 admin_or_mcp_key_required 依赖倒置实现。
顺带修既有测试卫生 bug：test_chatbi_api_endpoints 裸覆 `_admin_auth_fn`
不恢复（泄漏后全进程管理鉴权恒放行，组合顺序假失败）。改动面：kg 的
api.py/store.py、sdk/pack_api.py、main.py、conftest.py、
test_kg_subject_sync.py（停用条目不进 ACL 候选；删除目录条目不自动改
ACL——文档已如实写明）。同批带前端修复：历史对话二次载入被
selectConversation 竞态守卫误杀（代际计数器替代；3 例回归测试）。

## 智能客服知识库兜底（2026-10-29，已合入 main 随本轮部署）

未命中任何能力时走**知识库链路兜底**（用户裁决，2026-10-29 二次定案：
撤 `fallback_kb` 指定库配置——兜底=知识库链路本身，不是某一个库）。
`_fallback_answer` 直调 `retrieval.answer_question_multi`（kb_search 同款
跨库检索链），参与库按当前身份 ACL 过滤（被拒库静默跳过，不向客户透
拒权话术——初版曾把"无权访问「X」知识库"透给客户，用户否决）；有证据
出「知识库检索」证据链卡（fallback 标记+来源库清单），无证据/无可用库落
「暂不支持」引导卡（已检索注明），检索异常如实 error_for_llm。
`fallback_kb` 配置键/load_fallback_kb/runtime 缓存字段全撤（存量配置残留
键被忽略）；dry-run 未命中文案改为"将走知识库跨库检索兜底"；管理页说明
同步。链路图 customer-service-routing（总图吸收旧 插件总览/能力路由/端到端 三张,渠道接入+能力接入+兜底全在一张）与 flows/customer-service.md（⓪ 节）
与 flows/customer-service.md（⓪ 节）已体现新逻辑。契约测试重写 9 例
（tests/domains/test_metrology_kb_fallback.py：跨库/ACL 过滤/无证据/无可用
库/检索异常/命中不受影响）。
后续台阶（未开工）：数据查询能力接入 ask_data 库层+pack_params 数据源
白名单；管理页三组化+编辑抽屉契约只读收敛。

## PACKS_ENABLED 语义改版（2026-10-29，已部署 99.22）

env 从"锁死启停"改为**部署上限**：配置后名单外插件不加载、管理面不显示，
名单内管理端自由启停（热切换）；未配置=全量放开。改动点：
`pack_state.py`（启动期 enabled ∩ env 封顶，堵状态文件架空 env 的隐形运行）、
`api/admin.py`（toggle 撤整体 422 → 只拦名单外；payload 字段 envPinned→envAllowlist）、
前端 PacksTab（撤禁用开关，env 生效时显示提示条）。插件间依赖与启停守卫
（A 依赖 B：启用校验 B、停用 B 拦截已启用的 A）原本就有，未动。
**已提交 15fad70 双远程推送，99.22 部署 15fad70-10291213 并实测通过**
（列表只显 3 白名单内+envAllowlist=true+名单外 422+source=env 3/7 enabled；
导入队列断点续跑无损）。3.110 仍跑旧语义包，下次出包部署后生效。

## 常州院 KB 初始化（99.22 服务器）

- 结构：常州院（文件夹）> 外部文件 + 内部文件（两个库，kind=kb——文件夹不存文档）
- 内部 3 份：450MB 程序文件 succeeded（282 块）；162MB 管理手册导入中；87KB CMA 汇编排队
- 外部 3118 个规程 PDF（3.8GB，`~/Downloads/国家规程规范文件`）待批量上传+导入：
  批量导入端点 `POST /kbs/{kb_id}/import`（默认处理全部"未成功"文档），
  预计数十小时（LLM 逐块抽取，同库串行）
- 导入体验已定型：上传成功即自动提交导入任务（前端 DocManager + 建库向导同口径）

## feat/bid-demo 分支

南京院智能客服竞标 demo 在途。用户手上有未提交文件（tools/mcp-e2e/*、
chatbi/controlled.py、kg/service.py、metrology_server.py 等）——**不要动**。
需求细节见 doc/private/（不入库的内部文档专区）。

## 已定型机制（改动前先了解）

- 多层文件夹：上限 4 层（建+移按整棵子树核算）；树 UI 图标即开关 + 竖向连接线
- 删库五面联动清理（元数据/分块/文档/标注/ACL + 图 + 向量 + 文件）；清理失败 503 保留 KB 行可重删
- 上传 500MB 流式；>64MB 跳过解析预检；导入解析完即释放原始字节
