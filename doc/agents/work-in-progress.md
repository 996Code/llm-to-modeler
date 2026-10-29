# 在途工作快照（2026-10-29）

> 会过时。接手先核对实际状态（git log / 服务器任务队列），再决定本页还剩多少有效。

## 知识库用户/角色同步 API（2026-10-29）

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

未命中任何能力时先检索兜底知识库（配置文档顶层 `fallback_kb`，管理页整档
PUT 维护），有证据才按证据链回答，无证据/未配置落引导卡（已检索会注明）。
授权沿用 knowledge_graph pack_params；联调预演未命中文案随配置变化；
契约测试 9 例（tests/domains/test_metrology_kb_fallback.py）+ metrology 全量
179 例绿；本地四服务（18080/13080/9771/9772）浏览器实测：兜底命中出
「知识库检索」证据链卡、无证据如实不编造、BJ2026 走 9771 受控出进度卡、
预演显示兜底文案。独占全量 pytest：2314 过/1 失败——test_mcp_server::
test_full_roundtrip 网关 401 为测试顺序性偶发（同分支单跑 0.36s 过，
分支改动仅 metrology 不涉 MCP 网关）；此前 41 失败+10 错为与另一会话
并发撞测试库的假失败，独占重跑后全部消失。
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
