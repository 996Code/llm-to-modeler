# bid-demo — 南京院智能客服竞标演示

一个窗口走完"条件选库问答 + 业务平台查询"两类演示:强检判定(追问用途→
按用途选资料库→带依据三态结论)与检测进度查询(mock 业务平台,拒权/查无/
超时如实区分)。

## 组成

| 件 | 位置 | 说明 |
|---|---|---|
| 计量假 server | `../mcp-e2e/metrology_server.py` | 端口 9771;query_progress:BJ2026/BJ2027 正常、BJ8888 拒权、BJ9000 查无、BJ0000 超时;query_instrument:24 条器具价目(检测费/强检目录/检校周期/技术规程,夹具 `../mcp-e2e/metrology_instruments.json`,名称模糊匹配+型号过滤) |
| 客服插件 | `backend/src/domains/metrology_assistant/` | 薄编排临时件:槽位提取→追问→选库→KG 证据/ChatBI 受控执行→组结论 |
| KG 证据服务 | `backend/src/domains/knowledge_graph/service.py` | 显式库+范围→hybrid_retrieve→仅返回依据 |
| ChatBI 受控执行 | `backend/src/domains/chatbi/controlled.py` | 先判业务信封再归一(forbidden 不装成功行) |
| 演示文档 | 本目录两个 `*-demo.md` | 强检目录条款 + 检定规程(合成资料,不外发当真实依据) |
| 环境准备 | `setup_demo.py` | 幂等:注册 server/建库/传文档/等导入/建数据源 |
| 演示前检查 | `check_env.py` | 只读体检:三端口/.env/MCP 注册/两库导入/数据源,全 ✓ 才开演 |

## 启动顺序

```bash
# 1. 计量假 server
cd backend && .venv/bin/python ../tools/mcp-e2e/metrology_server.py

# 2. 后端(.env 已配 DATABASE_URL/LLM_*)
PYTHONPATH=src .venv/bin/python -m uvicorn main:app --port 18080

# 3. 环境准备(首次)
.venv/bin/python ../tools/bid-demo/setup_demo.py

# 4. 把输出的数据源 ID 配进 .env 并重启后端
#    METROLOGY_PROGRESS_DATASOURCE_ID=<id>

# 5. 前端聊天页打开(URL 加 packs 参数,直进客服插件)
#    http://localhost:13081/?packs=metrology_assistant
```

## 演示口径(如实)

- 资料/订单均为演示测试数据;业务平台是 mock MCP server,真实联调在
  工程化(P1)后进行。价目夹具(metrology_instruments.json)源自院方
  提供的价目表(AI.xlsx 转换清洗),测试用途,不外发当正式收费依据。
- 拒权/超时是模拟样例,演示"如实区分"而不是"全部成功"。
- 单号夹具:BJ2026(检测中)、BJ2027(已完成,换单演示)、BJ8888(他主体)、
  BJ9000(不存在)、BJ0000(触发超时,约 30s 后报错)。

## 逐句演示脚本

见 `doc/private/bid-demo-script.md`(内部资料,不入库)。
