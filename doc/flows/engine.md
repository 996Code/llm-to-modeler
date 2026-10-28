# 对话引擎链路走读（代码级）

> 所有插件的对话都走这一条引擎链路：SSE 进 → 意图路由 → LangGraph 工具
> 调度 →（可选中断追问）→ SSE 出。链路基线：c374f9c。

```
POST /api/config/chat (SSE)
  │
  ▼ api/config.py: chat()
┌──────────────────────────────────────────────────────────┐
│ ① 会话归属校验(带 conversation_id 必查归属,外人 404)       │
│ ② 补答分支: answers 非空 → Command(resume) 恢复断点        │
│ ③ LangGraph StateGraph 执行(app.state.graph)              │
└──────────────────────────────────────────────────────────┘
  │
  ▼ engine/nodes.py (图节点)
┌──────────────────────────────────────────────────────────┐
│ classify_intent ──LLM──▶ 命中 pack+tool                   │
│   packs 声明收窄:宿主声明什么就在什么里选                   │
│   单 pack 直通(零 LLM);全量部署走语义路由                  │
│ execute_tool ──▶ 插件 Tool.execute(state, ctx)            │
│   返回 AskSpec? ──是──▶ interrupt 挂起                    │
│                    └──否──▶ 制品/结果 → 组流               │
│ stream.py: SSE 事件编排 + 落库(消息/制品/追问卡)           │
└──────────────────────────────────────────────────────────┘
```

## ① 入口与安全闸

`api/config.py: chat() / chat_resume()`：

- **归属校验**：请求带 `conversation_id` 时先查会话归属（user_id 语义：可信
  网关注入的 `X-User-Id`，匿名=本地开发开放模式），不一致 404——在进入
  SSE/checkpoint **之前**，防跨用户套取历史与补答。
- 请求体（`ChatRequest`）：`message / conversation_id / answers / image_base64
  / packs / pack_params / services / context`。`packs` 是宿主的路由候选声明
  （嵌入门户初始化什么，对话就只在什么里走）；`pack_params` 是插件参数默认值
  （如指定默认知识库），用户消息里的显式值优先。

## ② 意图路由（classify_intent）

`engine/nodes.py`：

- 候选集 = 启用插件声明的工具 `when` 描述。LLM（stage=`route_pack` →
  `route_tool`）输出命中的 pack 与 tool；零 LLM 快路径：`packs` 声明过滤后
  只剩 1 个 pack 且该 pack 单工具时直通。
- 图片消息（`image_base64`）→ 多模态识别节点先行。

## ③ 工具执行与中断恢复

`engine/nodes.py: execute_tool` → 插件的 `Tool.execute(state, ctx)`：

- `ToolContext`：`llm_client / session_state(会话 KV 记忆) / conv_id / user_id /
  emit(stage 事件)`。工具返回 `ToolResult`：`artifact`(制品) / `ask`
  (AskSpec 追问) / `error_for_llm`(技术错误，引擎兜底成用户话术)。
- **追问中断**：返回 `AskSpec` → LangGraph `interrupt`。AskQuestion 带
  `question_id`（不透明令牌，各插件自定生成；客服=uuid，其他插件可不带）。
  挂起状态存 checkpointer（PostgreSQL），pending questions 原样持久化到会话
  历史（`clarificationQuestions`）——历史卡自带令牌，不依赖"当前 pending"。
- **补答恢复**：下一条请求 `answers` 非空 → `Command(resume=answers)` 回到
  断点。`engine/stream.py: clarify_gate` 把 answers 注入 tool_state；插件在
  execute 里读 `state["clarify_answers"]`（含 `__question_id`），自行校验
  令牌（客服：带错拒、不带兼容放行）。
- **checkpoint**：LangGraph checkpointer 落 PG；进程重启后 interrupt 状态
  可恢复（客户端断线重连走 `chat_resume`）。

## ④ SSE 事件流

`engine/stream.py` 逐事件下发，前端 `conversationResult.ts` 消费：

| 事件 | 内容 |
|---|---|
| `stage` | 进度（stage key + 中文消息；插件用单调闸门防倒退） |
| `pipeline_definition` | 工具的步骤条定义 |
| 工具制品 | `artifactType: data` 等，前端按插件注册的渲染器出卡 |
| 追问卡 | AskSpec.questions（含 question_id）→ 选项按钮/输入框 |
| `status: done` | 收尾 |

序列化注意：`nodes.py` 统一 `model_dump(exclude_none=True)`——不带令牌的
旧工具事件形状与历史完全一致（新字段可选，双端可独立升级）。

## ⑤ 会话记忆与观测

- **会话 KV**：`session_state`（插件 `state_scope` 隔离），客服存
  `capability_slots`（按能力隔离的槽位背景）与 pending 兜底。
- **调用日志**：每次 LLM/向量/图谱/上游调用经 `_log_retrieval_call` /
  llm client 落 `call_logs`（管理端「记录→调用明细」按 stage 排查：
  `assistant.select/extract/conclude`、`kg.query/query_embed/vector_search/
  subgraph/find_entities`、`route_pack/route_tool`…）。
- **审计**：管理端写操作走 `audit-logs`。

## 与插件的契约面

- 工具声明：`Tool.name / description / when / input_schema / state_scope /
  preflight / execute`。
- 追问：`AskSpec(questions=[AskQuestion(question, header, options,
  question_id?)])`。
- 制品：`artifact_type` + payload；前端按 `plugins/<pack>` 注册的 presentation
  渲染（通用表格卡/专用卡）。

深入各插件：[customer-service.md](customer-service.md) ·
[knowledge-graph.md](knowledge-graph.md) · [chatbi.md](chatbi.md)

## 数据形态速查

`POST /config/chat {message, packs, conversation_id?}` →
route_pack/route_tool `{"pack": "metrology_assistant", "tool": "metrology_assistant"}` →
execute_tool state `{user_input, clarify_answers?, …}` →
AskSpec `{questions: [{question, header, options, question_id: "uuid"}]}` →
interrupt（checkpointer 落 PG）→ SSE 追问卡 →
补答 `{answers: {"器具用途": "贸易结算", "__question_id": "uuid"}}` →
`Command(resume)` → 工具续跑 → 制品/结论 → SSE `status: done`。
