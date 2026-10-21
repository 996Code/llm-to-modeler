# MCP 二期实施计划 —— ChatBI 消费外部 MCP 数据源 + 双向测试项目

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ChatBI 报表把外部 MCP server 当数据源：建 mcp 类型数据源 → 扫描合成语义层（工具=表）→ ask_data 生成工具调用 → 调 MCP 拿数 → 照常出图；另交付 `tools/mcp-e2e` 双向测试项目。

**Architecture:** 复用 ChatBI 全管线，只加两个分支：generate 步产出工具调用 JSON（mcp 变体 prompt）、execute 步走 `sdk.mcp_client.call_tool` 并映射成 `ExecuteResult`（columns+rows 形状不变，check_result/visualize/finalize/switch_chart 零改动）。**隔离性铁律：chatbi 只 import `sdk.mcp_client`，零 services import**（server 配置经注册表+loader 注入，一期已就绪）。

**Tech Stack:** 既有栈；无新依赖。mcp 数据源契约：`chatbi_data_sources` 行 `db_type="mcp"`，`host`=server code，`database`=tool_name（空=全部工具），`port`=0，`username`/`encrypted_password`=空串（DDL NOT NULL 兼容）。

**Spec:** `doc/mcp-integration-design.md` §3.5（出口标准四条：管理端建 mcp 数据源/ask_data 取数出图/外部停机如实报错/全量绿）

## Global Constraints

- chatbi 包内禁止 `import services.*`（隔离性；MCP 调用只走 `sdk.mcp_client.call_tool/list_tools`）。
- 不改引擎、不改 sdk/mcp_client 公开签名；`ExecuteResult` 形状（columns: list[str], rows: list[tuple]）是图表管线的契约，mcp 分支必须映射成同形。
- fail-closed：mcp 数据源未扫描（无语义层）→ 沿用现有"请先扫描"澄清；工具调用 JSON 解析失败/工具不在白名单 → 拒绝执行；外部 server 失败 → 如实报错（PackInvokeError 文案直达用户），不静默降级。
- 测试跑法：`cd backend && .venv/bin/python -m pytest <file> -v`（TEST_DATABASE_URL 已配）；fake 外部 MCP server 复用 tests/test_mcp_integration.py 的 uvicorn 线程模式。
- **chatbi 内对 sdk.mcp_client 的引用一律用 `from sdk import mcp_client` 模块引用形式**（调用时 `mcp_client.list_tools(...)`）——函数内 `from sdk.mcp_client import list_tools` 的名字绑定会让测试 monkeypatch 失效。
- 前端 UI 惯例：antd 常规控件、图标按钮+tooltip、不新开顶级页。

---

### Task 1: 数据源 mcp 类型（模型/CRUD/API）

**Files:**
- Modify: `backend/src/domains/chatbi/datasources.py`（create/update 白名单 + 密码空串兼容）
- Modify: `backend/src/domains/chatbi/models.py`（DataSourceInfo docstring 补 mcp 映射）
- Modify: `backend/src/domains/chatbi/api.py`（DatasourceCreate 校验）
- Test: `backend/tests/domains/test_chatbi_mcp_datasource.py`（新建）

**Interfaces:**
- Produces: `db_type="mcp"` 的数据源行可经 `create_datasource/get/list/update/resolve_datasource` 全链路存取；`host`=server code、`database`=tool_name（可空）、密码空串不加密直存。

- [ ] **Step 1: 失败测试**

```python
# backend/tests/domains/test_chatbi_mcp_datasource.py
"""mcp 类型数据源:CRUD 全链路 + 密码空串兼容 + resolve 可选中。"""
import pytest
from domains.chatbi import datasources as ds_mod
from domains.chatbi.datasources import (
    create_datasource, get_datasource, list_datasources, resolve_datasource,
)


@pytest.fixture()
def db():
    from domains.chatbi.models import CHATBI_DDL
    from sdk.relational_store import PackRelationalDB
    store = PackRelationalDB("chatbi")
    with store.connect() as conn:
        conn.execute(CHATBI_DDL)
    yield store
    with store.connect() as conn:
        conn.execute("DELETE FROM chatbi_data_sources")


def test_create_and_get_mcp(db):
    row = create_datasource(db, name="外部报表", db_type="mcp",
                            host="srv_a", port=0, database="",
                            username="", password="")
    info = get_datasource(db, row["id"], decrypt=True)
    assert info.db_type == "mcp" and info.host == "srv_a"
    assert info.password_plain == ""


def test_mcp_requires_server_code(db):
    with pytest.raises(ValueError):
        create_datasource(db, name="坏", db_type="mcp", host="",
                          port=0, database="", username="", password="")


def test_resolve_picks_mcp(db):
    row = create_datasource(db, name="外部报表", db_type="mcp",
                            host="srv_a", port=0, database="",
                            username="", password="")
    info = resolve_datasource(db, None)
    assert info is not None and info.db_type == "mcp"
```

（fixture 细节以 tests/domains/ 既有 chatbi 测试的实际建库方式为准适配——grep `PackRelationalDB` 参照。）

- [ ] **Step 2: 红** — `pytest tests/domains/test_chatbi_mcp_datasource.py -v` FAIL（白名单拒绝 mcp）
- [ ] **Step 3: 实现** — `datasources.py` `create_datasource` 白名单 `("mysql","postgresql")` → `("mysql","postgresql","mcp")`；mcp 时校验 `host`（=server code）非空；`password` 空串跳过 `encrypt_password` 直存空串（非 mcp 保持原逻辑）；`update_datasource` 同步白名单。`models.py` DataSourceInfo docstring 补 mcp 字段映射说明。`api.py` DatasourceCreate 的校验文案同步（mcp 时 host 必填、port/database/username/password 可空——pydantic 字段有默认值即可）。
- [ ] **Step 4: 绿 + 回归** — 新测试 + `pytest tests/domains/ -k chatbi -q` 无回归
- [ ] **Step 5: Commit** — `feat(chatbi-mcp): 数据源 mcp 类型——CRUD/密码空串/server code 必填`

---

### Task 2: 健康巡检 mcp 分支

**Files:**
- Modify: `backend/src/domains/chatbi/datasources.py`（check_health）
- Test: `backend/tests/domains/test_chatbi_mcp_health.py`（新建）

**Interfaces:**
- Consumes: `sdk.mcp_client.list_tools(code) -> list[dict]`（探测；失败抛 PackInvokeError）。
- Produces: `check_health(info)` 对 `db_type=="mcp"`：调 `list_tools(info.host)`，成功→健康，异常→不健康（错误文案带 server code）。

- [ ] **Step 1: 失败测试**

```python
# backend/tests/domains/test_chatbi_mcp_health.py
"""mcp 数据源健康巡检:list_tools 探测,失败如实报不健康。"""
from types import SimpleNamespace
from domains.chatbi.datasources import DataSourceInfo, check_health


def _mcp_info(code="srv_a"):
    return DataSourceInfo(id="x", name="外部", db_type="mcp",
                          host=code, port=0, database="",
                          username="", password_plain="")


def test_healthy(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "list_tools",
                        lambda code: [{"name": "echo"}])
    ok, err = check_health(_mcp_info())
    assert ok and err == ""


def test_unhealthy(monkeypatch):
    from sdk.tool import PackInvokeError
    from sdk import mcp_client
    def boom(code):
        raise PackInvokeError("MCP server 连接失败(srv_a): timeout",
                              PackInvokeError.RETRYABLE)
    monkeypatch.setattr(mcp_client, "list_tools", boom)
    ok, err = check_health(_mcp_info())
    assert not ok and "srv_a" in err
```

（check_health 现签名以 datasources.py:309-337 为准——返回形状可能是 (bool, str) 或别的，测试按实际适配。）

- [ ] **Step 2: 红** — FAIL（"不支持的数据库类型: mcp"）
- [ ] **Step 3: 实现** — `check_health` 加分支：`if info.db_type == "mcp": from sdk.mcp_client import list_tools; try: list_tools(info.host); return 健康 except Exception as e: return 不健康(str(e))`。注意 import 放函数内（chatbi 惰性 import 惯例）。
- [ ] **Step 4: 绿** — 2 PASS
- [ ] **Step 5: Commit** — `feat(chatbi-mcp): 健康巡检 mcp 分支——list_tools 探测`

---

### Task 3: 扫描合成语义层（工具=表/参数=列）

**Files:**
- Modify: `backend/src/domains/chatbi/semantic.py`（新增 `synthesize_mcp_content`）
- Modify: `backend/src/domains/chatbi/tasks.py`（`_task_scan_datasource` mcp 分支）
- Test: `backend/tests/domains/test_chatbi_mcp_scan.py`（新建）

**Interfaces:**
- Consumes: `sdk.mcp_client.list_tools(code) -> [{"name","description","inputSchema"}]`；`Model/Column/SemanticModelContent`（models.py）。
- Produces: `synthesize_mcp_content(tools: list[dict]) -> SemanticModelContent`——每个工具一个 Model：`name`=工具名、`display_name`=工具名、`description`=工具 description、`columns`=inputSchema.properties 逐项（name=参数名、display_name=参数名、type 映射 string/integer→数值文本按 Column 实际字段适配）；`sample_questions` 留空。扫描任务 mcp 分支：list_tools →（`info.database` 非空时只保留该工具）→ synthesize → `save_content` 落库（跳过 LLM 富化/指标推断——设计从简，标注 source 可沿用默认）。

- [ ] **Step 1: 失败测试**

```python
# backend/tests/domains/test_chatbi_mcp_scan.py
"""mcp 扫描:工具清单合成语义层(工具=表/参数=列),落 chatbi_semantic_models。"""
from domains.chatbi.semantic import synthesize_mcp_content

TOOLS = [
    {"name": "query_orders", "description": "查询订单数据",
     "inputSchema": {"type": "object",
                     "properties": {"city": {"type": "string"},
                                    "month": {"type": "string"}},
                     "required": ["city"]}},
    {"name": "query_gmv", "description": "查询 GMV",
     "inputSchema": {"type": "object", "properties": {}}},
]


def test_synthesize():
    content = synthesize_mcp_content(TOOLS)
    assert [m.name for m in content.models] == ["query_orders", "query_gmv"]
    cols = {c.name for c in content.models[0].columns}
    assert cols == {"city", "month"}
    assert content.models[0].description == "查询订单数据"


def test_synthesize_empty():
    assert synthesize_mcp_content([]).models == []
```

扫描任务分支的测试：monkeypatch `list_tools` 返回 TOOLS → 调扫描入口（以 tasks.py 实际可测入口为准，若任务函数难直调则测 semantic 层+手动验证记录在 report）→ 断言 `load_current_content(db, ds_id)` 返回合成内容。

- [ ] **Step 2: 红** — FAIL（synthesize_mcp_content 不存在）
- [ ] **Step 3: 实现** — semantic.py 新函数（Column 字段以 models.py 实际定义适配：name/display_name/type 等）；tasks.py `_task_scan_datasource` 开头加 `if info.db_type == "mcp"` 分支：进度回写复用既有机制 → list_tools（惰性 import sdk）→ synthesize → save_content → scan_status="done"（跳过 `_connect_business`/内省/LLM 富化）。
- [ ] **Step 4: 绿 + 回归** — `pytest tests/domains/ -k "chatbi or semantic" -q`
- [ ] **Step 5: Commit** — `feat(chatbi-mcp): 扫描合成语义层——外部工具=表/参数=列`

---

### Task 4: ask_data 管线 mcp 分支（generate 工具调用 + execute 调 MCP）

**Files:**
- Modify: `backend/src/domains/chatbi/tools/ask_data.py`（generate/execute 两步 mcp 分支）
- Modify: `backend/src/domains/chatbi/datasources.py`（`execute_mcp_tool` 新函数）
- Test: `backend/tests/domains/test_chatbi_mcp_pipeline.py`（新建）

**Interfaces:**
- Consumes: Task 3 的合成语义层（state["content"]/allowed_columns 天然可用——白名单列=工具参数）；`sdk.mcp_client.call_tool(code, tool, args) -> dict`。
- Produces: `datasources.execute_mcp_tool(info: DataSourceInfo, tool_call: dict, max_rows=10000) -> ExecuteResult`——`tool_call` 形如 `{"tool": "query_orders", "args": {...}}`；结果映射：dict 含 `columns`+`rows`（rows 为 list[list]/list[tuple]）→ 直接用；list[dict] → 首行 keys 为 columns、值转 tuple；list[list] → columns 为空报错；其他形状 → 单列 `value`。行数超 max_rows 截断。ask_data：`state["sql"]` 对 mcp 数据源存工具调用 JSON 字符串（prev_sql 重放/switch_chart 天然兼容）。

**generate 步 mcp 变体**（`_step_generate_sql`）：`ds.db_type=="mcp"` 时走独立 prompt：输入=question+schema_context（合成语义层的工具/参数清单）+注入段（skills/fewshot/预思考等沿用，跳过 JOIN 路径/指标段——合成层无 relationships/metrics）；输出=工具调用 JSON（`{"tool":..., "args":{...}}`，chat_json 解析）；校验：tool ∈ 合成模型名集合、args 是 dict，不过→`state["_error"]` fail-closed。**execute 步 mcp 分支**（`_step_execute_sql`）：`ds.db_type=="mcp"` 时解析 `state["sql"]` JSON → `execute_mcp_tool`；失败进自愈回环（heal 用 mcp 变体 prompt：原调用+错误文本→重新生成，复用回环结构 ≤2 轮）；成功存 `state["execute_result"]`，后续步骤零改动。

- [ ] **Step 1: 失败测试**（核心：execute_mcp_tool 映射 + 管线分支；LLM 用 monkeypatch 假 chat_json）

```python
# backend/tests/domains/test_chatbi_mcp_pipeline.py
"""ask_data mcp 分支:工具调用 JSON 生成(mock LLM)+execute_mcp_tool 映射+全管线。"""
import json
from types import SimpleNamespace
import pytest
from domains.chatbi.datasources import DataSourceInfo, execute_mcp_tool


def _info(code="srv_a"):
    return DataSourceInfo(id="ds1", name="外部", db_type="mcp",
                          host=code, port=0, database="",
                          username="", password_plain="")


def test_execute_mcp_dict_columns_rows(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: {"columns": ["city", "gmv"],
                                         "rows": [["上海", 100], ["北京", 200]]})
    r = execute_mcp_tool(_info(), {"tool": "query_gmv", "args": {}})
    assert r.ok and r.columns == ["city", "gmv"]
    assert r.rows == [("上海", 100), ("北京", 200)] and r.rowcount == 2


def test_execute_mcp_list_of_dicts(monkeypatch):
    from sdk import mcp_client
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda c, t, a: [{"city": "上海", "gmv": 1},
                                         {"city": "北京", "gmv": 2}])
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert r.ok and r.columns == ["city", "gmv"] and r.rowcount == 2


def test_execute_mcp_error_honest(monkeypatch):
    from sdk.tool import PackInvokeError
    from sdk import mcp_client
    def boom(c, t, a):
        raise PackInvokeError("MCP server 连接失败(srv_a): refused",
                              PackInvokeError.RETRYABLE)
    monkeypatch.setattr(mcp_client, "call_tool", boom)
    r = execute_mcp_tool(_info(), {"tool": "t", "args": {}})
    assert not r.ok and "srv_a" in r.error      # 如实,不静默


def test_execute_mcp_rejects_unknown_tool():
    r = execute_mcp_tool(_info(), {"tool": "nope", "args": {}})
    assert not r.ok and "nope" in r.error
```

管线级测试（AskDataTool 直调，mock LLM chat_json 返回工具调用 JSON + mock call_tool 返回数据 + 合成语义层 state）——断言 `state["execute_result"].rows` 与 `state["sql"]` 是合法工具调用 JSON。以 AskDataTool 既有测试（tests/domains/ 下 chatbi 相关）的最小 state 构造方式为准适配。

- [ ] **Step 2: 红** — FAIL（execute_mcp_tool 不存在/分支缺失）
- [ ] **Step 3: 实现** — datasources.py 加 `execute_mcp_tool`（含 JSON 解析/白名单外工具拒绝/形状映射/截断）；ask_data.py 两步加分支（mcp prompt 变体写成模块级常量，参照 `_SQL_SYSTEM_PROMPT` 位置；heal 变体同构）。
- [ ] **Step 4: 绿 + 回归** — 新测试 + `pytest tests/domains/ -k chatbi -q`
- [ ] **Step 5: Commit** — `feat(chatbi-mcp): ask_data 管线 mcp 分支——工具调用生成+MCP 执行+自愈回环`

---

### Task 5: 前端数据源管理页 mcp 类型

**Files:**
- Modify: `frontend/src/admin/packPages/chatbi/DsManager.vue`（类型下拉+表单切换）
- Modify: `frontend/src/admin/api.ts`（如需 mcp_servers 列表函数复用）

**Interfaces:**
- Consumes: Task 1 的 mcp 数据源 API；一期 `listMcpServers`（api.ts 已有）。
- Produces: 类型下拉三项（postgresql/mysql/mcp）；选 mcp 时表单切换为「MCP 服务」下拉（listMcpServers 的 code+name，只列 connected）+「限定工具」可选输入（留空=全部）；连接信息字段（host/port/user/password）隐藏；列表类型列显示 mcp 徽标。

- [ ] **Step 1: 实现**（表单字段切换逻辑参照该文件既有 db_type 联动；提交体 mcp 时 host=所选 code、database=工具名或空、port=0、username/password 空串）
- [ ] **Step 2: `cd frontend && pnpm build` 通过**
- [ ] **Step 3: Commit** — `feat(chatbi-mcp): 数据源管理页 mcp 类型表单`

---

### Task 6: 双向测试项目 tools/mcp-e2e

**Files:**
- Create: `tools/mcp-e2e/fake_servers.py`（异构外部 server：含一个"报表数据"server——query_orders/query_gmv 工具返回 tabular 数据；一个 SSE 传输；一个带鉴权）
- Create: `tools/mcp-e2e/run_e2e.py`（主流程脚本）
- Create: `tools/mcp-e2e/README.md`

**Interfaces:**
- Consumes: 一期全部产物（admin API/mcp_servers 注册表/sdk client//api/mcp 端点）+ Task 1-4（mcp 数据源/扫描/ask_data）。
- Produces: 一条命令跑通双向闭环：`cd backend && .venv/bin/python ../tools/mcp-e2e/run_e2e.py --base http://localhost:18080 --frontend-origin http://localhost:13081`（参数可调）。

**run_e2e.py 流程（每步打印 ✓/✗ 与证据）：**
1. 起 fake servers（uvicorn 线程，随机端口；"报表数据" server 用 low-level SDK 手搭，工具返回 `{"columns":[...],"rows":[[...]]}`）
2. **注册**：经 `POST /api/admin/mcp/servers` 注册三个 server → 断言 connected+快照工具数
3. **建 mcp 数据源**：`POST /datasources`（db_type=mcp, host=报表 server 的 code）→ 断言行存在
4. **扫描**：`POST /datasources/{id}/scan` + 轮询 `GET .../scan` 至 done → `GET` 语义层断言工具=表
5. **消费（ask_data 全链路）**：`POST /api/config/chat`（packs=["chatbi"], user_input="查一下各城市订单量"）→ 收 SSE 至 result → 断言 artifact 含图表数据/行数>0（真实 LLM；失败时打印 call_logs 取证提示）
6. **出方向**：官方 mcp client 连 `{frontend-origin}/ai-modeler/api/mcp`（key 从管理端读或参数传入）→ tools/list + kb_search 调用
7. **错误路径**：停掉一个 fake server → 健康巡检 `POST /datasources/health-check/all` → 断言该数据源被标记不健康/禁用
8. 汇总打印 PASS/FAIL 清单，退出码非 0 on fail

- [ ] **Step 1: 实现 fake_servers.py**（复用 tests/test_mcp_integration.py 的 server 搭法，抽成可 import 的模块）
- [ ] **Step 2: 实现 run_e2e.py**（httpx 同步 client + SSE 解析用 httpx stream；ask_data 的 SSE 事件格式参照 engine/stream.py 与前端 services/api.ts 的消费方式）
- [ ] **Step 3: README**（用途/前置条件/命令/预期输出/排障：call_logs 取证）
- [ ] **Step 4: 本地全链路跑通**（后端 18080+前端 13081 已在跑；真实 LLM 走 .env 网关）——输出全 ✓
- [ ] **Step 5: Commit** — `feat(mcp-e2e): 双向测试项目——注册/消费/对外全链路一键验证`

---

### Task 7: 全量回归 + 终审（隔离性重点）+ 合并

**Files:** 无新文件。

- [ ] **Step 1: 全量 pytest** — `cd backend && .venv/bin/python -m pytest tests/ -q` 全绿（含新增 ~10 例）
- [ ] **Step 2: 隔离性静态检查** — `grep -rn "import services\|from services" backend/src/domains/chatbi/` 零命中（sdk import 不算）；`grep -rn "mcp" backend/src/engine/` 零命中
- [ ] **Step 3: 整分支终审**（SDD final review：spec §3.5 出口标准四条 + 隔离性 + ExecuteResult 形状契约 + fail-closed）
- [ ] **Step 4: 浏览器实测** — 管理端建 mcp 数据源→扫描→对话页问数出图（chrome-devtools 真点）
- [ ] **Step 5: 合并回 main + 收尾提交**

