# 工具包（Pack）开发指南

## 概述

系统采用自动发现机制，自动加载 `domains/` 目录下的所有工具包。每个工具包都是独立的，只需遵循约定的接口即可被系统识别和加载。

> 本文档是最小插件形态（工具注册）的速览。完整开发指南（manifest 声明/依赖门控/
> 插件 HTTP API/后台任务/SDK 存储设施/管理页）见 `doc/插件开发与嵌入指南.md`
> 与 `ARCHITECTURE.md`（本目录）;全能力插件参考 `knowledge_graph/`。

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

pack.py 还可以实现这些**可选钩子**（平台在装配/卸载期调用）:

| 钩子 | 作用 |
|------|------|
| `create_api_router()` | 插件自有 HTTP API（挂 `/api/packs/{name}` 前缀,启停热挂卸） |
| `register_tasks(mgr, app_state)` | 注册后台任务 handler（任务中心可见/可取消） |
| `unload()` | 清理（释放连接/注销 SDK 前缀等） |

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
├── njmind_form/             # 表单工具包
│   ├── __init__.py
│   ├── pack.py              # 必需：导出 create_registry()
│   ├── router.py            # 领域二级路由
│   ├── config.yaml          # manifest
│   ├── tools/
│   └── prompts/
├── leave_application/       # 请假申请插件（演示域）
├── knowledge_graph/         # 知识图谱插件（全能力形态示例）
│   ├── pack.py              # 四钩子齐备
│   ├── config.yaml          # manifest（依赖声明/管理页/设置页）
│   ├── settings.schema.yaml # 声明式设置页
│   ├── api.py               # 插件 HTTP API
│   ├── tasks.py             # 后台任务（导入流水线）
│   ├── store.py / stores.py # 领域元数据 + SDK 存储适配层
│   ├── retrieval.py         # 混合检索问答
│   └── probes.py            # 依赖探针
└── my_pack/                 # 你的工具包
    ├── __init__.py
    ├── pack.py              # 必需
    └── tools/
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

参考 `domains/knowledge_graph/`（全能力形态：工具+HTTP API+后台任务+依赖门控+
管理页+SDK 存储）或 `domains/njmind_form/`（工具形态）。

## 最佳实践

1. **单一职责**: 每个工具只负责一件事
2. **清晰命名**: 工具名称和描述要清晰明确
3. **错误处理**: 在 execute 中捕获异常并返回友好错误
4. **进度反馈**: 使用 `ctx.emit()` 报告执行进度
5. **日志记录**: 使用 `logger` 记录关键操作
6. **底层调用走 ctx**: LLM/上游/打点必须走 ctx 通道（可观测是平台能力,见插件指南 §2.2）
7. **长活走任务框架**: 分钟级操作用 `register_tasks` + `queue_key` 串行,不要占请求线程
8. **外部存储走 SDK**: 图/向量/文档解析用 `sdk/graph_store` 等设施,前缀先登记后使用
9. **自有设施调用自记日志**: 图/向量库等调用写 `call_logs`(call_type 自定义如 graph/vector),
   与 llm/upstream 同表同视图(参考 `knowledge_graph/retrieval.py` 的 `_log_retrieval_call`)

## 测试工具包

```bash
# 后端测试(425 条全量)
cd backend
./venv/bin/python -m pytest tests/ -q

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

**Q: 插件显示"依赖未配置"？**
A: manifest 声明了 `dependencies` 的插件（如 knowledge_graph 需要 Neo4j/Milvus）,
配好 env 或管理端→插件→设置里补配,再点"重新检测"热加载。

**Q: 如何使用 LLM？**
A: 在 execute 中使用 `ctx.llm_client`：
```python
response = ctx.llm_client.chat([
    {"role": "system", "content": "你是一个助手"},
    {"role": "user", "content": state.get("user_input")}
])
```
