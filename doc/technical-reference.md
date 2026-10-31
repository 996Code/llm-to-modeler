# 技术参考（README 的深度展开）

> README 精简为首屏门面后，本文件承接全部技术细节：架构大图、LangGraph 流程与
> GraphState、Tool 协议、各插件完整能力表、环境变量全集、目录树、设计亮点。
> 只描述当前行为；上手请先读 [README](../README.md) 与 [doc/README.md](README.md)。

## 一、核心架构：三层六边形

```
┌─────────────────────────────────────────────────────────────────────┐
│                         前端 (Vue 3)                                │
│                                                                     │
│   独立模式 (三栏布局)          嵌入模式 (IM 聊天窗 + SDK)            │
│   StandaloneLayout             EmbeddedLayout + embed.js            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP / SSE
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│            Python 后端 (FastAPI;dev :18080 / 容器内 nginx 反代)      │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  API 层 (api/)                                               │   │
│  │  /api/config/chat  /api/conversations  /health             │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ Engine 层 (engine/) — 零领域知识                          │   │
│  │                                                              │   │
│  │  ┌────────────────────────────────────────────────────────┐  │   │
│  │  │  LangGraph StateGraph                                  │  │   │
│  │  │                                                        │  │   │
│  │  │  classify_intent ──→ route_by_tool ──→ execute_tool    │  │   │
│  │  │        │                                  │            │  │   │
│  │  │        │                            interrupt?         │  │   │
│  │  │        │                           ↙        ↘          │  │   │
│  │  │        │                     挂起追问    正常完成       │  │   │
│  │  │        │                        │           │          │  │   │
│  │  │        │                   Command(resume)  │          │  │   │
│  │  │        │                        │           ▼          │  │   │
│  │  │        │                        └──→ execute_tool      │  │   │
│  │  │        │                             (重跑工具)         │  │   │
│  │  │        │                                  │            │  │   │
│  │  │        │                                  ▼            │  │   │
│  │  │        └──────────────────────→ handle_result ──→ END  │  │   │
│  │  │                                                        │  │   │
│  │  │  Checkpoint: PostgresSaver (thread_id = conv_id)     │  │   │
│  │  └────────────────────────────────────────────────────────┘  │   │
│  │                                                              │   │
│  │  辅助模块:                                                   │   │
│  │  ├── stream.py      graph.stream → SSE 桥接 (实时 chunk)    │   │
│  │  ├── conversation.py 多轮对话管理 (append-only 事件流)      │   │
│  │  ├── compression.py  上下文压缩 (70% 阈值 + 熔断器)         │   │
│  │  ├── prompt_loader.py Jinja2 模板加载 (缓存 + 覆写/追加)    │   │
│  │  ├── log_config.py   日志配置 (loguru: 控制台 + 轮转文件)   │   │
│  │  └── logging_filter.py 日志脱敏过滤器                       │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ SDK 层 (sdk/) — 协议定义 + 通用存储设施                   │   │
│  │                                                              │   │
│  │  ├── tool.py        Tool / CompositeTool / ToolResult        │   │
│  │  │                  ToolContext / AskSpec / AskQuestion       │   │
│  │  ├── registry.py    ToolRegistry (自动发现 + 注册)           │   │
│  │  ├── pack_router.py PackRouter 协议 (一级/二级路由)          │   │
│  │  ├── pack_api.py    插件 HTTP API 鉴权/身份辅助              │   │
│  │  ├── asset_client.py AssetClient ABC (submit/query)          │   │
│  │  ├── sanitize.py    Unicode 隐写清洗                         │   │
│  │  ├── llm_compat.py  LLM 响应形状归一(自插件上提 SDK)         │   │
│  │  ├── observability.py 观测常量+log_pack_call 通用打点        │   │
│  │  │                                                            │   │
│  │  ├── scope_registry.py  ★ 存储命名空间隔离(前缀登记+scope签发)│   │
│  │  ├── graph_store.py     ★ Neo4j 图存储(GraphStore Protocol)  │   │
│  │  ├── vector_store.py    ★ 向量存储工厂(VectorStore Protocol)│   │
│  │  └── doc_parser.py      ★ md/txt/pdf/docx 解析+切块          │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  ★ Domain Packs (domains/) — 领域知识全部在此                │   │
│  │  （新插件只需: 建目录 + pack.py + Tool 子类 → 自动发现上线）  │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                              │                                       │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  Adapters (adapters/) — HttpAssetClient HTTP 上游适配         │   │
│  │  （地址:宿主 services 表按请求下发,见 resolve_base 请求级解析）│   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
┌────────────────┐   ┌─────────────────────┐
│ 上游业务 API   │   │ LLM 推理服务         │
│ (提交/查询数据)│   │ (OpenAI 兼容接口)    │
└────────────────┘   └─────────────────────┘
```

### 架构试金石

```bash
# Engine 层不能包含任何领域知识(领域标识/插件名零出现)
grep -rE "formCode|leaveType|请假|kb_id|chatbi|njmind" backend/src/engine/
# → 必须返回空
# SDK 层零领域知识(存储设施不认识 LLM/本体/知识库)
grep -rn "kg_" backend/src/sdk/graph_store.py backend/src/sdk/vector_store.py
# → 仅注释命中(存量数据兼容说明)
```

## 二、LangGraph StateGraph 核心流程

### 2.1 图结构

```python
START → classify_intent (LLM 选工具, 从 registry 动态生成 prompt)
  │
  ├─ route_by_tool ──→ execute_tool (执行工具, 支持 interrupt)
  │                         │
  │                    ToolResult.ask?
  │                    ├─ 是 → interrupt() 挂起 → SSE needsClarification
  │                    │       前端发 answers → Command(resume=answers)
  │                    │       → execute_tool 重跑 (带 clarify_answers)
  │                    └─ 否 → handle_result → END
  │
  └─ route_after_result ──→ rerun (追问恢复后重跑) / done (结束)
```

### 2.2 路由范围与意图确认

- 请求声明 `packs` 时,插件选择、工具选择、兜底与最终执行均受该范围约束；
  声明的插件全部不可用时返回错误,不扩大到全局插件。
- 工具归属在插件装配时登记。插件路由只能返回本插件工具；执行与断点恢复
  再次校验归属,不以模型输出或用户确认替代范围校验。
- 工具兜底只采用插件明确声明的 `domain.fallback_tool`,不取全局首个工具。
  无匹配直接说明当前范围无法处理；合法候选低置信度才请求确认,展示处理方向。
  确认仅对当前轮有效,且只接受明确同意。
- 知识库插件直接进入 `kb_search`,问题理解由知识检索管线负责；向量检索不变。
- 首次回答、追问恢复和继续生成统一消费结果信封,数据制品与引用卡一致保留。

### 2.3 追问恢复机制（LangGraph 原生 interrupt）

```
用户: "帮我创建一个请假申请表"
  │ ▼ classify_intent → 选中 create_form
  │ ▼ execute_tool → CreateFormTool.execute()
  ├─ _step_parse_fields: LLM 提取字段 → 部分字段不明确 → _need_clarify 标记
  ├─ ToolResult(ask=AskSpec(...))
  └─ interrupt({questions, summary}) → 图挂起
     │ ▼ SSE → 前端渲染追问卡片
     │ 用户回答: {请假类型: "年假/事假/病假", 日期范围: "开始-结束"}
     ▼ Command(resume=answers) → 图从断点恢复
       ├─ interrupt() 返回 answers → 注入 tool_state["clarify_answers"]
       ├─ 清除 _need_clarify / _clarify_spec 标记
       └─ 重跑 execute_tool → 字段完整 ✓ → … → ToolResult(artifact) → END
```

### 2.4 GraphState 定义

```python
class GraphState(TypedDict, total=False):
    # 输入
    user_input: str
    conversation_history: list[dict]
    compressed_history: str
    conversation_id: str
    forward_headers: dict          # 嵌入模式透传的请求头
    context_artifact: dict | None  # 上下文制品 (pack 判断画布状态 + modify 增量基线)

    # 意图识别 (两级路由产出)
    tool_name: str                 # 选中的工具名 (pack 二级路由决定)
    intent_reason: str             # 路由理由 (含 pack 信息, 可观测)

    # 工具执行
    tool_state: dict               # 工具内部 state (透传, Engine 不读)
    tool_result: dict | None       # 工具执行结果

    # 追问 (LangGraph interrupt)
    pending_questions: list[dict]
    clarify_answers: dict          # resume 值

    # SSE 事件收集
    sse_events: list[dict]
```

## 三、插件系统

### 3.1 目录与自动发现

```
domains/
├── njmind_form/          ← 表单配置插件
│   ├── pack.py           ← create_registry() 注册工具
│   ├── tools/            ← create(6步)/modify(3步)/get/clone/image/chat
│   └── prompts/          ← Jinja2 模板
├── knowledge_graph/      ← 知识图谱插件(全能力形态示例)
│   ├── pack.py           ← 四钩子齐备: registry/api_router/tasks/unload
│   ├── config.yaml       ← manifest: 依赖声明+管理页+设置页
│   ├── api.py / tasks.py / store.py / retrieval.py / probes.py …
├── chatbi/               ← 智能问数(NL2SQL+图表,全能力形态)
├── metrology_assistant/  ← 计量智能客服(场景表驱动薄编排)
├── njmind_bpm/ njmind_list/ leave_application/ …
└── (新插件只需创建目录 + pack.py + tools/ → 自动发现, 零配置上线)
```

**pack.py 可选钩子**（全部由平台在装配/卸载期调用）:

| 钩子 | 必须 | 作用 |
|------|------|------|
| `create_registry()` | ✓ | 返回本插件 ToolRegistry |
| `create_api_router()` | 可选 | 插件自有 HTTP API,挂 `/api/packs/{name}` 前缀,启停随插件热挂卸 |
| `register_tasks(mgr, app_state)` | 可选 | 注册后台任务 handler(任务中心可见/可取消) |
| `unload()` | 可选 | 释放连接/注销前缀等清理 |

manifest（config.yaml）还可声明: `dependencies`(基础设施依赖+探针,未配置 fail-closed
不可启用)、`admin.settings`(声明式设置页)、`admin.page`(自定义管理页组件)。

### 3.2 Tool 协议

```python
class Tool(ABC):
    name: str                    # 工具名 (LLM 选择时看到)
    description: str             # 工具说明
    when: str                    # "何时用" 描述

    # 语义自检 (Engine 在 execute 前调用, 失败则跳过 execute 回流给 LLM)
    def validate_input(self, state: dict) -> str | None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult

    # 钩子方法 (Engine 通过钩子操作制品, 不直接读内部结构)
    def format_result(self, artifact: dict) -> dict     # SSE 前端字段
    def summarize_artifact(self, artifact: dict) -> str # 压缩器状态补偿
    def title_for(self, artifact: dict) -> str          # 对话列表标题

class CompositeTool(Tool):
    steps: list[str] = []        # 管线步骤名
    pipeline_steps: list[dict]   # 前端展示用

    def run_pipeline(self, state, ctx):
        for step in self.steps:
            if state.get("_need_clarify"): break
            getattr(self, f"_step_{step}")(state, ctx)
```

### 3.3 ToolResult 三态

```python
class ToolResult:
    artifact: dict | None        # 制品 (config/data)
    artifact_type: str           # "config" | "data"
    reply: str | None            # 闲聊回复
    ask: AskSpec | None          # 追问 (非空 → interrupt)
    summary: str                 # 摘要 (进对话历史)
    error_for_llm: str | None    # 错误 (回流给 LLM)
    extra: dict                  # 扩展数据
```

### 3.4 动态能力上报

兜底 ChatTool 通过 `ctx.registry.all()` 动态枚举全部工具的能力描述生成系统
prompt——新增插件后"我能做什么"自动更新，无需修改 Engine 或 ChatTool 代码。

### 3.5 通用后台任务框架（任务中心）

插件经 `register_tasks(mgr)` 注册 handler 后即可 `mgr.submit(...)`:

- **queue_key 串行**:同 key 任务 FIFO 串行（如 `queue_key=kb_id` → 同库导入不并发写图）
- **生命周期**:pending → running → succeeded/failed/cancelled;协作式取消
- **可观测**:进度/消息推送、请求级结构化日志、SSE 事件流、管理端任务中心实时视图
- **持久化**:任务与日志落 PG,重启后可查历史

## 四、各插件完整能力表

### 4.1 njmind_form — 表单配置

| 工具 | 类型 | 管线 | 说明 |
|------|------|------|------|
| create_form | CompositeTool | 6步 | 自然语言 → 完整表单配置 |
| modify_form | CompositeTool | 3步·两相式 | 增量指令集为主（~4s），全量重生成兜底（~33s，升格后本会话锁定） |
| get_form / clone_form / image_form / chat | Tool | - | 查询 / 复制 / 图片识别(多模态) / 兜底闲聊 |

**CREATE 管线**：`fetch_guide → list_assets → parse_fields(LLM) → fetch_templates → generate(LLM) → validate`

**MODIFY 管线**：`fetch_guide → modify → validate（差分校验）`；增量主路径
`build_catalog → plan_ops(LLM 只输出指令集) → apply_ops(确定性合并)`，锚点失败带清单
重试≤2次；未提及字段经 `restore_untouched` 从基线逐字节还原（改 A 不动 B）。

### 4.2 leave_application — 请假申请（示例插件）

`submit_leave`(3步: parse_info→validate_rules→submit) / `query_status`。
信息不足走 `ToolResult.ask` 声明式追问，不填默认值。作为多 pack 一级路由演示域。

### 4.3 knowledge_graph — 知识图谱 + 向量检索（全能力插件）

自然语言驱动的知识库：上传文档 → LLM 抽取实体/关系入 Neo4j、切块向量化入 PG(pgvector)
→ 对话里直接命中（`kb_search`），答案带三元组证据与原文引用。

| 组成 | 说明 |
|------|------|
| 导入流水线（后台任务） | 解析→切块→批内并行 LLM 抽取→本体约束→图+向量双写;**断点续跑**（块级 checkpoint）、连续失败熔断、进度心跳;收尾生成**摘要块**（D14:LLM 概括首尾节选 200 字,seq=-1 入索引——"主要讲什么"式抽象问法零召回兜底,重导幂等,失败降级不阻断） |
| 实体消歧（别名合并） | 同一实体跨块不同写法不再裂成多节点:抽取词表播种既有实体+别名,收尾按"别名指认+类型一致"合并碎片;大库名录先类型分桶+编辑距离预筛,仅可疑对送 LLM。零猜测:正文没指认的永不并。诛仙实测 563 实体收敛至 397 |
| 长文档导入 | 全局 LLM 限速（RPM/TPM 双令牌桶 + 429 全局退避）;`extraction_model` 可单独给导入换更快的模型;失败自动续跑（5 分钟退避,预算跨续跑链封顶,重启不再卡 retry_scheduled 中间态）;滑窗 ETA 预估 |
| 混合检索问答 | 意图解析→图谱子图 + 向量双路召回→LLM 组织答案（带证据链）;**父子回带**（向量子段命中后回带所属整章前后各 ~2000 字窗口,句读对齐,单块 5000 字/总预算 24000 字护栏;定位失败降级子段原文不静默丢内容）;**奇偶重排**（D15:lost-in-middle 缓解,高分占头尾,纯顺序变换默认开）;**图谱零命中时全文兜底**（tsvector+GIN,2-gram,默认开,命中标 `source='fulltext'`）;embedding 未配置时自动降级纯图谱 |
| 块命中统计（D29） | 回答中被引用的块 `hit_count`+1,管理端"N 次引用"列——治理数据;失败静默 |
| 检索可观测 | 三路库调用入 `call_logs`（graph/vector/fulltext）:逐词种子命中、子图召回量+截断水位、向量逐条相似度、全文兜底明细 |
| 插件 HTTP API | 知识库树 CRUD/文档上传/导入/图谱查询/增量展开/检索问答/检索测试/建库向导（分段配置+实时预览,与导入同一条切块代码路径）/块编辑（改文本仅重嵌该块向量）/分段重配;`GET /dependency-status` 探针 |
| 检索数据权限（嵌入模式） | 每库可配置允许检索的**用户/角色**;不配置=全开放。生效条件:宿主传入身份（`X-User-Id`）或角色（chat 走 `pack_params`,REST 走 body `roles`）才过滤;显式指定无权库=403 诚实拒绝,跨库自动过滤。管理端各页不受限 |
| 管理页 | 左右分栏:左侧库树（文件夹/子库层级,建/移/删）+ 右侧四子页——文档导入 / G6 图谱浏览（单击详情/双击展开邻居/悬停高亮一阶邻域）/ 本体设置 / 检索测试 |
| 依赖门控 | manifest 声明 neo4j/pg_vector 依赖与探针;未配置 → 插件中心"依赖未配置"不可启用,补配后热加载 |

**升级注意**:首次启动建全文索引对存量 kg_chunks 全表重写（一次性,迁移已放开 600s
超时）;存量文档的全文索引按旧文本计算——重点 PDF 建议重导入。

### 4.4 chatbi — 智能问数（NL2SQL + 图表，全能力插件）

| 组成 | 说明 |
|------|------|
| 主工具 | `ask_data`（8 步管线）/ `switch_chart`（换图表类型） |
| 插件 HTTP API | 数据源 CRUD/扫描/语义层版本/图谱浏览/记忆/指标/健康面板;`POST /datasources/{id}/rebuild-index`;`POST /feedback`（点赞/点踩→置信度微调,默认关） |
| 后台任务 | scan_datasource / refresh_semantics / memory.consolidate / rebuild_index |
| 管理页 | 数据源管理/语义层/图谱浏览 |
| 依赖门控 | pg_vector 可选（未配置降级全表清单） |

图表卡数据来源折叠区含表 schema 卡/JOIN 路径/执行 SQL,与引用编号构成可信度双件套。

### 4.5 metrology_assistant — 计量智能客服

场景表（DB 覆盖∪MCP 声明自动合成）驱动的薄编排：两段式提取（选能力→按声明提参数）
→ 缺槽追问（令牌防串答）→ 受控执行/知识库检索 → 带引用结论。能力路由页按 MCP 服务
分组;声明质量门禁（描述<6 字不进路由标红）。业务查无→知识库链路兜底（cited 非空才
顶业务通知）;拒答明确提示需人工确认。MCP 业务平台（价目/时限/资质/进度/预约/证书/
报告/设备…）声明即接入。

### 4.6 njmind_bpm / njmind_list — 流程与列表配置

BPM 审批流配置（`generate_process`）与列表页配置（`generate_list`）,上游走
njmind-modeler Java 侧纯计算端点（parse/render 零持久化,落库走 designer 原保存流程）。

## 五、SSE 实时进度

```
后端 (graph.stream)                    前端
┌──────────────┐
│ classify_    │──stage("正在理解您的意图...")──→  🔄 正在理解...
│ intent       │                                      │
└──────┬───────┘                                      ▼
       ▼                                        ┌──────────┐
┌──────────────┐                                 │ 进度条   │
│ execute_tool │──pipeline_definition──→         │ 动画     │
│              │  [{step: "解析字段"}, ...]       │          │
│  _step_x     │──stage("解析中...")──→          │ ✓ 解析   │
│              │                                  │ ○ 生成   │
└──────────────┘                                  └──────────┘
```

- `graph.stream()` 是同步 API，在线程池中执行；chunk 经 `call_soon_threadsafe` 实时推 SSE
- 传输保障：后端 15s `: ping` 心跳；`Cache-Control: no-cache, no-transform`（禁止中间
  代理压缩攒流）；前端 60s 空闲看门狗断流报错

## 六、嵌入主系统

### 方式 A：SDK 嵌入（推荐）

```html
<script src="http://你的部署:19090/ai-modeler/embed.js"></script>
<script>
  const assistant = new LLMFormModeler({
    baseUrl: 'http://你的部署:19090',
    userId: 'zhangsan',
    position: 'bottom-right',
    onConfigGenerated: (config) => { /* ... */ },
    onConfigApply: (config) => { /* 写入主系统设计器 */ },
  })
</script>
```

### 方式 B：iframe 嵌入

```html
<iframe src="http://你的部署:19090/ai-modeler/?embed=true"
        style="width: 400px; height: 600px; border: none;"></iframe>
```

### 生产形态：嵌入契约协议（mind-designer 已接入）

信封协议：`READY / INIT / GET_CONTEXT / APPLY / AUTH_UPDATE / GET_AUTH / CLOSE /
RESIZE`，带 requestId 关联与 capabilities 能力协商，配置读写走宿主前端、渲染画布不落库。
完整协议见 `doc/嵌入模式总体设计.md`。

### 请求头透传

嵌入模式下主系统请求头（X-User-Id / X-Tenant-Id / Authorization…）经
`forward_headers` 全链路透传到上游 API，零侵入身份传递。

## 七、环境变量全集

```env
# ── LLM 推理服务 (必填;OpenAI 兼容接口) ──
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=qwen-max
LLM_MAX_TOKENS=4096

# ── Docker 部署(compose 变量) ──
HOST_PORT=19090                # 应用入口 http://IP:19090/ai-modeler/
NEO4J_PASSWORD=change-me       # compose 内 Neo4j 密码(必填)
APP_MEM_LIMIT=8g               # 应用容器内存护栏

# ── 知识图谱插件（可选；不配则插件中心显示"依赖未配置"不可启用） ──
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j

# ── 向量存储（pgvector 单后端;Milvus 已下线移除） ──
# VECTOR_BACKEND=pg            # 默认且唯一;走 DATABASE_URL 同库,建表自动
#                              # CREATE EXTENSION vector(目标 PG 需装 pgvector)
LLM_EMBED_MODEL=bge-m3         # 默认本地 ONNX(零 API 费用)
EMBEDDING_BACKEND=local        # local(默认)/api(云端 /v1/embeddings)

# ── 管理端口令（可选加固；不配=开放访问，公网部署必须配置） ──
# ADMIN_TOKEN=

# ── 检索增强（可选；默认全关，管理端插件设置里也有开关） ──
# RERANK_BASE_URL=             # 外置 rerank API(Cohere/Jina 协议)
# RERANK_MODEL=                # 两变量都配才生效
# RERANK_API_KEY=
# RERANK_TIMEOUT=3             # 超时降级原召回序不阻断
# EMBED_QUERY_PREFIX=0         # 仅 bge-*-zh-v1.5 系需要;bge-m3 勿开
# EMBED_CACHE_MAX=4096         # 嵌入缓存 LRU(TTL 24h)
# LLM_CACHE_ENABLED=0          # chat 结果缓存(对话场景慎开)
```

健康检查：根路径 `GET /health`（运维/K8s 探针）与 `GET /api/health`（嵌入代理链
别名）双端点。

### 上游地址请求级解析（重要）

上游地址**只来自宿主 services 表**：designer 握手后经 chat 请求下发，按 pack 在
manifest 里声明的服务名解析。未下发的服务调用时抛 `ServiceUnresolvableError`
（fail-closed）。工具侧 `preflight` 前置校验（SDK 钩子）缺地址在进管线前就拦截，
不烧 LLM/上游调用。同一请求内所有上游调用（含缓存 key）绑定同一 base；每轮实际
地址记录在 `request_context` 打点（管理端可查）。

## 八、管理端细节

### 会话链路追踪

会话列表点"查看"进入（`GET /api/admin/conversations/{id}/trace`）：全量事件流与
LLM/上游调用日志按时间合并成统一时间线，**按轮分组**，每轮聚合墙钟耗时/LLM 次数与
耗时/上游次数与耗时。覆盖环节：

| 环节 | 来源 |
|------|------|
| 意图路由决策 | 引擎自动 trace 打点 + LLM 调用明细 |
| LLM 调用（完整 prompt/响应、token、stage） | `call_logs`（`LLM_LOG_FULL=0` 可退摘要） |
| 上游 API（请求/响应全文、状态码） | `call_logs`（会话归属线程上下文自动绑定） |
| 图谱/向量/全文检索（种子命中/召回量/相似度） | `call_logs`（call_type=graph/vector/fulltext） |
| 工具执行（工具名/耗时/结论） | 引擎自动 trace |
| 插件内部业务步骤 | pack 调 `ctx.trace()` |
| 后台任务 / 历史压缩 / 快照追问现场 | 任务日志 + compacted 事件 + checkpoint/ask |

### 插件热启停契约

- 部署契约是**单实例、单 Uvicorn worker**；不支持多副本/多 worker 热切换
- 开关**热生效**：重新 `nodes.configure` 注入 + 替换 `app.state` 引用，graph 拓扑不变
- 状态持久化在 `PACK_STATE_PATH`（默认 `data/pack_state.json`）
- `PACKS_ENABLED` env 是**部署上限**：名单外一律不启用不显示；名单内管理端自由启停
- 所有插件默认可选（含 ChatBI）；单插件异常整包隔离不留半个插件；仅部署方显式设置
  `PACKS_CRITICAL` 时名单内启用插件才 fail-fast
- 保护：不允许禁用最后一个启用的插件

### 鉴权边界

- `ADMIN_TOKEN` 未配置 → 开放模式（启动日志醒目警告）；配置后校验失败 → 401
- 签名密钥随进程重启随机轮换——重启后旧 token 全部失效
- 普通会话接口 `X-User-Id: admin` 的跨用户查看同样要求合法管理口令——只报用户名
  没有口令时 "admin" 就是普通用户

## 九、目录结构

```
llm-to-modler/
├── backend/src/
│   ├── main.py               # FastAPI 入口
│   ├── engine/               # ★ Engine 层(零领域知识)
│   │   ├── graph.py / graph_state.py / nodes.py
│   │   ├── stream.py / sse.py / conversation.py / compression.py
│   │   ├── prompt_loader.py / state_keys.py / log_config.py / logging_filter.py
│   ├── sdk/                  # ★ SDK 层(协议+存储设施)
│   │   ├── tool.py / registry.py / pack_router.py / pack_api.py
│   │   ├── asset_client.py / sanitize.py / llm_compat.py / observability.py
│   │   ├── catalog.py / env_config.py
│   │   ├── scope_registry.py / graph_store.py / vector_store.py
│   │   ├── pg_vector_store.py / relational_store.py / doc_parser.py
│   ├── domains/              # ★ Domain Packs(领域知识全部在此)
│   │   ├── njmind_form/      #   表单配置
│   │   ├── knowledge_graph/  #   知识图谱(全能力形态)
│   │   ├── chatbi/           #   智能问数(全能力形态)
│   │   ├── metrology_assistant/ # 计量智能客服
│   │   ├── njmind_bpm/ njmind_list/ leave_application/
│   ├── adapters/             # HttpAssetClient
│   ├── api/                  # config/conversations/admin/tasks/meta/settings/health/auth
│   ├── llm/                  # client / local_embeddings / embed_cache / rate_limit
│   └── services/             # db / stores / task_manager / pack_* / call_context
├── frontend/src/
│   ├── components/chat/      # ChatPanel/ChatInput/KgGraphCard/BiChartCard/TurnTraceModal
│   ├── admin/                # ★ 管理端(独立多页入口)
│   │   └── packPages/        # 插件自定义管理页(knowledge_graph/chatbi/metrology…)
│   ├── layouts/ stores/ services/ composables/
├── doc/                      # 文档总目录见 doc/README.md
├── tools/                    # mcp-e2e / bid-demo
├── deploy/ + deploy.sh
└── .env / README.md
```

## 十、设计亮点

1. **Engine 零领域知识** — 新业务能力 = 新增一个插件目录
2. **LangGraph 原生 interrupt/resume** — checkpoint 自动持久化，断点恢复零额外代码
3. **动态能力上报** — 新插件后"我能做什么"自动更新
4. **声明式追问** — 缺信息必追问不填默认值，`AskSpec` 声明式定义
5. **SSE 实时流式** — chunk 级实时推送 + 心跳/看门狗
6. **请求头全链路透传** — 零侵入身份传递
7. **两级路由** — 领域规则在 pack router 不泄漏进引擎；路由数据铁律 + 语义自检是真正防线
8. **插件即平台公民** — 依赖门控/设置页/管理页/自有 API/后台任务一体（kg 首创，chatbi 复用）

## 十一、Docker 部署细节

前后端合并一体容器（nginx 伺服静态/反代 + uvicorn 跑 API）+ Neo4j；向量存 PG
（pgvector 与平台同库同容器）。

```bash
cp .env.example .env && vim .env   # 注意:DATABASE_URL 保持注释(compose 自动拼装)
./deploy.sh                        # 拉码→预构建→打镜像→compose 切换→健康验证
./deploy.sh --status | --logs | --down | --rollback
```

- 数据落盘：`./data/postgres`（PG 数据含向量表）、`./data/app`（上传文件）、
  `./data/neo4j`、`./data/models/embedding`（bge-m3 ~543MB 跨版本复用）
- 镜像构建在 docker run 容器内预构建（前端 dist/后端 .deps/nginx deb），
  `docker build` 只做纯 COPY，规避构建容器 DNS 坑
- **存量升级（SQLite→PG，一次性）**：见 `backend/scripts/migrate_sqlite_to_pg.py`
  脚本头部 runbook（幂等可重跑）
- **存量升级（Milvus→PG 向量）**：迁移脚本已随 Milvus 下线移除，曾用部署直接
  重新导入文档/重扫语义层即可
