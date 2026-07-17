# LLM Form Modeler

自然语言 → 低码配置生成引擎。通过对话生成符合 njmind 低码平台规范的表单配置 JSON。

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Ant Design Vue |
| 后端 | Python 3.12 + FastAPI + LangGraph |
| LLM | OpenAI 兼容接口（Qwen3 / GPT / 任意兼容模型） |
| 存储 | SQLite（对话历史） |
| 上游 | njmind-modeler（模板 / Schema / 校验） |

---

## 一、整体架构

```
                    ┌─────────────────────────────────────────────┐
                    │              主系统 / 浏览器                   │
                    │                                             │
                    │   独立模式              嵌入模式(IM 聊天窗)    │
                    │   三栏布局              iframe + postMessage  │
                    └────────────────┬────────────────────────────┘
                                     │ HTTP / SSE
                                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Python 后端 (FastAPI :18080)                      │
│                                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │ REST API │  │ SSE 流式     │  │ 对话历史     │  │ MCP Server  │  │
│  │ /api/... │  │ 实时进度推送  │  │ SQLite       │  │ /mcp        │  │
│  └────┬─────┘  └──────┬───────┘  └──────────────┘  └─────────────┘  │
│       │               │                                              │
│       └───────┬───────┘                                              │
│               ▼                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                   LangGraph 工作流引擎                          │  │
│  │                                                                │  │
│  │   CREATE 管线 (6步)              MODIFY 管线 (3步)              │  │
│  │   ┌─────────────────┐            ┌─────────────────┐           │  │
│  │   │ fetch_guide     │            │ fetch_guide     │           │  │
│  │   │ list_assets     │            │ modify (LLM)    │           │  │
│  │   │ parse_fields ★  │            │ validate        │           │  │
│  │   │ fetch_templates │            └─────────────────┘           │  │
│  │   │ generate (LLM)  │                                          │  │
│  │   │ validate        │            ★ 可能触发追问                 │  │
│  │   └─────────────────┘            (需求不清晰时中断)              │  │
│  └────────────────────────────────────────────────────────────────┘  │
│               │               │                                      │
│       ┌───────┴───────┐       └──────────────┐                       │
│       ▼               ▼                      ▼                       │
│  ┌─────────┐   ┌───────────┐   ┌───────────────────────┐            │
│  │ 上游客户端│   │ LLM Client │   │ 上下文压缩器          │            │
│  │ httpx    │   │ OpenAI SDK │   │ 历史压缩 + 熔断器     │            │
│  └────┬────┘   └─────┬─────┘   └───────────────────────┘            │
└───────┼──────────────┼──────────────────────────────────────────────┘
        │              │
        │ HTTP         │ HTTP (OpenAI 兼容)
        ▼              ▼
┌────────────────┐  ┌─────────────────────┐
│ njmind-modeler │  │ LLM 推理服务         │
│ :80            │  │ (LM Studio / 云端)   │
│                │  │                     │
│ /api/mcp/      │  │ POST /v1/chat/      │
│  templates     │  │  completions        │
│  schemas       │  │                     │
│  guides        │  │ Qwen3 / GPT / ...   │
│  forms/validate│  │                     │
│  forms/create  │  └─────────────────────┘
└────────────────┘
```

---

## 二、核心链路：CREATE 管线（6 步）

用户描述表单需求 → 经过 6 个节点 → 输出完整配置 JSON。

```
用户输入："创建一个请假申请表，包含申请人、请假类型、开始日期、结束日期"
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 1: fetch_guide                                                 │
│ GET /api/mcp/guides/guide.json → 获取字段类型对照表 + 关键词索引      │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 2: list_assets                                                 │
│ GET /api/mcp/templates/list-templates → 19 个模板文件名              │
│ GET /api/mcp/schemas/list-schemas   → Schema 文件名（禁止猜测文件名）│
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 3: parse_fields ★ (第一次 LLM 调用)                             │
│                                                                     │
│ 用户消息注入：[对话历史] + [当前需求]                                 │
│ 系统消息：字段类型表 + 关键词映射 + 清晰度判断规则                     │
│                                                                     │
│ LLM 判断需求是否清晰：                                                │
│  ├─ 模糊 → needsClarification=true → 返回追问问题 → 中断管线 ──→ 前端│
│  └─ 清晰 → 解析出 formName + fields[]                                │
│                                                                     │
│ 输出示例：                                                           │
│   formName: "请假申请表"                                             │
│   fields: [                                                          │
│     {fieldTitleText:"申请人", fieldType:7 (USER)},                   │
│     {fieldTitleText:"请假类型", fieldType:4 (SELECT)},               │
│     {fieldTitleText:"开始日期", fieldType:2 (DATE)},                 │
│     {fieldTitleText:"结束日期", fieldType:2 (DATE)},                 │
│   ]                                                                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 4: fetch_templates                                             │
│ 根据 fieldType 获取对应模板：                                         │
│   simple_form.json (表单骨架)                                        │
│   user_field.json   (USER 类型模板)                                  │
│   select_field.json (SELECT 类型模板)                                │
│   date_field.json   (DATE 类型模板)                                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 5: generate (第二次 LLM 调用)                                   │
│                                                                     │
│ 系统消息：组装规则 + 表单模板 + 字段模板                              │
│ 用户消息：[对话历史] + [解析后的字段信息]                              │
│                                                                     │
│ LLM 组装完整 FormConfig JSON（deep copy 模板 + 替换字段）             │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Step 6: validate                                                    │
│ POST /api/mcp/forms/validate?mode=CREATE                             │
│                                                                     │
│ ┌─ pass=true  → 输出最终配置 → END                                   │
│ └─ pass=false → 错误信息反馈给 LLM → 回到 Step 5 重试（最多 3 次）   │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
                    最终 FormConfig JSON
```

### MODIFY 管线（3 步）

已有配置 → 自然语言修改指令 → 修改后配置。

```
用户输入："添加一个请假原因字段"
当前配置：请假申请表 (4 字段)
  │
  ▼
┌──────────────┐     ┌───────────────────┐     ┌──────────────┐
│ fetch_guide  │ ──► │ modify (LLM)      │ ──► │ validate    │
│              │     │ [历史]+[指令]+     │     │ pass→END    │
│              │     │ [当前配置]→修改    │     │ fail→retry  │
└──────────────┘     └───────────────────┘     └──────────────┘
                                                    │
                                                    ▼
                                        修改后配置 (5 字段)
```

---

## 三、多轮对话与上下文压缩

```
                    对话历史 (SQLite)
                    ┌──────────────────────────────┐
                    │ 用户: 创建请假表              │
                    │ 助手: 已生成，4 个字段        │
                    │ 用户: 加一个原因字段          │
                    │ 助手: 已修改，5 个字段        │
                    │ 用户: 把类型改成下拉          │
                    │ 助手: 已修改，5 个字段        │
                    │ ... (越来越长)               │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │  上下文压缩器                 │
                    │                              │
                    │  估算 token 总量              │
                    │  超过模型上限 70%?            │
                    │  ├─ 否 → 直接格式化           │
                    │  └─ 是 → 压缩：              │
                    │       ┌──────────────────┐   │
                    │       │ 旧轮次 → LLM摘要  │   │
                    │       │ 保留最近 3 轮     │   │
                    │       │ 状态补偿：当前配置│   │
                    │       └──────────────────┘   │
                    │       熔断器：连续3次失败停止 │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │  注入到 LLM 的 user message   │
                    │                              │
                    │  【历史摘要】                 │
                    │  用户创建了请假表，修改过字段  │
                    │                              │
                    │  【最近对话】                 │
                    │  用户: 把类型改成下拉         │
                    │  助手: 已修改，5 个字段       │
                    │                              │
                    │  【当前状态】                 │
                    │  当前表单: 请假申请表 (5字段) │
                    └──────────────────────────────┘
```

---

## 四、SSE 实时进度推送

```
后端节点执行                          前端接收
┌─────────────┐
│ fetch_guide │──progress("fetch_guide","正在获取指南...")──┐
└──────┬──────┘                                              │
       ▼                                                     ▼
┌─────────────┐                                     ┌──────────────┐
│ parse_fields│──progress("parse_fields","解析...")─►│ SSE 事件流   │
└──────┬──────┘                                     │              │
       ▼                                            │ event: stage │
┌─────────────┐                                     │ event: stage │
│  generate   │──progress("generate","生成...")────►│ event: stage │
└──────┬──────┘                                     │ ...          │
       ▼                                            │ event: result│
┌─────────────┐                                     │ event: done  │
│  validate   │──progress("validate_pass","通过✓")─►└──────────────┘
└──────┬──────┘                                            │
       ▼                                                   ▼
   最终结果                                           前端实时渲染
                                                   6 步进度条动画
```

---

## 五、嵌入主系统

### 方式 A：iframe 直接嵌入

在主系统页面中直接嵌入 iframe：

```html
<iframe
  src="http://你的部署地址:13080/?embed=true&userId=用户ID"
  style="width: 400px; height: 600px; border: none;"
  allow="clipboard-write"
></iframe>
```

**URL 参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `embed=true` | 是 | 启用嵌入模式（隐藏侧边栏，IM 聊天窗口风格） |
| `userId` | 否 | 用户 ID（用于隔离对话历史），也可通过 Header 传递 |

**URL 示例：**
```
http://192.168.99.22:13080/?embed=true&userId=zhangsan
```

### 方式 B：SDK 嵌入（推荐）

引入 `embed.js`，一行代码创建浮动聊天气泡：

```html
<script src="http://你的部署地址:13080/embed.js"></script>
<script>
  const modeler = new LLMFormModeler({
    baseUrl: 'http://192.168.99.22:13080',
    userId: 'zhangsan',           // 从主系统登录态获取
    position: 'bottom-right',      // 浮动按钮位置
    onConfigGenerated: (config) => {
      // 配置生成成功回调
      console.log('生成的配置:', config)
    },
    onConfigApply: (config) => {
      // 用户点击"应用配置"回调
      // 在这里把 config 发送给主系统的表单设计器
      console.log('用户要应用配置:', config)
    },
    onClose: () => {
      console.log('用户关闭了聊天窗口')
    }
  })
</script>
```

### postMessage 通信协议

```
主系统 (parent)                         iframe (modeler)
      │                                        │
      │                                        │
      │  ◄── MODELER_READY ────────────────────│  iframe 加载完成
      │                                        │
      │  ── MODELER_INIT ─────────────────►    │  主系统传入上下文
      │     {userId, formCode}                 │  （可选）
      │                                        │
      │                                        │
      │  ◄── MODELER_CONFIG_GENERATED ─────────│  配置生成完成
      │     {config: {...}}                    │  （实时通知）
      │                                        │
      │  ◄── MODELER_CONFIG_APPLY ─────────────│  用户点击"应用配置"
      │     {config: {...}}                    │  （主系统接收并写入）
      │                                        │
      │  ◄── MODELER_CLOSE ────────────────────│  用户关闭窗口
      │                                        │
```

### 嵌入效果

```
主系统页面
┌────────────────────────────────────────────────────────────┐
│  ┌──────────────────────────────────────────┐              │
│  │  低码表单设计器（主系统的内容）            │              │
│  │                                          │   ┌──────┐   │
│  │                                          │   │ 💬   │   │ ← 浮动按钮
│  │                                          │   └──────┘   │
│  └──────────────────────────────────────────┘              │
│                                              点击后展开 ↓    │
│                                    ┌───────────────────┐    │
│                                    │ 表单配置助手    ✕ │    │
│                                    ├───────────────────┤    │
│                                    │ 用户: 创建请假表  │    │
│                                    │ 助手: 已生成 ✓    │    │
│                                    │    4 个字段       │    │
│                                    │ [应用配置]        │    │
│                                    ├───────────────────┤    │
│                                    │ [输入框...] [发送]│    │
│                                    └───────────────────┘    │
└────────────────────────────────────────────────────────────┘
```

---

## 六、快速开始

### 环境要求

- Python 3.12+
- Node.js 20+

### 配置

编辑 `.env`：

```env
# 上游 njmind-modeler（模板/Schema/校验的来源）
UPSTREAM_BASE_URL=http://192.168.99.22/njmind-modeler

# LLM 推理服务（OpenAI 兼容接口）
LLM_BASE_URL=http://127.0.0.1:1234/v1
LLM_API_KEY=local-dev-key
LLM_MODEL=qwen/qwen3.6-35b-a3b
LLM_MAX_TOKENS=16384
LLM_TIMEOUT=300

# 服务端口
BACKEND_PORT=18080
FRONTEND_PORT=13080
```

### 启动

```bash
# 后端
cd backend
source venv/bin/activate
uvicorn src.main:app --reload --host 0.0.0.0 --port 18080

# 前端
cd frontend
npm install && npm run dev
```

### 访问

| 地址 | 说明 |
|------|------|
| http://localhost:13080/ | 独立模式（三栏布局） |
| http://localhost:13080/?embed=true | 嵌入模式（IM 聊天窗口） |
| http://localhost:13080/embed.js | 嵌入 SDK |
| http://localhost:18080/docs | API 文档（Swagger） |
| http://localhost:18080/health | 健康检查 |

---

## 七、API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config/generate` | SSE 流式生成表单配置（CREATE 管线） |
| POST | `/api/config/modify` | SSE 流式修改表单配置（MODIFY 管线） |
| POST | `/api/config/validate` | 同步校验配置（代理上游） |
| GET | `/api/skills/templates` | 获取模板列表（代理上游） |
| GET | `/api/skills/guide` | 获取配置指南（代理上游） |
| POST | `/api/conversations` | 创建对话 |
| GET | `/api/conversations` | 对话列表（按 userId） |
| GET | `/api/conversations/:id` | 对话详情（含消息历史） |
| DELETE | `/api/conversations/:id` | 删除对话 |
| GET | `/health` | 健康检查 |

**用户身份传递**：所有请求通过 Header `X-User-Id` 传递用户 ID（无登录系统，由主系统透传）。

---

## 八、Docker 部署

```bash
docker-compose up -d
```

```yaml
# docker-compose.yml
services:
  backend:
    build: ./backend
    ports:
      - "18080:8000"
    env_file: .env

  frontend:
    build: ./frontend
    ports:
      - "13080:80"
    depends_on:
      - backend
```
