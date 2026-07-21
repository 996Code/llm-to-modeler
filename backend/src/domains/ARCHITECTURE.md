# 工具包架构设计文档

## 核心概念

### 1. 工具（Tool）

工具是系统的基本执行单元，每个工具完成一个特定任务。

```python
class Tool(ABC):
    name: str              # 工具名称（唯一标识）
    description: str       # 工具描述（给 LLM 看）
    when: str              # 使用场景（帮助 LLM 选择）
    
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具逻辑"""
```

**示例**：
- `create_form`: 创建表单
- `modify_form`: 修改表单
- `chat`: 闲聊回复

### 2. 复合工具（CompositeTool）

复合工具封装多步骤链路，每个步骤是一个独立的方法。

```python
class CompositeTool(Tool):
    steps: list[str] = []  # 步骤名称列表
    
    def run_pipeline(self, state: dict, ctx: ToolContext) -> None:
        """按顺序执行所有步骤"""
        for step_name in self.steps:
            method = getattr(self, f"_step_{step_name}")
            method(state, ctx)
```

**示例**：`CreateFormTool` 的 6 步链路
```python
steps = ["fetch_guide", "list_assets", "parse_fields", 
         "fetch_templates", "generate", "validate"]

def _step_fetch_guide(self, state, ctx):
    ctx.emit("stage", "fetch_guide", "正在获取配置指南...")
    state["guide"] = ctx.asset_client.get_guide()

def _step_parse_fields(self, state, ctx):
    ctx.emit("stage", "parse_fields", "AI 正在解析字段...")
    # LLM 解析逻辑
```

### 3. 工具上下文（ToolContext）

工具执行时的依赖注入容器：

```python
class ToolContext:
    llm_client: LLMClient        # LLM 调用
    asset_client: AssetClient    # 资源获取（模板、Schema）
    conversation: Conversation   # 对话管理
    emit: Callable               # SSE 进度回调
    conv_id: str                 # 对话 ID
```

### 4. 工具结果（ToolResult）

工具执行后的返回结果：

```python
class ToolResult:
    artifact: dict      # 结构化结果（存入对话历史）
    summary: str        # 摘要（用于对话历史）
    reply: str          # 回复消息（显示给用户）
    ask: AskSpec        # 追问规格（如果需要用户确认）
    extra: dict         # 额外数据（给前端用）
```

## 链路定义

### 单步骤链路

简单工具只有一个 `execute` 方法：

```python
class ChatTool(Tool):
    name = "chat"
    description = "闲聊回复"
    when = "用户打招呼、闲聊时"
    
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        user_input = state.get("user_input", "")
        reply = ctx.llm_client.chat([
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": user_input}
        ])
        return ToolResult(reply=reply, summary=reply)
```

### 多步骤链路

复杂工具使用 `CompositeTool` 定义多步骤：

```python
class CreateFormTool(CompositeTool):
    name = "create_form"
    description = "创建表单"
    when = "用户想创建新表单时"
    
    steps = ["fetch_guide", "list_assets", "parse_fields", 
             "fetch_templates", "generate", "validate"]
    
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        self.run_pipeline(state, ctx)
        return ToolResult(
            artifact=state.get("artifact"),
            summary="表单已生成"
        )
    
    def _step_fetch_guide(self, state, ctx):
        ctx.emit("stage", "fetch_guide", "正在获取配置指南...")
        state["guide"] = ctx.asset_client.get_guide()
    
    def _step_parse_fields(self, state, ctx):
        ctx.emit("stage", "parse_fields", "AI 正在解析字段...")
        # LLM 解析逻辑
    
    def _step_generate(self, state, ctx):
        ctx.emit("stage", "generate", "AI 正在生成配置...")
        # LLM 生成逻辑
    
    def _step_validate(self, state, ctx):
        ctx.emit("stage", "validate", "正在校验...")
        # 校验逻辑
```

### SSE 事件流

每个步骤通过 `ctx.emit()` 发送进度事件：

```python
# 发送阶段事件
ctx.emit("stage", "parse_fields", "AI 正在解析字段...")

# 发送完成事件
ctx.emit("stage", "parse_fields_done", "已解析 4 个字段")

# 发送错误事件
ctx.emit("stage", "validate_fail", "校验失败：字段类型错误")
```

前端接收 SSE 事件并更新 UI：
```javascript
event: stage
data: {"stage": "parse_fields", "message": "AI 正在解析字段..."}

event: stage
data: {"stage": "parse_fields_done", "message": "已解析 4 个字段"}

event: result
data: {"config": {...}, "summary": "表单已生成"}

event: done
data: {"status": "done"}
```

## 路由机制

### 1. 意图识别

用户消息进入系统后，LLM 根据工具的 `name`、`description`、`when` 选择工具：

```python
# 构建工具描述
tools_desc = """
可用工具:
- create_form: 创建表单 (适用: 用户想创建新表单时)
- modify_form: 修改表单 (适用: 用户想修改已有表单时)
- chat: 闲聊回复 (适用: 用户打招呼、闲聊时)
"""

# LLM 选择工具
prompt = f"""
根据用户消息，选择最合适的工具。

{tools_desc}

用户消息: {user_input}

请输出 JSON: {{"tool": "工具名称", "reason": "选择理由"}}
"""

selected = llm_client.chat_json(prompt)
tool_name = selected["tool"]
```

### 2. 工具执行

选中的工具执行 `execute()` 方法：

```python
# 获取工具
tool = registry.get(tool_name)

# 构建上下文
ctx = ToolContext(
    llm_client=llm_client,
    asset_client=asset_client,
    conversation=conversation,
    emit=emit_callback,
    conv_id=conv_id
)

# 执行工具
result = tool.execute(state, ctx)
```

### 3. 结果处理

根据 `ToolResult` 的类型处理结果：

```python
if result.ask:
    # 需要追问用户
    send_clarification(result.ask)
elif result.reply:
    # 闲聊回复
    send_reply(result.reply)
elif result.artifact:
    # 配置生成/修改
    send_config(result.artifact)
```

## 完整流程示例

### 场景：用户说"创建一个请假申请表"

**1. 意图识别**
```
用户消息: "创建一个请假申请表"
↓
LLM 分析:
  - create_form: 适用（用户想创建新表单）
  - modify_form: 不适用（没有已有表单）
  - chat: 不适用（不是闲聊）
↓
选择工具: create_form
```

**2. 工具执行**
```
CreateFormTool.execute(state, ctx)
↓
run_pipeline(state, ctx)
  ├─ _step_fetch_guide()
  │   └─ emit("stage", "fetch_guide", "正在获取配置指南...")
  ├─ _step_list_assets()
  │   └─ emit("stage", "list_assets", "正在获取模板列表...")
  ├─ _step_parse_fields()
  │   └─ emit("stage", "parse_fields", "AI 正在解析字段...")
  │   └─ LLM 解析: [申请人, 请假类型, 开始日期, 结束日期]
  ├─ _step_fetch_templates()
  │   └─ emit("stage", "fetch_templates", "正在匹配模板...")
  ├─ _step_generate()
  │   └─ emit("stage", "generate", "AI 正在生成配置...")
  │   └─ LLM 生成: FormConfig JSON
  └─ _step_validate()
      └─ emit("stage", "validate", "正在校验...")
      └─ 校验通过
↓
返回 ToolResult(artifact=FormConfig, summary="表单已生成")
```

**3. 结果处理**
```
ToolResult.artifact 存在
↓
发送配置到前端
↓
保存对话历史
```

## 扩展新工具包

### 步骤 1：创建工具包目录

```bash
cd backend/src/domains
mkdir calendar_pack
cd calendar_pack
touch __init__.py pack.py
mkdir tools
touch tools/__init__.py
```

### 步骤 2：实现工具

```python
# tools/create_event.py
from sdk.tool import Tool, ToolResult, ToolContext

class CreateEventTool(Tool):
    name = "create_event"
    description = "创建日历事件"
    when = "用户想安排会议、提醒、约会时"
    
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "time": {"type": "string"}
            },
            "required": ["title", "time"]
        }
    
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        title = state.get("title")
        time = state.get("time")
        
        ctx.emit("stage", "create_event", f"正在创建事件: {title}")
        
        # 调用日历 API
        event_id = calendar_api.create_event(title, time)
        
        ctx.emit("stage", "create_event_done", "事件已创建")
        
        return ToolResult(
            artifact={"eventId": event_id, "title": title, "time": time},
            summary=f"已创建事件: {title}",
            reply=f"好的，我已经为您创建了事件：{title}，时间：{time}"
        )
```

### 步骤 3：注册工具

```python
# pack.py
from sdk.registry import ToolRegistry
from .tools.create_event import CreateEventTool

def create_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(CreateEventTool())
    return registry
```

### 步骤 4：自动加载

系统启动时自动发现并加载 `calendar_pack`，无需修改 `main.py`。

## 高级特性

### 1. 追问机制

工具可以要求用户提供更多信息：

```python
from sdk.tool import AskSpec, AskQuestion, AskOption

def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
    if not state.get("time"):
        return ToolResult(
            ask=AskSpec(
                questions=[
                    AskQuestion(
                        question="请问会议时间是？",
                        header="会议时间",
                        options=[
                            AskOption(label="明天上午", description="明天 9:00-10:00"),
                            AskOption(label="明天下午", description="明天 14:00-15:00")
                        ]
                    )
                ]
            )
        )
```

### 2. 重试机制

复合工具可以实现重试逻辑：

```python
def _step_validate(self, state, ctx):
    result = validate(state["artifact"])
    
    if not result.valid and state.get("retry_count", 0) < 3:
        state["retry_count"] = state.get("retry_count", 0) + 1
        ctx.emit("stage", "validate_retry", f"校验失败，重试第 {state['retry_count']} 次")
        self._step_generate(state, ctx)  # 重新生成
        return self._step_validate(state, ctx)  # 重新校验
```

### 3. 错误处理

工具可以返回错误信息：

```python
def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
    try:
        result = do_something()
        return ToolResult(artifact=result)
    except Exception as e:
        return ToolResult(
            error_for_llm=str(e),
            summary=f"执行失败: {e}"
        )
```

## 总结

**链路定义**：
- 单步骤：实现 `Tool.execute()`
- 多步骤：继承 `CompositeTool`，定义 `steps` 列表

**SSE 事件**：
- 使用 `ctx.emit("stage", name, message)` 发送进度
- 前端接收并更新 UI

**路由机制**：
- LLM 根据工具的 `name`、`description`、`when` 选择工具
- 选中的工具执行 `execute()` 方法
- 根据 `ToolResult` 类型处理结果

**扩展方式**：
- 创建独立工具包目录
- 实现 `pack.py` 和工具类
- 系统自动发现和加载
