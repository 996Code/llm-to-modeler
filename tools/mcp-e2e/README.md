# mcp-e2e — MCP 双向端到端验证

一键验证 MCP 双向闭环：**注册**（外部 server 进注册表）→ **消费**（ChatBI 报表从外部 MCP 拉数出图）→ **对外**（外部 AI 工具连本平台查知识库）。

## 前置

- 后端 dev server 在跑（默认 `http://localhost:18080`）
- 前端 dev server 在跑（默认 `http://localhost:13081`，前缀网关链路）
- 管理端已配置对外 MCP key（或用 `--api-key` 传入当前值）
- 真实 LLM 网关可用（ask_data 步走真实大模型）

## 用法

```bash
cd backend && .venv/bin/python ../tools/mcp-e2e/run_e2e.py \
    --base http://localhost:18080 \
    --frontend-origin http://localhost:13081 \
    --api-key <管理端配置的 MCP key>
```

## 覆盖矩阵（12 步）

| 方向 | 步骤 |
|---|---|
| 进 | 起 3 个异构 fake server（streamable 报表数据/SSE 传输/X-Api-Key 鉴权）→ 注册即探测（connected+快照）→ 坏 URL 不阻塞（pending） |
| 进·消费 | 建 mcp 数据源 → 扫描合成语义层（工具=表）→ **ask_data 全链路**（真实 LLM 生成工具调用 → MCP 拿数 → 图表） |
| 出 | 官方 mcp client 经前缀网关连 `/ai-modeler/api/mcp` → tools/list + kb_search 调用 |
| 错误路径 | server 宕机 → 单源健康探测如实报不健康（`check_all_health` 需连续 3 败才自动禁用，故测单源端点） |
| 收尾 | 清理全部测试数据（不碰既有数据源/server 注册） |

## 排障

ask_data 步失败时查调用日志取证（LLM 实际收到/返回了什么）：

```sql
SELECT stage, request_data FROM call_logs ORDER BY created_at DESC LIMIT 5;
```

常见失败：
- **检索追问（无法匹配到相关表）**：扫描后看 `scan_stage` 是否"完成: N 个工具"；N=0 检查 fake server 是否可达；N>0 仍追问则查语义层向量索引（`build_index` 日志）。
- **工具调用 JSON 解析失败**：LLM 输出不稳，重跑一次；持续失败看 call_logs 里 generate 步的 prompt 与响应。

## 文件

- `fake_servers.py` — 四个异构 fake MCP server（low-level SDK 手搭，模拟真实接入方）：报表数据/SSE 传输/X-Api-Key 鉴权/计量业务平台（`build_metrology_server`，南京院客服 Demo 用）
- `run_e2e.py` — 主流程（每步 ✓/✗ + 证据，退出码非 0 on fail）
- `demo_server.py` — 常驻演示 MCP（固定端口 9770，模拟"外部订单系统"，供管理端/对话页演示）
- `metrology_server.py` — 常驻计量业务平台 MCP（固定端口 9771，南京院客服 Demo）：query_progress 进度（拒权/查无/超时样例）、query_instrument 价目（24 条夹具，名称模糊匹配）、query_appointment 预约（date 槽位链路）、query_certificate 证书（enum 白名单链路）、query_report 报告进度（拒权分态 + 通用表格卡链路）
