# 智能客服链路走读（代码级）

> 一句话从进入到拿到带依据的回答，数据在代码里的完整路径。
> 配套图：`doc/diagram-src/customer-service-bid.workflow.json`（投标全景图）。
> 链路基线：c374f9c（2026-10-28 通用化改造后）。

架构一句话：**平台只留通用机制（选能力→提参数→追问→计划→受控执行→结论），业务语义全部来自 MCP 声明与场景表配置**——计量演示是 `legacy_demo.py` 里显式兼容的一套存量，不是引擎的一部分。

```
用户消息
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ 引擎(api/config.py): 归属校验 → route_pack 选中本插件        │
└─────────────────────────────────────────────────────────────┘
  │
  ▼ MetrologyAssistantTool.execute (tools/assistant.py)
┌─────────────────────────────────────────────────────────────┐
│ ① 路由表合成 _runtime_scenarios()                            │
│    场景表(DB 覆盖,resolve_scenarios 合并服务端最新声明)        │
│    ∪ auto_scenarios(未编排 MCP 工具按声明自动合成)            │
│ ② 两段式提取(LLM×2)                                          │
│    select: 选能力 → extract: 只提该能力的参数(typed)          │
│ ③ 追问(缺槽/类型错) ──问题令牌 question_id──▶ 挂起等补答      │
│ ④ 计划解析 prepare_plan (capability_plan.py,纯函数)           │
│    KG: 选库规则→查询模板→结论模式 / ChatBI: 数据源解析        │
│ ⑤ 受控执行                                                    │
│    KG: retrieve_evidence(带身份/ACL) → 检索三路               │
│    ChatBI: execute_controlled → MCP 工具(jsonschema 验参)     │
│ ⑥ 结论生成(LLM×1): 三态判定/通用回答 + 引用编号校验           │
└─────────────────────────────────────────────────────────────┘
```

## ① 路由表合成——能力的唯一事实源

`tools/assistant.py: _runtime_scenarios()`，每轮对话现算一份：

```
table = scenario_config.load_scenarios()          # DB 场景表(管理员覆盖)
      → capability_import.resolve_scenarios(table) # 覆盖行与服务端当前声明合并
      → {**auto_scenarios(resolved), **resolved}   # 并上未编排工具的自动合成
```

三个关键机制（全部在 `capability_import.py`）：

- **能力身份 = `(server, tool)`**。`capability_key()` 生成 `metrology::query_progress`
  式的 key；跨服务同名工具互不遮蔽。场景表里旧行没有 server 的，由
  `_action_identity()` 经数据源 host 反解（显式 `datasource_id` → env 缺省 →
  数据源清单查 host）。
- **覆盖只改话术，改不动契约**。`resolve_scenarios()` 对每个 chatbi 覆盖行，
  把 `action.input_schema / output_schema / annotations` 替换为注册表快照里
  该工具的**当前声明**，并按声明补全参数映射与提示；服务停用/探测失败/工具
  下线时该能力整行移除（用户看到"暂不支持"而不是拿旧契约硬调）。
- **自动路由（注册即识别）**。`auto_scenarios()` 遍历启用且探测成功的服务快照，
  跳过：已被场景表编排的（`_scenario_bindings`）、声明不完整的
  （`_blocking_issues`：描述<6 字 / schema 非法）、无可执行数据源的
  （`resolve_datasource_for_tool`：限定工具的固定源 → 该服务通用源 →
  多候选歧义时不猜）。快照 60s TTL 后台重探测（`_refresh_stale_snapshots`），
  管理页「刷新」走 `GET /config/routing?refresh=true` 同步强制重探测。

场景表装载（`scenario_config.py`）：

- **保存 strict / 装载宽松双档**。保存（`config_api.py` PUT）强制：kg 动作
  必须绑库或选库规则、非 answer 结论模式必须有查询模板；装载对存量缺字段的
  回退宽松（原文检索），不拒绝启动。
- **空安装零业务**：`factory_default_config() = {"scenarios": {}}`。计量演示
  场景只存在于 `legacy_demo.py`，由 `setup_demo.py` 显式初始化或
  `upgrade_legacy_scenario()` 识别**未被人工改动过的存量原始声明**补全
  （conclusion/查询模板/非强检用途兜底选库规则/instrument 提取提示）。
- **DB 故障不回退演示**：装载失败用进程内最后有效快照；无快照直接报错。

## ② 两段式提取——先选能力，再提参数

`tools/assistant.py: extract_slots()`，两次 LLM 调用（均为 `chat_json`）：

1. **select**（stage=`assistant.select`）：把路由表的 `{key: label, intent}`
   给模型，只输出 `{"scenario": "key"}`。单能力部署可跳过（路由直通）。
2. **extract**（stage=`assistant.extract`）：**只给命中能力的参数声明**
   （`capability_plan.parameter_specs()` 产出：slot/schema/hint），提示词要求
   保留原生 JSON 类型（`0`/`false` 不丢）。同名参数不跨能力串扰——这是
   通用化改造的核心修复（旧版全场景槽位混提，`name`/`instrument` 互踩）。

会话背景按能力隔离（`mem['capability_slots'][scenario]`），本轮明确值优先于
背景值（`merge_slots()`）。

## ③ 追问与恢复——问题令牌

`prepare_plan()`（纯函数，`capability_plan.py`）按声明 required **全集**逐项
检查：缺槽或类型不符 → 返回 `will_ask`（追问规格：header/question/options）。

- **零参数/全可选工具**：无 required 即不追问，直接执行。
- **类型归一**：`typed_value()` 按参数 schema 转换（integer 用 `Decimal` 不丢
  精度；boolean 接受 true/false；object/array 解析 JSON 并递归校验本地
  `$defs`/`oneOf`；enum/minimum/maximum 校验）。声明允许 null 的
  （`type: ["integer","null"]`）显式 `null` 算已提供。
- **追问令牌**：每个 AskQuestion 带不透明 `question_id`（uuid，
  `sdk/tool.py`）。前端把所点卡片的令牌回传 `answers.__question_id`——
  **带错令牌=旧卡重放，拒绝**；不带令牌（嵌入宿主/旧前端）保持旧语义放行。
  补答只认 pending 里的槽位（`parse_clarify_answers(pending=…)`），不再扫
  全场景中文标题猜。
- **持久化**：pending（含令牌+声明快照 hash）落 flows 表（`store.advance_flow`
  CAS 推进 revision）；补答时声明快照变了（管理员改配置/服务声明变更）→
  明确提示"该追问已过期，请重新提问"，不拿旧答案套新配置。
- 引擎侧：AskSpec 触发 LangGraph interrupt（见 [engine.md](engine.md)），
  补答经 `Command(resume=answers)` 回到断点。

## ④ 计划解析（prepare_plan 细节）

同一份纯函数被**对话执行与联调预演共用**（`config_api.effective_scenarios`
→ 同一合成路径），保证"预演过=保存后行为一致"。

- **KG 动作**：`kb` 显式 → `kb_rules`（`[{slot, in: [值], kb}]` 依次匹配，
  规则槽位也进参数规格可被提取）→ 都无则报错（**不猜库**）。
  `query_template` 支持 `{槽位}` 占位 + 内置 `{user_input}`/`{query}`（用户
  提炼的同名槽优先）；模板引用但本轮缺席的槽位补空串（追问流中器具名可能
  还没提供）。`conclusion` 决定答案模式（见⑥）。`answer_prompt` 是管理员
  附加指引，**不得突破证据约束**（提示词明示）。
- **ChatBI 动作**：`resolve_datasource()`（`capability_plan.py`）三级解析
  显式 `datasource_id` → env 缺省 → `(server, tool)` 执行期解析；**强校验**：
  数据源必须存在、启用、`db_type=mcp`、host 与声明的 server 一致（防跨服务
  错绑）。出站参数按 schema 确定性编码后再过一遍
  `validate_against_schema`（`chatbi/controlled.py` 的本地 jsonschema 校验）。

## ⑤ 受控执行

两条出口，都是共享内核薄入口（见 [共享内核API契约.md](../共享内核API契约.md)）：

- **KG**：`knowledge_graph/service.retrieve_evidence(app_state, kb, query,
  user_id, roles, metadata_filter)` —— 复用 kb_search 同款 ACL（身份来自可信
  上下文，不是模型参数）；无权抛 `PermissionError` → 客服转"无权"业务通知。
  检索内部三路详见 [knowledge-graph.md](knowledge-graph.md)。
- **ChatBI**：`chatbi/controlled.execute_controlled(app_state, ds_id, tool,
  params)` —— 完整 jsonschema 验参（禁远程 ref）→ MCP 调用（探测失败的
  服务 fail-closed）→ 信封分态（ok / not_found / forbidden / timeout /
  invalid_input / upstream_error…）。业务态走场景表 `notices` 文案模板
  （`format_map` 缺失槽位兜空）；技术态如实上报不编结果。

## ⑥ 结论生成

`tools/assistant.py: compose_conclusion / compose_answer`（LLM，stage=
`assistant.conclude`）：

- **计量三态**（legacy 模式 `mandatory/turnaround/qualification`，仅存量
  升级路径可达）：decision 枚举随模式（mandatory|not_mandatory|uncertain
  等），提示词在 `legacy_demo.py`。
- **通用 answer 模式**（新场景默认）：answered|uncertain，管理员指引可附加。
- **契约校验 `_validate_conclusion`**：确定结论必须有 evidence，chunk_refs
  必须是有效片段编号（1..N）——引用编号权威源 = chunks 最终序。
- 制品 `kg_search_result`：answer/kb/引用片段/instrument/purpose + 前端
  通用卡渲染。

## 管理面（能力路由页）

- `GET /config/routing`：能力四态 polished（场景表覆盖）/auto（声明合成）/
  incomplete（声明门禁，红色让业务平台补声明）/blocked（无数据源或服务不可用）。
- 编辑抽屉**独立草稿**：取消/关闭/保存失败不夹带入配置；声明参数表
  （类型/必填/来源）只读展示，可编辑的只有话术层（追问文案/提取提示/通知）。
- 行级「重置为最新声明」= 删除该覆盖（唯一的"以声明为准"出口）；全局
  「刷新」只重探测声明，**从不动覆盖**。

## 接入一个新业务平台（零代码）

1. MCP server 起服务（compose 内网即可）。
2. 管理页「集成→MCP 服务」登记地址（server 级人工一步=管理员安全背书）。
3. 「智能问数→数据源」建该 server 的通用源（host=server code）。
4. 声明完整的工具自动进对话路由；要定制话术→能力路由页创建覆盖。

## 数据形态速查（一句话在每步的样子）

「压力表用于贸易结算,需要强制检定吗」→
select: `{"scenario": "mandatory_check"}` →
extract: `{"purpose": "贸易结算", "instrument": "压力表"}`（typed）→
prepare_plan: `{kind: kg, kb: 强检目录, query: "压力表 贸易结算 强制检定 检定周期", conclusion: mandatory}` →
retrieve_evidence: `{chunks: [{chunkId, docId, text, score}…]}`（已收敛/父块回带）→
compose: `{decision: mandatory, conclusion, evidence: [{text, chunk_refs: [1]}]}` →
制品 `kg_search_result`（answer+sources.chunks+cited_chunks）。

「查一下 EQ-0001 设备状态」→
select: `{"scenario": "equipment::query_equipment_status"}`（自动路由 key）→
extract: `{"eq_code": "EQ-0001"}` →
prepare_plan: `{kind: chatbi, server: equipment, tool: query_equipment_status, params: {eq_code: "EQ-0001"}}` →
execute_controlled 信封: `{status: ok, data: {columns, rows}}` → 制品 `metrology_table`。
