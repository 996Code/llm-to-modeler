# 阶段 0:骨架搭建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 engine/sdk/domains 三层目录骨架 + 所有 ABC/数据类 + Engine 空实现委托旧代码，**零功能改动**，现有流程仍工作

**Architecture:** 本阶段不引入任何新行为。SDK 层定义抽象（Tool/CompositeTool/AssetClient/ToolRegistry + 数据类），Engine 层用空壳类委托给现有 graph/conversation_store/sse。冒烟测试验证无回归。

**Tech Stack:** Python 3.11+ / Pydantic v2 / pytest

**前置条件:** 无（本阶段是改造起点）

**权威来源:** v4 设计文档 §4（SDK 契约）、§5（Engine 核心）、§7 阶段 0

---

## File Structure

本阶段新建以下文件（全部是空壳/ABC/数据类，无业务逻辑）：

```
backend/src/
├── sdk/                          # 新建:契约层
│   ├── __init__.py
│   ├── tool.py                   # Tool/CompositeTool/ToolContext/ToolResult/AskSpec
│   ├── asset_client.py           # AssetClient ABC
│   └── registry.py               # ToolRegistry
├── engine/                       # 新建:通用内核(空实现委托)
│   ├── __init__.py
│   ├── dispatcher.py             # ToolDispatcher 骨架(委托 FormConfigWorkflow)
│   ├── conversation.py           # ConversationManager 骨架(委托 ConversationStore)
│   └── stream.py                 # StreamManager 骨架(委托现有 sse.StreamManager)
└── (现有 graph/services/api/llm 不动)

backend/
├── pytest.ini                    # 新建:pytest 配置
└── tests/
    ├── __init__.py
    ├── conftest.py               # 新建:公共 fixture
    └── test_phase0_smoke.py      # 新建:冒烟测试(验证无回归)
```

**架构试金石**: 本阶段结束后 `grep -rE "form|formCode|template|field" engine/` 应为空（engine 不含领域词）。

---

## Task 1: pytest 初始化 + 目录骨架

**Files:**
- Create: `backend/pytest.ini`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/src/sdk/__init__.py`
- Create: `backend/src/engine/__init__.py`
- Create: `backend/src/domains/__init__.py`
- Create: `backend/src/domains/njmind_form/__init__.py`
- Create: `backend/src/adapters/__init__.py`

- [ ] **Step 1: 创建 pytest 配置**

写入 `backend/pytest.ini`:
```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
```

- [ ] **Step 2: 创建空 `__init__.py` 和 conftest**

写入 `backend/tests/conftest.py`:
```python
"""公共 pytest fixtures."""
import sys
from pathlib import Path

# 让 backend/src 可被 import
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "src"))
```

所有 `__init__.py` 创建为空文件。

- [ ] **Step 3: 验证 pytest 可运行**

Run: `cd backend && python -m pytest --collect-only 2>&1 | tail -5`
Expected: `collected 0 items`（无测试但不报错）

- [ ] **Step 4: 验证现有 import 不破**

Run: `cd backend && python -c "from src.graph.graph import FormConfigWorkflow; from src.services.conversation_store import ConversationStore; from src.api.sse import StreamManager; print('imports OK')"`
Expected: `imports OK`

- [ ] **Step 5: Commit**

```bash
git add backend/pytest.ini backend/tests/ backend/src/sdk/ backend/src/engine/ backend/src/domains/ backend/src/adapters/
git commit -m "chore(phase0): 初始化 pytest + 目录骨架(engine/sdk/domains/adapters)"
```

---

## Task 2: SDK 层 - ToolResult / AskSpec 数据类

**Files:**
- Create: `backend/src/sdk/tool.py`
- Test: `backend/tests/sdk/test_tool_result.py`

**设计来源:** v4 §4.1（含 C.2-A 追问的 AskSpec）

- [ ] **Step 1: 写失败测试**

写入 `backend/tests/sdk/__init__.py`（空）和 `backend/tests/sdk/test_tool_result.py`:
```python
"""ToolResult 三层结构 + AskSpec 追问规格测试。"""
import pytest
from pydantic import ValidationError

from sdk.tool import ToolResult, AskSpec, AskQuestion, AskOption, ClarificationRaised


class TestToolResult:
    def test_empty_result_defaults(self):
        """空 ToolResult 所有字段有默认值。"""
        r = ToolResult()
        assert r.artifact is None
        assert r.reply is None
        assert r.ask is None
        assert r.summary == ""
        assert r.extra == {}
        assert r.error_for_llm is None

    def test_artifact_is_dict_opaque(self):
        """artifact 是 dict,Engine 不读内部结构。"""
        r = ToolResult(artifact={"formCode": "leave", "formFieldConfigVos": []})
        assert isinstance(r.artifact, dict)

    def test_extra_accepts_arbitrary(self):
        """extra 接受领域自由扩展。"""
        r = ToolResult(extra={"validation_errors": ["字段缺失"]})
        assert r.extra["validation_errors"] == ["字段缺失"]


class TestAskSpec:
    """C.2-A:Clarification 建模为内置 AskTool 的追问规格。"""

    def test_ask_with_questions(self):
        q = AskQuestion(
            question="请假单需要哪些字段?",
            header="字段",
            options=[AskOption(label="基础字段", description="申请人、日期"),
                     AskOption(label="完整字段", description="含请假原因、审批人")],
        )
        spec = AskSpec(questions=[q])
        assert len(spec.questions) == 1
        assert spec.questions[0].header == "字段"

    def test_ask_option_requires_label_and_description(self):
        with pytest.raises(ValidationError):
            AskOption()  # 缺必填


class TestClarificationRaisedLegacy:
    """向后兼容:旧式异常仍可抛(阶段 3 改造完的工具会切到 ToolResult.ask)。"""

    def test_exception_carries_questions(self):
        exc = ClarificationRaised(questions=["需要哪些字段?"])
        assert exc.questions == ["需要哪些字段?"]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/sdk/test_tool_result.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sdk.tool'`

- [ ] **Step 3: 写最小实现**

写入 `backend/src/sdk/tool.py`:
```python
"""SDK 工具协议 — Tool/CompositeTool/数据类。

对标 Claude Code src/Tool.ts。设计原则:
- Fail-Closed 默认值:安全相关属性默认保守
- 安全声明与执行分离:validate_input 先于 execute
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional
from pydantic import BaseModel, Field, ConfigDict


class ToolContext(BaseModel):
    """工具执行时拿到的依赖(由 Engine 注入)。"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_client: Any              # LLMClient(chat / chat_json)
    asset_client: Any            # AssetClient
    conversation: Any            # ConversationStore
    emit: Callable[..., None]    # emit(event_type, message, **extra)
    forward_headers: dict = Field(default_factory=dict)


class AskOption(BaseModel):
    """追问选项。"""
    label: str
    description: str


class AskQuestion(BaseModel):
    """单个追问问题。"""
    question: str
    header: str                       # ≤12 字符,前端显示为 chip
    options: list[AskOption]          # 2-4 个(前端自动加"其他")
    multi_select: bool = False


class AskSpec(BaseModel):
    """追问规格。对标 CC AskUserQuestionTool。
    工具产出 ToolResult.ask → SSE 推前端 → 用户带 answers 重发 → dispatcher 重跑工具。"""
    questions: list[AskQuestion]


class ToolResult(BaseModel):
    """工具执行结果。三层设计:
    - artifact: 不透明制品,Engine 不读内部结构
    - summary: 标准化摘要,进 ConversationManager 历史
    - extra: 领域自由扩展,不进历史
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    artifact: Optional[dict] = None
    reply: Optional[str] = None
    ask: Optional[AskSpec] = None     # 非空 = 需要追问(C.2-A)
    summary: str = ""
    extra: dict = Field(default_factory=dict)
    error_for_llm: Optional[str] = None


class ClarificationRaised(Exception):
    """[向后兼容] 工具中途需要追问时抛出。
    v4 起统一改用 ToolResult.ask。新代码不应再抛此异常。"""
    def __init__(self, questions: list[str]):
        self.questions = questions


# 以下在本 Task 暂不实现(下个 Task),先占位避免 import 错
class Tool(ABC):
    """工具基类。下个 Task 实现。"""
    pass


class CompositeTool(Tool):
    """复合工具基类。下个 Task 实现。"""
    pass
```

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/sdk/test_tool_result.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/sdk/tool.py backend/tests/sdk/
git commit -m "feat(sdk): ToolResult/AskSpec/ClarificationRaised 数据类(C.2-A 追问规格)"
```

---

## Task 3: SDK 层 - Tool / CompositeTool ABC

**Files:**
- Modify: `backend/src/sdk/tool.py`（替换 Tool/CompositeTool 占位实现）
- Test: `backend/tests/sdk/test_tool_abc.py`

**设计来源:** v4 §4.1（Fail-Closed 默认值 + validate_input + requires_follow_up + hooks）

- [ ] **Step 1: 写失败测试**

写入 `backend/tests/sdk/test_tool_abc.py`:
```python
"""Tool/CompositeTool ABC 测试 — Fail-Closed 默认值 + hooks。"""
import pytest
from sdk.tool import Tool, CompositeTool, ToolResult, ToolContext


class DummyTool(Tool):
    """测试用最小工具实现。"""
    name = "dummy"
    description = "测试工具"
    when = "测试时用"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(reply="ok", summary="dummy 执行完成")


class TestToolFailClosedDefaults:
    """Fail-Closed:安全相关属性默认保守。"""

    def test_is_destructive_defaults_true(self):
        t = DummyTool()
        assert t.is_destructive is True  # 默认破坏性

    def test_is_read_only_defaults_false(self):
        t = DummyTool()
        assert t.is_read_only is False

    def test_is_concurrency_safe_defaults_false(self):
        """C.2-B:默认不可并发(保守)。"""
        t = DummyTool()
        assert t.is_concurrency_safe is False


class TestToolHooks:
    """可选 hooks 有默认实现,pack 按需覆写。"""

    def test_validate_input_defaults_none(self):
        t = DummyTool()
        assert t.validate_input({}) is None  # 默认通过

    def test_requires_follow_up_defaults_false(self):
        t = DummyTool()
        assert t.requires_follow_up(ToolResult()) is False

    def test_summarize_artifact_defaults_empty(self):
        t = DummyTool()
        assert t.summarize_artifact({}) == ""

    def test_title_for_defaults_empty(self):
        t = DummyTool()
        assert t.title_for({}) == ""


class TestCompositeTool:
    """CompositeTool 提供 step 编排。"""

    def test_steps_defaults_empty(self):
        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx): return ToolResult()

        c = MyComposite()
        assert c.steps == []

    def test_run_pipeline_executes_steps_in_order(self):
        """steps 按序调用 _step_<name>。"""
        executed = []

        class MyComposite(CompositeTool):
            name = "my"
            description = "d"
            when = "w"
            steps = ["alpha", "beta"]

            def input_schema(self): return {"type": "object"}
            def execute(self, state, ctx):
                self.run_pipeline(state, ctx)
                return ToolResult(summary="done")

            def _step_alpha(self, state, ctx):
                executed.append("alpha")

            def _step_beta(self, state, ctx):
                executed.append("beta")

        c = MyComposite()
        ctx = _make_ctx()
        c.run_pipeline({}, ctx)
        assert executed == ["alpha", "beta"]


def _make_ctx() -> ToolContext:
    """构造测试用 ToolContext。"""
    return ToolContext(
        llm_client=None,
        asset_client=None,
        conversation=None,
        emit=lambda *a, **k: None,
    )
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/sdk/test_tool_abc.py -v`
Expected: FAIL（`Tool` 是占位 pass，没有 is_destructive 等属性）

- [ ] **Step 3: 实现 Tool / CompositeTool**

替换 `backend/src/sdk/tool.py` 末尾的占位类:
```python
class Tool(ABC):
    """工具基类。对标 Claude Code 的 Tool 协议(src/Tool.ts)。

    设计原则(借鉴 CC):
    - Fail-Closed 默认值:安全相关属性默认保守,避免误用
    - 安全声明与执行分离:check_permissions / validate_input 先于 execute
    """
    name: str                     # 工具名,LLM 选择时看到
    description: str              # 工具说明
    when: str                     # "何时用"短描述(填进选择 prompt)

    # ── 安全声明(Fail-Closed,借鉴 CC Tool.ts:757)──
    is_destructive: bool = True   # 默认破坏性,需 pack 显式声明 False 才认为安全
    is_read_only: bool = False
    # ── 并发安全声明(C.2-B:借鉴 CC isConcurrencySafe)──
    is_concurrency_safe: bool = False

    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema,描述这个工具需要的参数(从 state 抽取)。"""

    @abstractmethod
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具。可中途 emit progress,可抛 ClarificationRaised(兼容)。"""

    def validate_input(self, state: dict) -> Optional[str]:
        """语义校验(比 JSON Schema 更严格)。返回错误文本或 None。
        默认 None=通过。Engine 在 execute 前调用,失败则跳过 execute、
        把错误写进 ToolResult.error_for_llm 回流给下一轮选择。"""
        return None

    def requires_follow_up(self, result: ToolResult) -> bool:
        """工具执行后是否需要 Engine 再做一轮选择。默认 False。
        未来引入 Agent Loop 时,工具可声明"我做完但还需要继续判断"。"""
        return False

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:从制品提取状态补偿文本。默认空。"""
        return ""

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从制品生成标题。默认空。"""
        return ""


class CompositeTool(Tool):
    """复合工具基类:内部有多步 pipeline。对标 CC 的 Skill——
    "封装一个工作流 + 声明触发条件"。

    run_pipeline 顺序执行 steps:
    - 每个 step 对应 _step_<name>(state, ctx) 方法
    - step 内可抛 ClarificationRaised → 立即上抛,Engine 转成 SSE
    - step 内可重跑前序 step 实现 retry(如 validate 失败重跑 generate)
    - 每个 step 自动 emit 一个 stage 事件
    """
    steps: list[str] = []

    def run_pipeline(self, state: dict, ctx: ToolContext) -> None:
        for step_name in self.steps:
            ctx.emit("stage", step_name)
            method = getattr(self, f"_step_{step_name}")
            method(state, ctx)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/sdk/ -v`
Expected: 之前 5 个 + 新 8 个,共 13 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/sdk/tool.py backend/tests/sdk/test_tool_abc.py
git commit -m "feat(sdk): Tool/CompositeTool ABC(Fail-Closed 默认值 + step 编排)"
```

---

## Task 4: SDK 层 - AssetClient ABC + ToolRegistry

**Files:**
- Create: `backend/src/sdk/asset_client.py`
- Create: `backend/src/sdk/registry.py`
- Test: `backend/tests/sdk/test_registry.py`

**设计来源:** v4 §4.2 AssetClient、§4.3 ToolRegistry

- [ ] **Step 1: 写失败测试**

写入 `backend/tests/sdk/test_registry.py`:
```python
"""ToolRegistry 测试。"""
import pytest
from sdk.registry import ToolRegistry
from sdk.tool import Tool, ToolResult, ToolContext


class FakeTool(Tool):
    def __init__(self, name, when="测试"):
        self.name = name
        self.description = f"{name} 工具"
        self.when = when

    def input_schema(self): return {"type": "object"}
    def execute(self, state, ctx): return ToolResult()


class TestToolRegistry:
    def test_register_and_get(self):
        r = ToolRegistry()
        t = FakeTool("create_form")
        r.register(t)
        assert r.get("create_form") is t

    def test_all_returns_registered(self):
        r = ToolRegistry()
        r.register(FakeTool("a"))
        r.register(FakeTool("b"))
        names = sorted(t.name for t in r.all())
        assert names == ["a", "b"]

    def test_get_missing_returns_none(self):
        r = ToolRegistry()
        assert r.get("nope") is None

    def test_describe_for_llm_lists_tools(self):
        r = ToolRegistry()
        r.register(FakeTool("create_form", when="用户想新建表单时"))
        r.register(FakeTool("chat", when="闲聊时"))
        desc = r.describe_for_llm(state={})
        assert "create_form" in desc
        assert "chat" in desc
        assert "用户想新建表单时" in desc
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/sdk/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sdk.registry'`

- [ ] **Step 3: 实现 AssetClient + ToolRegistry**

写入 `backend/src/sdk/asset_client.py`:
```python
"""AssetClient 抽象 — 资产来源的抽象接口。

pack 用它取模板/schema/guide/校验/持久化,不关心是 HTTP 还是本地。
通用实现 HttpAssetClient 在 adapters/(阶段 1 实现)。

安全约定(阶段 1 强化):所有 get_* 方法返回的内容在进入 prompt 前
必须经过 Unicode 清洗(sdk.sanitize.sanitize_obj),防止上游数据
携带零宽字符/方向反转字符等隐写指令。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class AssetClient(ABC):
    """资产来源的抽象。"""

    @abstractmethod
    def get_template(self, name: str) -> dict:
        """取模板 JSON。"""

    @abstractmethod
    def list_templates(self) -> list[str]:
        """列出所有模板名。"""

    @abstractmethod
    def get_schema(self, name: str) -> dict:
        """取 JSON Schema。"""

    @abstractmethod
    def get_guide(self) -> dict:
        """取 guide.json。"""

    @abstractmethod
    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验制品。mode ∈ {"create", "update"}。
        返回 {valid: bool, errors: list, warnings: list}。"""

    @abstractmethod
    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """持久化制品到上游。mode ∈ {"create", "update"}。
        返回 {success: bool, ...}。"""
```

写入 `backend/src/sdk/registry.py`:
```python
"""ToolRegistry — 工具注册表。

pack 启动时静态注册工具。describe_for_llm(state) 生成给 LLM 看的工具清单。
"""
from typing import Optional
from sdk.tool import Tool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def describe_for_llm(self, state: dict) -> str:
        """生成给 LLM 看的工具清单。
        当前简单列出 name/description/when。
        阶段 3 增强:按 state 过滤不可用工具(如无 artifact 时禁用 modify)。"""
        lines = ["可用工具:"]
        for tool in self._tools.values():
            lines.append(f"- {tool.name}: {tool.description} (适用: {tool.when})")
        return "\n".join(lines)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/sdk/ -v`
Expected: 17 passed（13 + 新 4）

- [ ] **Step 5: Commit**

```bash
git add backend/src/sdk/asset_client.py backend/src/sdk/registry.py backend/tests/sdk/test_registry.py
git commit -m "feat(sdk): AssetClient ABC + ToolRegistry(register/all/get/describe_for_llm)"
```

---

## Task 5: Engine 层 - 空实现委托旧代码

**Files:**
- Create: `backend/src/engine/dispatcher.py`
- Create: `backend/src/engine/conversation.py`
- Create: `backend/src/engine/stream.py`
- Test: `backend/tests/engine/test_skeleton.py`

**设计来源:** v4 §5.1/§5.2/§5.3 骨架。本阶段只搭空壳,**委托给现有 FormConfigWorkflow/ConversationStore/StreamManager**。

- [ ] **Step 1: 写委托骨架测试**

写入 `backend/tests/engine/__init__.py`（空）和 `backend/tests/engine/test_skeleton.py`:
```python
"""Engine 骨架测试 — 验证空实现可实例化,委托旧代码不报错。"""
from engine.dispatcher import ToolDispatcher
from engine.conversation import ConversationManager
from engine.stream import StreamManager


class TestEngineSkeleton:
    def test_dispatcher_instantiable(self):
        d = ToolDispatcher.__new__(ToolDispatcher)  # 不触发 __init__(需要依赖)
        assert d is not None

    def test_conversation_manager_instantiable(self):
        cm = ConversationManager.__new__(ConversationManager)
        assert cm is not None

    def test_stream_manager_instantiable(self):
        sm = StreamManager.__new__(StreamManager)
        assert sm is not None

    def test_engine_has_no_domain_words(self):
        """架构试金石:engine/ 目录下不应有领域词汇。"""
        import subprocess
        from pathlib import Path
        engine_dir = Path(__file__).resolve().parent.parent.parent / "src" / "engine"
        result = subprocess.run(
            ["grep", "-rE", "form|formCode|template|field", str(engine_dir)],
            capture_output=True, text=True,
        )
        # 排除注释中的引用(行内有 # 或 """ 或 //)
        offending = [
            line for line in result.stdout.splitlines()
            if line.strip()
            and not any(mark in line for mark in ["#", '"""', "//"])
        ]
        assert not offending, f"engine/ 含领域词汇:\n{chr(10).join(offending)}"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/engine/test_skeleton.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.dispatcher'`

- [ ] **Step 3: 实现空壳 Engine**

写入 `backend/src/engine/dispatcher.py`:
```python
"""ToolDispatcher — 工具调度器。

阶段 0:空壳,不实现调度逻辑(阶段 3 实现)。
本阶段只声明类存在,Engine 模块可被 import。
"""
from typing import Any


class ToolDispatcher:
    """阶段 0 占位。阶段 3 实现 _select_tools / _partition_tool_calls /
    _run_single / _run_concurrent / _resume_ask 等调度逻辑。"""

    def __init__(self, registry: Any = None, conversation: Any = None,
                 llm_client: Any = None):
        self._registry = registry
        self._conversation = conversation
        self._llm_client = llm_client
        self._max_clarify_rounds = 3
```

写入 `backend/src/engine/conversation.py`:
```python
"""ConversationManager — 多轮/压缩/存储。

阶段 0:空壳。阶段 4 实现 append/load/save/list_meta/compress。
当前实际存储仍由 services/conversation_store.py 负责。
"""
from typing import Any


class ConversationManager:
    """阶段 0 占位。阶段 4 实现 append-only 事件流 + 压缩 sidechain。"""

    def __init__(self, store: Any = None):
        self._store = store  # 委托给现有 ConversationStore
```

写入 `backend/src/engine/stream.py`:
```python
"""StreamManager — SSE 流式桥接。

阶段 0:空壳。实际 SSE 仍由 api/sse.py 的 StreamManager 负责。
阶段 3-4 把 result payload 钩子化(调 tool.format_result)。
"""
from typing import Any


class StreamManager:
    """阶段 0 占位。阶段 3 增强后委托给现有 api.sse.StreamManager。"""

    def __init__(self):
        pass
```

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/engine/ -v`
Expected: 4 passed（含架构试金石）

- [ ] **Step 5: 架构试金石全量验证**

Run: `cd backend && grep -rE "form|formCode|template|field" src/engine/ src/sdk/`
Expected: 空输出（engine/ 和 sdk/ 都不含领域词）

- [ ] **Step 6: Commit**

```bash
git add backend/src/engine/ backend/tests/engine/
git commit -m "feat(engine): 空壳骨架(dispatcher/conversation/stream 委托旧代码占位)"
```

---

## Task 6: 冒烟测试 — 验证现有流程无回归

**Files:**
- Create: `backend/tests/test_phase0_smoke.py`

**目的:** 本阶段零功能改动,现有 LangGraph 流程必须仍能工作。

- [ ] **Step 1: 写冒烟测试**

写入 `backend/tests/test_phase0_smoke.py`:
```python
"""阶段 0 冒烟测试 — 验证现有流程无回归。

本阶段只搭骨架,不引入新行为。以下 import 都必须成功,
证明 engine/sdk/domains 目录创建没有破坏现有代码。
"""
import pytest


def test_existing_graph_imports():
    """现有 LangGraph workflow 仍可 import。"""
    from src.graph.graph import FormConfigWorkflow
    assert FormConfigWorkflow is not None


def test_existing_store_imports():
    """现有 ConversationStore 仍可 import。"""
    from src.services.conversation_store import ConversationStore
    assert ConversationStore is not None


def test_existing_sse_imports():
    """现有 StreamManager 仍可 import。"""
    from src.api.sse import StreamManager as ExistingStreamManager
    assert ExistingStreamManager is not None


def test_existing_upstream_imports():
    """现有 UpstreamClient 仍可 import。"""
    from src.services.upstream_client import UpstreamClient
    assert UpstreamClient is not None


def test_existing_llm_client_imports():
    """现有 LLMClient 仍可 import。"""
    from src.llm.client import LLMClient
    assert LLMClient is not None


def test_new_sdk_imports():
    """新 SDK 模块可 import。"""
    from sdk.tool import Tool, CompositeTool, ToolResult, AskSpec, ToolContext
    from sdk.asset_client import AssetClient
    from sdk.registry import ToolRegistry
    assert all([Tool, CompositeTool, ToolResult, AskSpec, ToolContext,
                AssetClient, ToolRegistry])


def test_new_engine_imports():
    """新 Engine 模块可 import。"""
    from engine.dispatcher import ToolDispatcher
    from engine.conversation import ConversationManager
    from engine.stream import StreamManager
    assert all([ToolDispatcher, ConversationManager, StreamManager])
```

- [ ] **Step 2: 跑全部测试**

Run: `cd backend && python -m pytest -v`
Expected: 全部 passed（之前的 sdk/engine 测试 + 新 7 个冒烟测试）

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_phase0_smoke.py
git commit -m "test(phase0): 冒烟测试 — 验证现有流程无回归 + 新模块可 import"
```

---

## 阶段 0 完成检查

完成本阶段后,验证以下全部通过:

- [ ] `cd backend && python -m pytest -v` 全部 passed
- [ ] `grep -rE "form|formCode|template|field" backend/src/engine/ backend/src/sdk/` 无结果
- [ ] 现有 LangGraph 流程未被修改(`/api/chat` 行为不变)
- [ ] 目录结构已创建:engine/sdk/domains/adapters/

**回滚方式**: 删除 `backend/src/{engine,sdk,domains,adapters}/` 和 `backend/tests/{sdk,engine}/` 即可完全恢复。

**下一阶段**: [01-asset-client.md](./01-asset-client.md) — 抽 AssetClient + Unicode 清洗
