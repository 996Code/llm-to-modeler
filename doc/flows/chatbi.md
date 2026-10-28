# 智能问数链路走读（代码级）

> 一句话进去 → 选表 → 生成 SQL 或 MCP 工具调用 → 受控执行（可自愈）→
> 图表与结果卡 → 记忆/反馈回流。链路基线：c374f9c。
> 主文件 `backend/src/domains/chatbi/tools/ask_data.py`（8 步 CompositeTool）。

```
ask_data.execute(state, ctx)   ← 引擎选中(见 engine.md)
  ├ Step1 resolve_datasource   会话绑定 > 首个启用源
  ├ Step2 retrieve_schema      两阶段选表(向量召回→LLM 精筛)+图谱 JOIN 路径
  ├ Step3 think                预思考(选表/聚合/注意事项,可跳过)
  ├ Step4 generate_sql │ _generate_mcp_tool_call
  ├ Step5 execute_sql │ _execute_mcp   (受控执行+自愈回环)
  ├ Step6 check_result         结果异常检查(可追问用户)
  ├ Step7 visualize            图表类型决策+数据注入
  └ Step8 finalize             结果卡制品+持久化四连+会话写回
```

## Step1-2：数据源与选表（自然语言怎么"对上"表）

- 数据源：会话绑定 `datasource_id` > 第一个启用源；无源→追问/报错。
- **语义层是安全边界**：`semantic.load_current_content`（扫描合成：数据库表
  /列 → Model/Column；MCP 数据源由后台任务 `list_tools` → 工具=表、
  inputSchema.properties=列）。没扫描过 → fail-closed。
- **两阶段选表**（`retrieval.retrieve`）：用户问题 embedding（本地 bge-m3,
  stage=`chatbi.embed`）→ 语义层向量召回 top20（阈值 0.35）→ **LLM 精筛**
  （stage=`chatbi.retrieve.refine`，"Schema Linking 专家，宁缺毋滥"）→
  `seed_tables` + `schema_context`（表结构文本）+ 全量允许列。
- **图谱 JOIN 扩展**（`graph_context`）：SchemaGraph 上表间最短路径/
  关系补全 → `join_path_section`（多表问题的 JOIN 提示）。
- 选不出表：表少→列选项追问（table_confirm）；表多→前 4 选项追问。
  上轮表继承（`prev_tables`，多轮"再看下另一张表"场景）。

## Step3-4：生成（LLM 提示词怎么拼）

预思考（stage=`chatbi.think`，temperature 0）：问题+schema+候选表+历史 →
选表/聚合方式/注意事项，作为提示段注入生成。

生成提示词 sections（顺序拼进 user 消息）：**业务规则(Skills) → 参考示例
(fewshot 向量召回) → 预思考 → JOIN 路径 → 指标定义 → 相关记忆 → 对话历史
→ 上轮筛选条件 → 用户问题**。

- **SQL 路径**（数据库源，stage=`chatbi.generate_sql`）：system prompt 约束
  只 SELECT、只用允许列、AS 中文别名。产物过**三层校验**（sql_validator：
  AST 只读/单语句 → 危险函数黑名单 → 列白名单含派生名）。
- **MCP 路径**（db_type=mcp，stage=`chatbi.generate_mcp`）：额外注入
  **【MCP 原始声明】**（注册表快照的完整工具声明 JSON）；约束只能调声明内
  工具/参数。产物 `{"tool": ..., "args": {...}}`（落 `state["sql"]` 键，
  双语义——重放/换图同分支）。

## Step5：受控执行与自愈

**SQL**：`datasources.execute_readonly`——无参数绑定（LLM 生成整条 SQL，
防线=三层校验+只读事务+超时+行数上限）；注入 LIMIT（无顶层 LIMIT 加
max_rows+1，fetchmany 判截断）；PG `READ ONLY` 事务 / MySQL
max_execution_time。错误脱敏（不回显连接串）。

**自愈回环**（`healer.heal_sql`，≤2 轮，熔断器 3 次失败开 60s）：错误
**分类**（表/列不存在、语法、歧义）后只传 `[类别]+hint` 给修正器（安全：
不透传原始 DB 错误防跨库信息泄漏，stage=`chatbi.heal`）→ 重新校验 → 重执行。

**MCP**（`controlled._invoke_mcp_core`，与客服 `execute_controlled` 同内核）：
1. **jsonschema 本地验参**（Draft202012，禁远程 `$ref`；空白必填串=缺失）
   → 不过=invalid_input；
2. `mcp_client.call_tool` → `PackInvokeError` 三分类
   （credential/retryable→unavailable/transport）；
3. **信封分态先于归一**：payload 带 status+载荷键 → 按业务态
   （ok/forbidden/not_found/invalid_input/unavailable/error）；
   ok → 表格归一（columns/rows、dict 列表、单记录、标量——**不猜列名**，
   形状不符=invalid_input）；
4. **自愈门**（`_mcp_can_heal`）：仅 `invalid_input` 且工具声明
   annotations 中性（未声明=放行保持存量能力；`destructiveHint:true`/
   显式非只读=禁）——只修参数，**不换工具不换服务**；修正候选同样过只读门；
   跨查询熔断（持续故障直接"熔断中"不再烧 LLM）。

## Step6-8：检查、图表、结果卡

- **结果检查**（checker，纯统计）：0 行=正常空；过半列全 NULL/疑似笛卡尔
  积=fail → 有建议则再修一轮 → 仍 fail 追问用户（按原样展示/帮我调整）。
- **图表**（stage=`chatbi.chart`）：0 行不烧 LLM；类型决策（kpi/line/pie/
  bar/...只选类型与列映射）→ 程序化注入数据构建 ECharts option；LLM 失败
  → 规则推断降级（degraded 标记）。
- **结果卡制品**：sql/rows_sample(50)/chart/指标命中/表来源证据
  （table_provenance：每表列与中文名）/耗时/自愈轮数/降级标记。前端
  `plugins/chatbi` 注册的 ResultCard 渲染。
- **持久化四连**（全部 fail-open）：记忆提炼（LLM，值得记才存）→ JOIN 表对
  经验（图谱置信度）→ 自动保存查询 → fewshot 例回流向量库（下次生成更准）。
- **会话写回**：prev_sql/prev_tables/prev_chart_config/prev_filters（多轮
  "换个饼图/再加个日期条件"的底座；switch_chart 工具重放 prev_sql 换图）。
- **反馈回路**：结果卡点赞/点踩 → 表对 ±、表置信度 ±（FOR UPDATE 原地写）
  → 影响后续 JOIN 路径排序；per-user 幂等。

## LLM 调用一览（排查看 call_logs 的 stage）

`route_pack / route_tool`（引擎）→ `chatbi.embed`（向量）→
`chatbi.retrieve.refine`（选表）→ `chatbi.think` →
`chatbi.generate_sql | generate_mcp` → `chatbi.heal | heal_mcp`（自愈）→
`chatbi.chart` → `chatbi.memory.extract`（记忆）。
