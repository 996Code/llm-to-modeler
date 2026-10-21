# MCP 双向接入设计

> 状态：设计定稿（2026-10-21）。一期实施待排期。
> 源码参照：Dify v1.17.1（~/cyble-code/dify）、RAGFlow v0.27.2（~/cyble-code/ragflow-v0272）。

## 1. 愿景

两个方向、一扇门：

- **出**：把平台的检索/领域能力以 MCP 标准暴露，Claude Desktop、Qoder 等外部 AI 工具和 njmind 平台模块可以直接当工具调用（例：外部 AI 调 `kb_search` 查"GB 50017 的焊缝要求"）。
- **进**：插件（首发 ChatBI 报表）把外部 MCP server 当**数据源/能力源**用（例：出报表时数据从外部 MCP server 拉回，用户问"上个月 GMV"，ask_data 发现数据源是 mcp 类型 → 调外部工具拿数 → 照常出图表）。

**引擎核心零改动**——两半都不碰 LangGraph 引擎和工具选择路由。

## 2. 调研结论存档（为什么这么设计）

| 决策 | 来源 | 证据 |
|---|---|---|
| 无状态 Streamable HTTP（每条消息独立处理，不存会话） | Dify | api/controllers/mcp/mcp.py:59；core/mcp/server/streamable_http.py:113 |
| 工具快照入库，运行时零实时发现 | Dify | models/tools.py:323；services/tools/mcp_tools_manage_service.py:388 |
| 每次调用新建短连接（ExitStack 统一管理 transport+session） | Dify | core/mcp/mcp_client.py:51,108-115 |
| 传输协商：URL 以 /mcp、/sse 结尾直选，否则 SSE 回退 streamable | Dify | core/mcp/mcp_client.py:66-89 |
| 双超时按 server 持久化（timeout=30s / read_timeout=300s） | Dify | models/tools.py:334-335 |
| 网络 I/O 永远在 DB 事务外（三段式：读→探测→写） | Dify | mcp_tools_manage_service.py:637 |
| 创建即探测、失败不阻塞（进 pending 态） | Dify | controllers/console/workspace/tool_providers.py:1378-1503 |
| 结果转换：text 先试 json.loads，structuredContent 优先 | Dify | core/tools/mcp_tool/tool.py:84-112 |
| 检索工具描述动态内嵌资源清单（一次 list_tools 知道有哪些库） | RAGFlow | mcp/server/server.py:549-556 |
| 检索返回三段式：chunks + 分页 + 回显参数 | RAGFlow | mcp/server/server.py:328-345 |
| 官方 mcp SDK（两家都不用 fastmcp） | 两者 | dify vendor 官方 SDK；ragflow pyproject `mcp>=1.28.1,<2.0.0` |

本项目残留依赖 `mcp>=1.28,<1.29`（backend/requirements.txt）直接启用，不新增依赖。

## 3. 第一部分：对外 MCP Server（一期）

### 3.1 端点与传输

- 官方 `mcp` SDK 的 FastMCP，`stateless_http=True`，挂载 `/api/mcp`。
- AuthMiddleware 对 `/api/mcp` 前缀放行，模块内自设 key 门（复刻 `/api/open/` 既有模式，见 domains/knowledge_graph/open_api.py）。

### 3.2 工具面：声明制自动生成

- pack 的 `config.yaml` 新增 `mcp_tools:` 列表，声明哪些工具对外暴露；**默认不暴露**（fail-closed，与 Tool 基类设计原则一致）。
- 转换器：内部 Tool（name/description/input_schema，sdk/tool.py:245）→ MCP 工具，一次转换零手写。pack 声明后新工具自动出现在 MCP 面。
- `kb_search` 的 description 动态拼上知识库清单（id+名称）——外部 AI 一次 tools/list 即知有哪些库。
- 检索类返回三段式：结果 + 分页信息 + 回显参数。

### 3.3 鉴权：开放 key 体系上提

- KG 插件内的 key 签发/校验（SHA-256 落库、吊销、last_used_at 审计）上提到 `services/open_keys.py` 平台级服务；表加 `scope` 字段（`kg_push` / `mcp`），一条 SQL 迁移旧数据；KG open_api 改调公共服务。
- 管理端"开放接口"一页统一管理（key 加用途标记）；新增 MCP 端点信息展示（URL + 接入示例文案）。

### 3.4 错误处理

- 工具执行异常 → MCP 协议错误如实返回（不静默降级）；单工具超时保护。
- 不做限流、SSRF 防护（内网部署 + admin 门控签发，无此场景）。

## 4. 第二部分：接入外部 MCP = 插件内消费（一期交付地基，二期 ChatBI 试点）

### 4.1 定位

外部 MCP 不进引擎路由（无"虚拟 pack"）。它是**插件管线里的数据源**：引擎照旧选中内部工具（如 ask_data）→ 工具内部发现数据源是 mcp 类型 → 调外部 server → 结构化数据回插件管线。

### 4.2 分层：底层全部下沉平台层

```
平台层（新增）
  services/mcp_registry.py    mcp_servers 表 CRUD + 管理 API（admin 门）
  sdk/mcp_client.py           连接/协议设施：list_tools(code)、call_tool(code, tool, args) -> dict
插件层（零 MCP 感知）
  domains/chatbi              数据源类型 mcp：只存引用（server_code + tool_name + 参数映射），
                              import SDK 两个函数
```

插件对底层无感：连接管理、传输协商、超时、结果转换全在平台层；将来协议升级插件零改动。

### 4.3 数据模型 `mcp_servers`

| 字段 | 说明 |
|---|---|
| `id` | 主键 uuid |
| `name` | 展示名 |
| `code` | 引用标识，`^[a-z0-9_-]{1,24}$`，唯一；插件配置引用它 |
| `url` / `url_hash` | URL 明文 + sha256 唯一索引（**不做加密**——内网 + admin 门控） |
| `headers` | 认证头 JSON 明文（**不做加密**） |
| `timeout` / `read_timeout` | 双超时，默认 30 / 300 秒 |
| `status` | `connected` / `pending` |
| `last_error` | 最近探测错误（Dify 缺此字段，管理员排障瞎——我们补上） |
| `tools_snapshot` | list_tools 结果 JSON 原样落库（name/description/inputSchema/outputSchema），**整存整取，不做工具级开关**——粒度控制留给引用方（插件配置里选哪个工具） |
| `created_at` / `updated_at` | 时间戳 |

### 4.4 生命周期（网络 I/O 永远在 DB 事务外）

- **创建即探测**：落库（短事务）→ 事务外连 server 拉 list_tools → 回写快照 + status=connected；失败不阻塞，status=pending + last_error。
- **刷新快照**：管理端手动按钮 + 二次确认（"下游引用可能悬空"）。
- **删除引用拦截**：删除前查"被 N 个 ChatBI 数据源引用"，有则拒删（Dify 悬空引用运行期才爆是缺陷，我们补上）。
- **编辑**：headers 不回显明文，留空 = 不变。

### 4.5 SDK 设施 `sdk/mcp_client.py`

```
list_tools(code) -> [工具清单]        # 读快照，零网络
call_tool(code, tool, args) -> dict   # 每次调用新建连接
```

内部实现（全部 Dify 验证过）：
- 每调用新建连接：transport + session 压入同一 ExitStack，用完即毁，无连接池。
- 传输协商：URL 以 /mcp、/sse 结尾直选，否则 SSE 失败回退 streamable HTTP（先关旧栈防双活）。
- 双超时透传 httpx。
- 结果转换：structuredContent 优先；text 先试 json.loads；统一吐 dict。
- 错误：连不上/超时 → `ToolError(retryable)` 带 server 名，走引擎既有错误分流，如实报错不静默降级。

### 4.6 ChatBI 试点（二期）

- `chatbi_data_sources` 加类型 `mcp`：存 `server_code + tool_name + 参数映射`，不存连接信息。
- ask_data 数据获取步骤识别 mcp 类型 → 走 SDK 设施 → 结构化数据照常进图表/汇总管线。
- 健康巡检：mcp 类型 = 探测 server（复用现有巡检框架）。

## 5. 明确不做清单（防跑偏）

- OAuth 全家桶（RFC9728 发现 / PKCE / 动态注册 / token 自动刷新）
- 任何加密（Fernet/RSA）——内网 + admin 门控
- SSRF 代理
- `{{request.headers.X}}` 身份透传占位符
- 多租户 / 限流
- 虚拟 pack（外部工具进引擎路由）——按需求澄清砍掉
- 独立 sidecar 进程部署——主进程内挂载

## 6. 分期与出口标准

**一期**：对外 MCP server（§3 全部）+ mcp_servers 注册表 + sdk/mcp_client.py + 管理端 MCP 注册页。
出口标准：
1. Claude Desktop / 任意 MCP client 用签发 key 连 `/api/mcp`，tools/list 可见 kb_search（描述含知识库清单），检索往返成功；
2. 无 key / 吊销 key 访问 → 401；
3. 管理端可注册外部 server，快照入库可见，坏 URL 进 pending + last_error；
4. 全量 pytest 通过。

**二期**：ChatBI mcp 数据源全链路。
出口标准：
1. 管理端建 mcp 类型数据源（引用平台 server）；
2. ask_data 对 mcp 数据源完成一次取数出图；
3. 外部 server 停机时对话如实报错（不静默降级）；
4. 全量 pytest 通过。

## 7. 测试策略

- 对外：官方 mcp SDK 的 client 打真实 HTTP 全链路（initialize / tools/list / tools/call、401/403、kb_search 主路径+边界）。
- 对内：本地起 mock MCP server 测 SDK 设施（连接、超时、传输协商、结果转换、错误分流）。
- 交付前全量 pytest（~1810 例）。

## 8. 部署影响

主进程带出，无新进程、无新容器；99.22 / 3.110 随版本部署。前端仅管理端新增页签（平台层页面，非插件页）。
