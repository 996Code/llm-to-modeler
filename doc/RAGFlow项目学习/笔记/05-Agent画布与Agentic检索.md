# 05 · Agent 画布与 Agentic 检索

> 画布执行引擎、组件体系、人机协同，与 Go 主干的 agentic 检索运行时。

## 1. 画布执行引擎（agent/canvas.py）
> 🗺 对应链路图：[../图/ragflow-agent-canvas.html]（runtime 视图）

- **每轮重置防脏值**（:539-549）：除 Begin 外路径组件每轮清空 inputs/outputs——下游引用只解析本轮运行时值，长对话画布不串状态的根基
- **UserFillUp 人机断点**（:577）：画布跑到"等用户补充"可暂停，回复后从断点 resume——人机协同画布
- sys.* 隐式全局变量（sys.date/query/files/conversation_turns，:532,568-576）
- Webhook 触发模式：Begin 可设 Webhook，外部事件直接驱动画布（:551-560）
- 就绪节点分批 asyncio.gather 并行（:607-652）+ 全程 SSE 装饰事件（workflow_started 等）
- 组件异常走 _ERROR 输出变量不炸画布（component/base.py:507-512）；单组件调试 debug_inputs（:561-562）
- 结束输出 token/耗时用量 payload（:1133）

## 2. 组件体系（agent/component/）
> 🗺 对应链路图：[../图/ragflow-agent-canvas.html]（trigger-load 视图）

- 20+ 组件：begin/llm/**agent_with_tools**/retrieval/browser/categorize/switch/iteration/loop/exit_loop/fillup/message/code/excel_processor/data_operations/variable_aggregator/variable_assigner/string_transform/invoke/docs_generator
- ComponentBase 生命周期：init（_param.check）→ invoke_async → _run；inputs/outputs 声明式变量
- 画布持久化：UserCanvas + **UserCanvasVersion（版本化）** + CanvasTemplate；DSL 迁移有 dsl_migration.py
- 前端画布 UX：canvas/chat/debug-content/explore 四区（web/src/pages/agent/）

## 3. IM 渠道（api/channels/）
> 🗺 对应链路图：[../图/ragflow-agent-canvas.html]（human-loop 视图）

- 8 家：飞书/钉钉/企微/Discord/Telegram/Line/QQ 机器人/WhatsApp
- Channel ABC（core/base.py:33-60）：IncomingMessage/OutgoingMessage/set_message_handler——单条坏消息不炸渠道（:45-52）
- bootstrap.py 嵌入 API server：10s 轮询对账 chat_channel 表（启停/凭证变更热生效，_RECONCILE_INTERVAL_SECS）；渠道用户按 `channel:{id}:{chat}:{sender}` 隔离上下文（:86-88）；消息转表单回复或新提问（:67-83）

## 4. Go 主干 agentic 检索运行时（internal/agentic_rag/）
> 🗺 对应链路图：[../图/ragflow-agent-canvas.html]（human-loop 视图）

### 4.1 SCA 充分性反思（orchestrator/sufficient_context.go:69-108）
- 结构化契约：MissingPiece{What,SearchHint} + SubQuery{satisfied,missing_fact}；SCAResult{IsSufficient,Confidence,Contradictions,SubQueries,Claims}
- 只评审 claim 报告+300 字符证据锚点，**不读原始块**（防 prompt 膨胀）
- **存储/评审分离**：证据池自由累积，评审只看 ranked view（SCAViewCap=60，评分=相关度 0.45+覆盖 0.45+新鲜度，graph_sca.go:75）；**view 哈希两轮相同 ⇒ NoProgress**
- 判定 prompt：Sufficient-Context 论文 autorater 判据+三段式评审+Q-CARE 子查询分解（prompts/sca_select.md:16-34）

### 4.2 查询改写（query_rewriter.go:56）
- 单步 LLM RewriteGapToQuery；BridgeValues 锚定（已解析值入新 query 防重推）+ ResearchContext 防重（死角度不 paraphrase）
- 注意：生产调用未传 BridgeValues（仅测试传），锚定实际靠成员行（agentic_rag_graph.go:643-647）

### 4.3 三层时间预算（policy.go:53-118）
- TotalBudgetS=180 总墙钟 / MinRoundHeadroomS=50 起轮门槛 / 枚举题一次性买断加时 120s（需留 90s 下游储备）
- defaultMaxIterations=120 只是 workflowx loop 兜底（agent.go:114）——检索循环靠轮次+deadline
- 放弃路径：NoProgress → formalize；预算尽 → ComposeFallbackDraft；recordContract 防"slot 已有值仍答未找到"（:1300-1307）

### 4.4 双轨记账
- slot 表（模型账）+ 探针台账 reached/absent（框架账）同入答案 prompt——杜绝"检索到却没列出"

## 对我们的启示

- 画布不抄（无生态），但**UserFillUp 人机断点**思想可用于 ChatBI 澄清往返的暂停恢复
- SCA 四件套（存储评审分离/view-hash NoProgress/三层预算/双轨记账）是未来"检索充分性护栏"的现成设计
- 渠道对账模式（DB 表轮询热启停）比进程重启优雅，未来接 IM 时可抄
