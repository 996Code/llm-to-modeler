# LLM Form Modeler

**LLM 驱动的多插件智能助手引擎** — LangGraph StateGraph 编排意图识别、工具执行与追问恢复，自然语言驱动多种业务能力（表单配置、知识图谱问答、智能问数、计量智能客服等），Engine 层零领域知识。

> 📖 **新入手必读**：[doc/README.md](doc/README.md) —— 全项目文档总目录（书的目录指南）。
> 🔧 **技术细节**：[doc/technical-reference.md](doc/technical-reference.md) —— 架构大图 / LangGraph 流程 / 插件协议与全能力表 / 环境变量全集 / 目录树。
> `AGENTS.md` —— AI agent / 开发者操作手册入口（规约/测试/发布/部署索引）。

## 界面一览

> 以下截图来自 99.22 演示环境线上实测（2026-10-31），更多截图见
> [doc/test-evidence/](doc/test-evidence/)。

**管理端概览** —— 会话/调用/检索大盘，插件启停热切换：

![管理端概览](doc/test-evidence/2026-10-31-9922-full-test/01-admin-overview.png)

**智能客服 · 能力路由** —— MCP 工具声明自动接入 + 人工润色，18 项能力按服务分组：

![能力路由](doc/test-evidence/2026-10-31-9922-full-test/02-capability-routing.png)

**知识问答 · 强检判定** —— 缺条件先追问，结论带知识库片段引用与引用实体：

![强检判定证据卡](doc/test-evidence/2026-10-31-9922-full-test/06-mandatory-evidence.png)

**业务查询 · 器具价目** —— "查一下肺活量测试仪的测量范围"直达 MCP 业务平台，结构化结果卡：

![价目查询](doc/test-evidence/2026-10-31-9922-full-test/07-price-measure-range.png)

**业务查询 · 检测时限 / 委托单进度** —— 检测工作日承诺与进度三态（正常/拒权/查无）：

![检测工作日](doc/test-evidence/2026-10-31-9922-full-test/08-turnaround.png)

![委托单进度](doc/test-evidence/2026-10-31-9922-full-test/13-progress-normal.png)

**知识库管理** —— 库树/文档导入/图谱浏览/检索测试/数据权限，任务中心断点续跑：

![知识库管理页](doc/test-evidence/2026-10-31-9922-full-test/18-kb-page.png)

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Ant Design Vue + Pinia + AntV G6（图谱可视化） |
| 后端 | Python 3.12 + FastAPI + LangGraph StateGraph |
| LLM | OpenAI 兼容接口（Qwen3 / GPT / 任意兼容模型；embedding 同协议） |
| 存储 | PostgreSQL（对话/审计/任务/checkpoint + 向量 pgvector，必填）+ Neo4j（图谱，可选） |
| 上游 | AssetClient 抽象（HTTP 适配；地址请求级解析：宿主 services 表按请求下发） |

## 核心架构：三层六边形

```
前端(Vue3) ── 独立模式(三栏) / 嵌入模式(IM 聊天窗+SDK)
   │ HTTP / SSE
Engine(LangGraph StateGraph) ── classify → execute → clarify_gate → result
   │  零领域知识（领域词零出现）
SDK(协议 + 存储设施) ── Tool/Registry/PackRouter + 图/向量/文档解析（scope 隔离）
   │
Domain Packs ── 领域知识全部在此；新插件 = 建目录 + pack.py + tools/，自动发现零配置
   │
Adapters(HttpAssetClient) → 上游业务 API / LLM 推理服务
```

- **Engine 零领域知识**（试金石：`grep -rE "formCode|leaveType|请假|kb_id|chatbi|njmind" backend/src/engine/` 必须为空）
- **SDK 存储下沉**：图/向量/文档解析 + 命名空间隔离（前缀登记 fail-fast + scope 签发），插件三行代码接入全套设施
- **完整大图与逐层说明**：[技术参考 §一](doc/technical-reference.md)

## 核心机制

| 机制 | 要点 |
|---|---|
| 两级路由 | 引擎按 manifest 选 pack（单包直通零 LLM）；pack 内选工具，领域规则写在 pack router，不泄漏进引擎 |
| 追问恢复 | LangGraph 原生 `interrupt()` + `Command(resume)`，checkpoint 持久化（PG），断点恢复零额外代码；缺信息必追问不填默认值 |
| SSE 实时进度 | `graph.stream()` 每 chunk 实时桥接 SSE（不等完成）；后端 15s 心跳 + 前端 60s 看门狗 |
| 动态能力上报 | 兜底工具经 `ctx.registry` 动态枚举全部工具能力，新增插件自动可见零改动 |
| 插件热启停 | 管理端开关热生效（单实例单 worker 契约）；`PACKS_ENABLED` 为部署上限，状态持久化 |
| 嵌入契约 | 信封协议 READY/INIT/GET_CONTEXT/APPLY/AUTH_UPDATE…（mind-designer 已接入）；请求头全链路透传 |
| 后台任务框架 | queue_key 串行 / 块级断点续跑 / 协作取消 / SSE 事件流，任务中心实时可视 |
| 检索与兜底 | 混合检索（图谱+向量+全文）带证据链；智能客服业务查无→知识库链路兜底，拒答须提示需人工确认 |

## 插件一览

| 插件 | 能力 |
|---|---|
| njmind_form | 自然语言 → 表单配置：创建（6 步）/ 增量修改（指令集主路径 ~4s）/ 图片识别 / 克隆 / 查询 |
| knowledge_graph | 文档导入 → 图谱+向量双写；混合检索问答带片段引用；实体消歧；检索 ACL（全能力形态示范） |
| chatbi | NL2SQL+图表：语义层/SQL 自愈/三层校验/记忆/反馈闭环（全能力形态） |
| metrology_assistant | 计量智能客服：场景表（DB 覆盖∪MCP 声明自动合成）路由，价目/检测时限/资质/强检判定/进度查询 |
| njmind_bpm / njmind_list | 审批流 / 列表页配置（上游走 njmind-modeler 纯计算端点） |
| leave_application | 请假申请（演示域，多 pack 一级路由示例） |

各插件完整能力表（导入流水线/检索细节/管线步骤…）见 [技术参考 §三](doc/technical-reference.md)。

## 快速开始

**环境要求**：Python 3.12+、Node.js 20+。完整环境变量说明见 `.env.example` 与
[技术参考 §六](doc/technical-reference.md)。

```env
# .env 关键项（必填）
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=qwen-max
HOST_PORT=19090                 # 应用入口 http://IP:19090/ai-modeler/
NEO4J_PASSWORD=change-me        # compose 内 Neo4j（图谱插件依赖）
# 可选：ADMIN_TOKEN（公网部署必须配置，否则管理端开放访问）
```

```bash
# 后端
cd backend && source venv/bin/activate
uvicorn src.main:app --reload --host 0.0.0.0 --port 18080

# 前端
cd frontend && npm install && npm run dev
```

| 地址 | 说明 |
|------|------|
| http://localhost:13080/ai-modeler/ | 独立模式（三栏布局） |
| http://localhost:13080/ai-modeler/?embed=true | 嵌入模式（宿主 iframe） |
| http://localhost:13080/ai-modeler/embed.js | 嵌入 SDK |
| http://localhost:18080/docs | API 文档（Swagger） |
| http://localhost:18080/health | 健康检查（`/api/health` 为嵌入探测别名） |

Docker 一键部署：`cp .env.example .env && vim .env && ./deploy.sh`（详见
[doc/agents/deploy-99-22.md](doc/agents/deploy-99-22.md)）。数据落盘 `./data/`
（PG 含向量表 / 上传文件 / Neo4j / embedding 模型跨版本复用）。

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config/chat` | **统一对话入口**（SSE 流式，走 LangGraph） |
| GET/POST/DELETE | `/api/conversations*` | 会话列表/详情/创建/删除（`?contextKey=x&latest=true` 恢复绑定会话） |
| POST | `/api/auth/token` | 统一认证：口令换 24h token |
| GET | `/api/meta/packs` | pack manifest（前端渲染 actions/示例/展示字段） |
| GET | `/health` `/api/health` | 健康检查（`Cache-Control: no-store`） |
| GET/DELETE | `/api/admin/conversations*` | 管理端：全量会话/链路追踪/删除（需管理口令） |
| GET | `/api/admin/call-logs` | 管理端：LLM/上游/检索调用审计 |
| GET/POST | `/api/admin/packs*` | 管理端：插件清单/启停/设置/依赖重检（热生效） |
| GET | `/api/tasks*` | 任务中心：列表/日志/取消/SSE 事件流 |
| * | `/api/packs/{name}/*` | 插件自有 API（启停随插件热挂卸） |

**ChatRequest**：`{"message", "conversation_id", "answers"?, "image_base64"?}` —
`answers` 非空走追问恢复；请求头自动透传到上游（嵌入模式）。

## 管理端

`/ai-modeler/admin.html`（独立入口，不经嵌入 SDK）。概览/会话/调用日志/任务中心/
插件（启停/设置/自定义管理页）/插件专属页（知识库、智能客服、智能问数…）。

- **鉴权**：未配 `ADMIN_TOKEN` = 开放访问（仅限内网，启动日志有警告）；配置后经
  `auth.html` 门禁换 24h token，全部 API 带 Bearer；签名密钥随重启轮换
- **链路追踪**：会话视图按轮聚合事件流与调用日志（LLM 完整 prompt/上游全文/检索
  召回明细），统一时间线
- **插件热启停**：开关即时生效不重启；不允许禁用最后一个启用的插件

## 深入阅读

| 文档 | 内容 |
|---|---|
| [doc/technical-reference.md](doc/technical-reference.md) | 架构大图 / LangGraph 流程与 GraphState / Tool 协议 / 插件全能力表 / 环境变量全集 / 目录树 / 设计亮点 |
| [doc/README.md](doc/README.md) | 全项目文档总目录 |
| [doc/插件开发与嵌入指南.md](doc/插件开发与嵌入指南.md) | 从零写一个插件 + 嵌入契约协议全文 |
| [backend/src/domains/ARCHITECTURE.md](backend/src/domains/ARCHITECTURE.md) | pack 全部钩子 + SDK 存储设施契约 |
| [doc/嵌入模式总体设计.md](doc/嵌入模式总体设计.md) | 嵌入模式总体设计 |
| [doc/agents/deploy-99-22.md](doc/agents/deploy-99-22.md) | 部署手册（守卫/内存红线/排查） |
| [doc/test-evidence/](doc/test-evidence/) | 界面截图库（按日期归档，线上实测） |
