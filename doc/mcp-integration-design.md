# MCP 双向接入设计

> 状态：设计定稿（2026-10-21，简化版）。
> 源码参照：Dify v1.17.1（~/cyble-code/dify）、RAGFlow v0.27.2（~/cyble-code/ragflow-v0272）。

## 1. 愿景

- **出**：把平台的检索/领域能力以 MCP 标准暴露，Claude Desktop、Qoder 等外部 AI 工具直接当工具调用。
- **进**：插件（首发 ChatBI 报表）把外部 MCP server 当**数据源**用——出报表时数据从外部 MCP 拉，管线照常出图。

**引擎核心零改动**；两半的底层（协议、连接、鉴权）全部下沉平台层，插件只调两个函数。

## 2. 第一部分：对外 MCP Server

- 官方 `mcp` SDK（low-level `Server` + `StreamableHTTPSessionManager(stateless=True, json_response=True)`，RAGFlow 同款无状态模式）挂载 `/api/mcp`（依赖 `mcp>=1.28,<1.29` 已在 requirements）。对外访问统一走项目前缀网关：`http://<host>/ai-modeler/api/mcp`（网关剥前缀转后端，与前端/开放 API 同约定；管理页面板按当前 origin 动态展示）。
- **鉴权：全局单 key**。platform_settings 配一个 `mcp_api_key`（管理端设置页可改），请求头 `X-Api-Key` 校验，不匹配 401。不搞 key 表/签发/吊销——内网够用。
- **工具面：声明制自动生成**。pack 的 `config.yaml` 加 `mcp_tools:` 列表声明对外暴露哪些工具（默认不暴露）。转换器把内部 Tool（name/description/input_schema）自动转成 MCP 工具，零手写。
- `kb_search` 描述里动态拼上知识库清单（外部 AI 一次 tools/list 就知道有哪些库）。
- 工具异常如实返回 MCP 协议错误，不做静默降级。

## 3. 第二部分：接入外部 MCP（插件内消费）

### 3.1 定位

外部 MCP 不进引擎路由。它是**插件管线里的数据源**：ask_data 发现数据源是 mcp 类型 → 调外部工具拿数 → 照常出图。

### 3.2 平台层设施（新增两处）

```
services/mcp_registry.py    mcp_servers 表 CRUD + 管理端 API（admin 门控）
sdk/mcp_client.py           list_tools(code) / call_tool(code, tool, args) -> dict
```

连接管理、传输协商、超时、结果转换全在平台层；插件 import 两个函数，MCP 协议零感知。

### 3.3 数据模型 `mcp_servers`

| 字段 | 说明 |
|---|---|
| `id` / `name` / `code` | 主键 / 展示名 / 引用标识（`^[a-z0-9_-]{1,24}$` 唯一，插件引用它） |
| `url` / `headers` | URL 明文 / 认证头 JSON 明文（内网，不加密） |
| `timeout` / `read_timeout` | 双超时，默认 30 / 300 秒 |
| `status` | `connected` / `pending` |
| `tools_snapshot` | list_tools 结果 JSON 原样落库（对话链路零实时发现） |
| `created_at` / `updated_at` | 时间戳 |

### 3.4 行为约定

- **注册即探测**：保存时连一次 server 拉 list_tools 回填快照；连不上照样保存，status=pending（管理端再试）。
- **快照手动刷新**：管理端按钮重拉覆盖。
- **连接策略**（Dify 验证过的套路）：每次调用临时建连接用完即毁；URL 以 /mcp、/sse 结尾直选传输，否则 SSE 失败回退 streamable HTTP；structuredContent 优先、text 先试 json.loads，统一返回 dict；连不上/超时抛 `ToolError(retryable)` 走引擎既有错误分流。

### 3.5 ChatBI 试点（二期）

- `chatbi_data_sources` 加类型 `mcp`：存 `server_code + tool_name + 参数映射`。
- ask_data 数据获取步骤识别 mcp 类型 → 走 sdk/mcp_client → 数据照常进图表管线。
- 健康巡检：mcp 类型 = 探测 server。

## 4. 明确不做清单（防跑偏）

- OAuth、任何加密（Fernet/RSA）、SSRF 代理、限流、多租户
- key 签发/吊销体系（全局单 key 即可）
- 虚拟 pack（外部工具进引擎路由）
- 独立 sidecar 进程（主进程内挂载）
- 删除引用拦截、快照刷新二次确认（二期有引用方时再评估）

## 5. 分期与出口标准

**一期**：对外 MCP server + mcp_servers 注册表 + sdk/mcp_client.py + 管理端「MCP 服务」页（统一登记处：注册表 CRUD + 对外接入 key；插件消费时引用，如 ChatBI 数据源选 server）。
出口标准：
1. MCP client 用 key 连 `/api/mcp`：tools/list 可见 kb_search（描述含知识库清单），检索往返成功；
2. 错 key → 401；
3. 管理端注册外部 server，快照可见，坏 URL 进 pending；
4. 全量 pytest 通过。

**二期**：ChatBI mcp 数据源全链路。
出口标准：
1. 管理端建 mcp 类型数据源；
2. ask_data 对 mcp 数据源取数出图成功；
3. 外部 server 停机时对话如实报错；
4. 全量 pytest 通过。

## 6. 测试与部署

- 测试：官方 mcp SDK client 打真实 HTTP 测对外端点（含 401/边界）；本地 mock MCP server 测 sdk/mcp_client；交付前全量 pytest。
- 部署：主进程带出，无新进程/容器；99.22 / 3.110 随版本走。
