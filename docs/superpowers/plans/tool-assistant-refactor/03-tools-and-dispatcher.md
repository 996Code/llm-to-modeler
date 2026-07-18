# 阶段 3:工具 + Dispatcher + C.2-A 追问 + C.2-B 并发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** 实现 `CreateFormTool`/`ModifyFormTool`/`ChatTool`（把现有 6 步/3 步管线搬进复合工具），实现 `ToolDispatcher`（单轮多工具 + is_concurrency_safe 分批并发 + ToolResult.ask 追问重跑），切换 `/api/chat` 走 Dispatcher。

**Architecture:** 工具内部封装管线（CompositeTool.steps），Dispatcher 只做"选→校验→执行→分流"。retry 在工具内（_step_validate 递归重跑），clarify 通过 ToolResult.ask 跨请求异步重跑，并发通过 ThreadPoolExecutor。

**Tech Stack:** Python / concurrent.futures / Pydantic

**前置条件:** 阶段 2 完成（PromptLoader 可用，prompt 模板就绪）

**权威来源:** v4 §4.1 CompositeTool、§5.1 ToolDispatcher、§6.2-6.4 工具、§7 阶段 3、C.2-A/B

---

## File Structure

```
backend/src/
├── engine/
│   └── dispatcher.py             # 重写(阶段 0 是空壳):完整调度逻辑
├── domains/njmind_form/
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── create_form.py        # CreateFormTool(6 步复合)
│   │   ├── modify_form.py        # ModifyFormTool(3 步复合)
│   │   └── chat.py               # ChatTool(简单)
│   ├── models.py                 # FormConfig/ParsedField Pydantic
│   └── pack.py                   # NjmindFormPack:注册工具 + 提供 asset_client
└── (现有 graph/nodes.py 保留,阶段 4 删除)

backend/tests/
├── engine/test_dispatcher.py
├── engine/test_clarify_round.py       # C.2-A 追问重跑
├── engine/test_concurrent_batch.py    # C.2-B 并发
└── domains/tools/test_create_form.py
```

**关键迁移**: `nodes.py` 的节点函数 → 工具的 `_step_<name>` 方法（先搬位置不改逻辑）

---

## Task 1: FormConfig / ParsedField Pydantic 模型

**Files:**
- Create: `backend/src/domains/njmind_form/models.py`
- Test: `backend/tests/domains/test_models.py`

**设计来源:** v4 §6.1。把 njmind schema 收口到 pack 内。

- [ ] **Step 1: 写失败测试**
```python
# backend/tests/domains/test_models.py
from domains.njmind_form.models import FormConfig, ParsedField

def test_parsed_field_required_keys():
    f = ParsedField(fieldTitleText="姓名", fieldTitleKey="name", formFieldType="TEXT")
    assert f.fieldTypeName == "文本"  # 自动填充(type_names 查表)

def test_form_config_basic():
    c = FormConfig(formCode="leave", formName="请假表", formFieldConfigVos=[])
    assert c.formCode == "leave"
```

- [ ] **Step 2: 实现 models.py**（从 config.yaml 的 type_names 自动填充 fieldTypeName）

- [ ] **Step 3: 跑测试 + Commit**
```bash
git commit -m "feat(njmind_form): models.py FormConfig/ParsedField Pydantic"
```

---

## Task 2: ChatTool（最简单的工具，先做）

**Files:**
- Create: `backend/src/domains/njmind_form/tools/chat.py`
- Test: `backend/tests/domains/tools/test_chat.py`

**设计来源:** v4 §6.4。声明 is_concurrency_safe=True、is_read_only=True。

- [ ] **Step 1: 写失败测试**
```python
# backend/tests/domains/tools/test_chat.py
from unittest.mock import MagicMock
from domains.njmind_form.tools.chat import ChatTool
from sdk.tool import ToolContext

def test_chat_returns_reply():
    tool = ChatTool()
    llm = MagicMock(); llm.chat.return_value = "你好!我是表单助手。"
    ctx = ToolContext(llm_client=llm, asset_client=None, conversation=None,
                      emit=lambda *a, **k: None)
    result = tool.execute({"user_input": "你好"}, ctx)
    assert result.reply == "你好!我是表单助手。"
    assert result.summary  # 非空摘要进历史

def test_chat_is_concurrency_safe():
    assert ChatTool().is_concurrency_safe is True
    assert ChatTool().is_read_only is True
```

- [ ] **Step 2: 实现 ChatTool**（用 PromptLoader 渲染 chat.j2 → llm.chat → ToolResult）

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 3: CreateFormTool（6 步复合工具，核心）

**Files:**
- Create: `backend/src/domains/njmind_form/tools/create_form.py`
- Test: `backend/tests/domains/tools/test_create_form.py`

**设计来源:** v4 §6.2。把现有 `nodes.py` 的 fetch_guide/list_assets/parse_fields/fetch_templates/generate/validate 节点搬成 `_step_*` 方法。

- [ ] **Step 1: 写失败测试（mock asset_client + llm）**
```python
# 关键测试点
def test_create_form_6_steps_executed_in_order():
    """run_pipeline 按序执行 6 个 _step。"""
    # 用 mock 验证 _step_fetch_guide / _step_list_assets / ... 依次被调用

def test_step_validate_retries_on_failure():
    """校验失败 → 重跑 _step_generate,上限 3 次。"""

def test_step_parse_fields_raises_clarification():
    """信息不足 → 产出 ToolResult.ask(兼容 ClarificationRaised)。"""
```

- [ ] **Step 2: 实现 CreateFormTool**
```python
class CreateFormTool(CompositeTool):
    name = "create_form"
    description = "根据自然语言需求生成 njmind 表单配置"
    when = "用户想新建表单时"
    steps = ["fetch_guide", "list_assets", "parse_fields",
             "fetch_templates", "generate", "validate"]
    MAX_RETRIES = 3

    def execute(self, state, ctx):
        state.setdefault("retry_count", 0)
        self.run_pipeline(state, ctx)
        return ToolResult(
            artifact=state.get("artifact"),
            summary=f"已生成「{state['artifact'].get('formName', '')}」",
        )

    def _step_fetch_guide(self, state, ctx):
        state["guide"] = ctx.asset_client.get_guide()

    def _step_parse_fields(self, state, ctx):
        # ... 调 PromptLoader 渲染 parse.j2 → llm.chat_json
        if parsed.get("needsClarification"):
            raise ClarificationRaised(parsed["questions"])  # 兼容;阶段 3 末改 ToolResult.ask
        state["parsed_fields"] = parsed["fields"]

    def _step_validate(self, state, ctx):
        result = ctx.asset_client.validate_artifact(state["artifact"], mode="create")
        if result["valid"]:
            return
        state["retry_count"] += 1
        if state["retry_count"] < self.MAX_RETRIES:
            ctx.emit("stage", "validate_retry",
                     message=f"校验失败,第 {state['retry_count']} 次重试")
            self._step_generate(state, ctx)  # 重跑前序
            return self._step_validate(state, ctx)  # 递归
        state["validation_errors"] = result["errors"]
```

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 4: ModifyFormTool（3 步复合工具）

**Files:**
- Create: `backend/src/domains/njmind_form/tools/modify_form.py`
- Test: `backend/tests/domains/tools/test_modify_form.py`

**设计来源:** v4 §6.3。从 state["source_artifact"] 出发保留原字段。

- [ ] **Step 1: 写失败测试**
```python
def test_modify_preserves_existing_fields():
    """修改时保留原有字段,不丢失。"""
    # source_artifact 有 3 个字段,modify 后仍应有这 3 个 + 新增

def test_modify_requires_source_artifact():
    """无 source_artifact → validate_input 返回错误(回流给 LLM)。"""
    tool = ModifyFormTool()
    err = tool.validate_input({})  # 无 source_artifact
    assert err is not None
```

- [ ] **Step 2: 实现 ModifyFormTool**（steps=["fetch_guide", "modify", "validate"]）

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 5: ToolDispatcher — _select_tools + _partition_tool_calls（C.2-B）

**Files:**
- Modify: `backend/src/engine/dispatcher.py`（重写）
- Test: `backend/tests/engine/test_dispatcher.py`

**设计来源:** v4 §5.1。单轮多工具 + is_concurrency_safe 分批。

- [ ] **Step 1: 写失败测试**
```python
def test_select_tools_returns_list():
    """LLM 返回工具名列表(1..N)。"""

def test_partition_safe_tools_concurrent():
    """[A(safe), B(safe), C(unsafe), D(safe)] → [[A,B], [C], [D]]。"""
    dispatcher = ToolDispatcher(...)
    batches = dispatcher._partition_tool_calls([tool_a_safe, tool_b_safe, tool_c_unsafe, tool_d_safe])
    assert len(batches) == 3
    assert len(batches[0]) == 2  # A,B 并发

def test_run_concurrent_uses_thread_pool():
    """并发批次用 ThreadPoolExecutor。"""
```

- [ ] **Step 2: 实现 _select_tools + _partition_tool_calls + _run_concurrent**（见 v4 §5.1 完整代码）

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 6: ToolDispatcher — _run_single + C.2-A 追问重跑

**Files:**
- Modify: `backend/src/engine/dispatcher.py`
- Test: `backend/tests/engine/test_clarify_round.py`

**设计来源:** v4 §5.1 _run_single + _resume_ask。SSE 单向,追问跨请求。

- [ ] **Step 1: 写追问重跑测试**
```python
def test_clarify_saves_pending_ask():
    """工具产出 ask → state 存 pending_ask + emit type=ask。"""

def test_resume_ask_with_answers_reruns_tool():
    """load_state 检测 pending_ask + answers → 重跑工具。"""

def test_clarify_round_limit_3():
    """追问超 3 轮 → emit error + 清 pending_ask。"""

def test_legacy_clarification_raised_compatible():
    """工具抛 ClarificationRaised(旧式) → 转 ToolResult.ask。"""
```

- [ ] **Step 2: 实现 _run_single + _resume_ask**（见 v4 §5.1，含 pending_ask 保存/恢复）

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 7: /api/chat 切换走 ToolDispatcher

**Files:**
- Modify: `backend/src/api/config.py`（/api/chat 改用 dispatcher）
- Modify: `backend/src/api/sse.py`（stream_workflow 改调 dispatcher.run）

- [ ] **Step 1: 修改 config.py**
```python
# /api/chat 改为:
# 1. 加载 conversation → state
# 2. 若有 pending_ask 且本次带 answers → dispatcher.run(answers=...)
# 3. 否则 dispatcher.run(user_input=...)
```

- [ ] **Step 2: 修改 sse.py** — stream_workflow 调 dispatcher.run 并桥接 emit 到 SSE

- [ ] **Step 3: 端到端测试**（手动或脚本）
```bash
# 测三意图
curl -N -X POST http://localhost:18080/api/chat -d '{"message":"你好","conversation_id":"test"}'
curl -N -X POST http://localhost:18080/api/chat -d '{"message":"创建请假表...","conversation_id":"test"}'
curl -N -X POST http://localhost:18080/api/chat -d '{"message":"加一个手机号字段","conversation_id":"test"}'
```

- [ ] **Step 4: Commit**

---

## Task 8: 落库前确认（confirm SSE + dry_run）

**Files:**
- Modify: `backend/src/domains/njmind_form/tools/create_form.py`（加 _step_persist）
- Modify: `backend/src/sdk/tool.py`（ToolContext 加 dry_run 字段）

**设计来源:** v4 附录 D.1 #5。persist 前 emit confirm，用户确认才继续。

- [ ] **Step 1: 写测试** — dry_run=True 跳过 persist 返回预览
- [ ] **Step 2: 实现** — ToolContext.dry_run + _step_persist 内 if not ctx.dry_run: asset_client.persist_artifact
- [ ] **Step 3: Commit**

---

## Task 9: 端到端全回归

- [ ] **Step 1: 三意图 E2E**
- [ ] **Step 2: 追问重跑 E2E**（创建表单 → 信息不足 → 回答 → 重跑）
- [ ] **Step 3: 并发 E2E**（多工具请求）
- [ ] **Step 4: 架构试金石** — `grep -rE "formCode|formFieldConfigVos" backend/src/engine/` 应为空
- [ ] **Step 5: Commit**

---

## 阶段 3 完成检查

- [ ] `cd backend && python -m pytest -v` 全部 passed
- [ ] `grep -rE "formCode|formFieldConfigVos" backend/src/engine/` 无结果
- [ ] CreateFormTool 6 步 + ModifyFormTool 3 步 + ChatTool 端到端通
- [ ] C.2-A 追问重跑可用（pending_ask 持久化 + answers 重发）
- [ ] C.2-B 并发可用（is_concurrency_safe 分批）
- [ ] `/api/chat` 走 ToolDispatcher（旧 graph 不再被调用,但文件保留到阶段 4 删）

**下一阶段**: [04-storage-and-compression.md](./04-storage-and-compression.md) — append-only + RedactFilter + 压缩 sidechain + 清理
