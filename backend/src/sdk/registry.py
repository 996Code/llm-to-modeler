"""ToolRegistry — 工具注册表。

【模块定位】
本模块是整个 Agent 系统的"工具仓库",集中持有所有可被 LLM 调用的工具实例。
pack(工具包)启动时调用 register(...) 把工具登记进来;dispatcher 从这里
取出工具清单生成给 LLM 的"可用工具"提示词,并根据 LLM 选择的名字查找对应实例。

【核心设计】
- 单一实例(ToolRegistry 在一个 Engine 进程中只 new 一次),全局共享。
- 内部用 dict 按 tool.name 做唯一索引 —— 类似 Java 里 Map<String, Tool>。
- 工具注册是静态的(在 pack 启动时一次性完成),运行期只读不改,
  因此不需要考虑并发写,本质是一个"启动期构建、运行期只读"的注册表。

【Java 类比】
对标 Spring 的 BeanFactory / ApplicationContext:启动时把 Bean 收集起来,
运行时按名字 getBean。区别是这里没有依赖注入,工具所需的依赖(llm_client、
asset_client 等)不在注册时注入,而是在 execute 时通过 ToolContext 由 Engine
临时注入(类似方法参数传递,而非构造器注入)。

【关键约定】
- tool.name 必须全 Registry 内唯一(后注册的同名工具会覆盖先注册的)。
- describe_for_llm() 负责把工具清单序列化成给大模型看的中文文本。
"""
from typing import Optional
from sdk.tool import Tool


class ToolRegistry:
    """工具注册表 —— 持有所有 Tool 实例,提供查询和清单生成能力。

    【职责】
    - 注册工具(register)
    - 按名字查找工具(get)
    - 列出全部工具(all)
    - 生成给 LLM 看的工具清单文本(describe_for_llm)

    【Java 类比】
    类似一个手写的 ServiceRegistry:
        private final Map<String, Tool> tools = new HashMap<>();
    所有方法都是对这个 Map 的简单包装,没有额外魔法。
    """

    def __init__(self):
        # 内部存储:dict[str, Tool]。Python 3.9+ 支持内置泛型语法 dict[str, Tool],
        # 等价于 Java 的 Map<String, Tool>。实例属性在 __init__ 里初始化,
        # 相当于 Java 字段在构造器中赋值。
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具到注册表。

        Args:
            tool: 工具实例(Tool 的子类实例,如 CreateFormTool)。

        Note:
            以 tool.name 为 key,若同名工具已存在会被静默覆盖。
            注册发生在 pack 启动期(见 domains/*/pack.py),运行期不再修改。
        """
        # 以工具的 name 属性作为唯一 key,类似 Java 的 map.put(tool.getName(), tool)
        self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        """返回所有已注册工具的列表(副本)。

        Returns:
            list[Tool]: 工具列表。返回 list(...) 是浅拷贝,
            调用方对返回列表的增删不会影响内部 dict(但工具对象本身仍是同一引用)。

        【Java 类比】
        类似 return new ArrayList<>(tools.values()); —— 防止外部直接改内部集合。
        """
        # dict.values() 返回视图,用 list(...) 包一层转为独立列表,
        # 避免外部对列表的操作(如 append)污染内部状态。
        return list(self._tools.values())

    def get(self, name: str) -> Optional[Tool]:
        """按工具名查找工具实例。

        Args:
            name: 工具名(即 tool.name 属性,LLM 选择工具时使用的标识)。

        Returns:
            Tool 实例;若不存在返回 None。
            Optional[Tool] 等价于 Java 的 @Nullable Tool,调用方需自行判空。

        【Java 类比】
        对标 Map.getOrDefault(name, null)。
        """
        # dict.get(key) 在 key 不存在时返回 None(Python 的空值),
        # 不会抛 KeyError,这比 Java 的 map.get(name) 返回 null 更安全。
        return self._tools.get(name)

    def describe_for_llm(self, state: dict) -> str:
        """生成给 LLM 看的工具清单文本。

        把注册表里所有工具的 name / description / when 拼成一段中文清单,
        注入到意图识别 prompt 中,告诉大模型"当前可选哪些工具、分别用在什么场景"。

        Args:
            state: 当前会话状态 dict(如是否已有 artifact、当前表单配置等)。
                当前实现未使用此参数;阶段 3 将基于 state 过滤掉不可用工具
                (例如没有 artifact 时,modify 类工具不应出现在清单里)。

        Returns:
            多行字符串,形如:
                可用工具:
                - create_form: 创建表单 (适用: 用户要新建表单)
                - modify_form: 修改表单 (适用: 用户要改已有表单)
                ...

        【扩展点】
        阶段 3 增强:按 state 过滤不可用工具(如无 artifact 时禁用 modify)。
        """
        # 首行固定为标题,后续每行描述一个工具。
        lines = ["可用工具:"]
        for tool in self._tools.values():
            # f-string 是 Python 的字符串模板,等价于 Java 的 String.format
            # 或 Text Blocks 拼接。when 是工具"何时该被选用"的短描述。
            lines.append(f"- {tool.name}: {tool.description} (适用: {tool.when})")
        # 用换行符连接成单一字符串返回,prompt 模板会直接嵌入这段文本。
        return "\n".join(lines)
