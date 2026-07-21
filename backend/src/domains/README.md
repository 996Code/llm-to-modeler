# 工具包（Pack）开发指南

## 概述

系统采用自动发现机制，自动加载 `domains/` 目录下的所有工具包。每个工具包都是独立的，只需遵循约定的接口即可被系统识别和加载。

## 快速开始

### 1. 创建工具包目录结构

```bash
cd backend/src/domains
mkdir my_pack
cd my_pack
touch __init__.py pack.py
```

### 2. 实现 pack.py

```python
"""My Pack - 工具包描述"""
from sdk.registry import ToolRegistry
from engine.prompt_loader import PromptLoader
from .tools.my_tool import MyTool

def create_registry() -> ToolRegistry:
    """创建工具注册表"""
    registry = ToolRegistry()
    registry.register(MyTool())
    return registry

def create_prompt_loader() -> PromptLoader:
    """创建提示词加载器"""
    return PromptLoader()
```

### 3. 实现工具类

创建 `tools/my_tool.py`：

```python
from sdk.tool import Tool, ToolResult, ToolContext

class MyTool(Tool):
    name = "my_tool"
    description = "工具描述"
    when = "何时使用此工具"
    
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string"}
            },
            "required": ["user_input"]
        }
    
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        user_input = state.get("user_input", "")
        # 实现工具逻辑
        result = f"处理结果: {user_input}"
        
        return ToolResult(
            artifact={"result": result},
            summary=result,
            reply=result
        )
```

### 4. 完成！

系统会自动发现并加载你的工具包，无需修改 `main.py`。

## 目录结构规范

```
domains/
├── __init__.py              # 自动发现机制（已实现）
├── njmind_form/            # 示例：表单工具包
│   ├── __init__.py
│   ├── pack.py             # 必需：导出 create_registry()
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── create_form.py
│   │   ├── modify_form.py
│   │   └── chat.py
│   └── prompts/
│       └── ...
└── my_pack/                # 你的工具包
    ├── __init__.py
    ├── pack.py             # 必需
    └── tools/
        ├── __init__.py
        └── my_tool.py
```

## 核心接口

### ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None:
        """注册工具"""
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
    
    def all(self) -> List[Tool]:
        """获取所有工具"""
```

### Tool

```python
class Tool(ABC):
    name: str                    # 工具名称（唯一）
    description: str             # 工具描述
    when: str                    # 使用场景
    
    @abstractmethod
    def input_schema(self) -> dict:
        """定义输入参数 schema"""
    
    @abstractmethod
    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行工具逻辑"""
```

### ToolContext

```python
class ToolContext:
    llm_client: LLMClient        # LLM 客户端
    asset_client: AssetClient    # 资源客户端
    conversation: Conversation   # 对话管理
    emit: Callable               # 进度回调
    conv_id: str                 # 对话 ID
```

### ToolResult

```python
class ToolResult:
    artifact: Optional[dict]     # 结构化结果
    summary: str                 # 摘要
    reply: Optional[str]         # 回复消息
    ask: Optional[AskSpec]       # 追问规格
    extra: dict                  # 额外数据
```

## 完整示例

参考 `domains/njmind_form/` 实现：

- **pack.py**: 注册 3 个工具（create_form, modify_form, chat）
- **tools/**: 每个工具独立文件
- **prompts/**: Jinja2 提示词模板

## 最佳实践

1. **单一职责**: 每个工具只负责一件事
2. **清晰命名**: 工具名称和描述要清晰明确
3. **错误处理**: 在 execute 中捕获异常并返回友好错误
4. **进度反馈**: 使用 `ctx.emit()` 报告执行进度
5. **日志记录**: 使用 `logger` 记录关键操作

## 测试工具包

```bash
# 重启后端
cd backend
pkill -f "uvicorn src.main:app"
PYTHONPATH=src nohup ./venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 18081 > /tmp/backend.log 2>&1 &

# 查看日志，确认工具包已加载
tail -f /tmp/backend.log | grep "发现工具包"
```

## 常见问题

**Q: 工具包没有被发现？**
A: 检查：
1. 目录名不以 `_` 开头
2. 包含 `pack.py` 文件
3. `pack.py` 导出了 `create_registry` 函数

**Q: 工具注册失败？**
A: 检查：
1. 工具类正确继承 `Tool`
2. 实现了所有必需方法
3. 工具名称唯一

**Q: 如何使用 LLM？**
A: 在 execute 中使用 `ctx.llm_client`：
```python
response = ctx.llm_client.chat([
    {"role": "system", "content": "你是一个助手"},
    {"role": "user", "content": state.get("user_input")}
])
```
