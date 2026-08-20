"""PackRouter —— pack 级二级路由协议与默认实现。

【背景：两级路由架构】
意图识别分两级，领域知识的归属由此修正：

  引擎一级路由（领域无关）："这个请求属于哪个领域（pack）？"
      —— 候选来自各 pack 的 manifest domain 声明，引擎不认识任何业务词。
  pack 二级路由（本模块）："这个领域内该用哪个工具？"
      —— 「画布有内容（含未保存草稿）+ 增量话术 = 修改类工具」这类判断
      是领域知识，归 pack 自己（此前写在引擎意图规则里，是泄漏）。

【兼容设计】
- pack 不提供 create_router 时用 DefaultPackRouter（行为与旧引擎扁平路由
  一致的中性实现）——下个插件零成本接入；
- 单 pack 部署时引擎一级路由直通（不产生额外 LLM 调用）。

【Java 类比】
PackRouter ≈ Spring 的 HandlerMapping：请求 → 处理器的映射策略可插拔，
DispatcherServlet（引擎）只管流程，映射规则归策略实现。
"""
from typing import Optional, Protocol

from sdk.registry import ToolRegistry


class PackRouter(Protocol):
    """pack 二级路由协议：在本 pack 的工具集合里选一个。

    实现方（pack 自带或 DefaultPackRouter）负责把「用户消息 + 画布状态 +
    对话历史」映射到工具名。返回 None 表示无合适工具（由调用方决定兜底）。
    """

    def route(self, user_input: str, artifact: Optional[dict],
              history: str = "", llm_client=None) -> Optional[str]:
        ...


class DefaultPackRouter:
    """默认二级路由：中性框架文本 + 工具 when 描述（原引擎扁平路由的下沉版）。

    不含任何领域知识——工具怎么选完全由各工具的 `when` 描述承载。
    pack 想要自己的领域规则（如"画布有字段则倾向修改类"）时提供
    create_router() 覆盖本实现。
    """

    def __init__(self, registry: ToolRegistry):
        """保存本 pack 的工具集（build_prompt 遍历它拼候选清单）。"""
        self._registry = registry

    def build_prompt(self, has_artifact: bool) -> str:
        """构建二级路由 prompt（protected：领域路由子类可复用骨架换规则）。

        工具是否需要画布等适用条件由各工具的 when 描述自行表达（领域语言），
        框架不做属性级标注——需要精细规则的 pack 覆写本方法（见 njmind router）。
        """
        tools_list = "\n".join(f"- {t.name}: {t.when}" for t in self._registry.all())

        return (
            "你是工具路由器。根据用户消息从本领域的候选工具中选择最合适的，只返回 JSON。\n\n"
            "规则：\n"
            "1. 没有可匹配工具时返回 null。\n"
            "2. 只返回 JSON，不要解释。\n\n"
            f"候选工具:\n{tools_list}\n\n"
            f"当前 has_artifact={str(has_artifact).lower()}\n"
            '输出格式: {"tool": "tool_name"} （无合适工具则 {"tool": null}）'
        )

    def route(self, user_input: str, artifact: Optional[dict],
              history: str = "", llm_client=None) -> Optional[str]:
        """LLM 二级路由。无 llm_client 时退化为首个工具（测试/降级场景）。"""
        if llm_client is None:
            tools = self._registry.all()
            return tools[0].name if tools else None

        import json
        parts = []
        if history:
            parts.extend(["## 对话历史", history, ""])
        parts.extend([
            f"## 画布是否已有内容：{'是' if artifact is not None else '否'}",
            "",
            "## 用户消息",
            user_input,
            "",
            "请选择工具，输出 JSON。",
        ])
        messages = [
            {"role": "system", "content": self.build_prompt(artifact is not None)},
            {"role": "user", "content": "\n".join(parts)},
        ]
        parsed = llm_client.chat_json(messages)
        name = parsed.get("tool") if isinstance(parsed, dict) else None
        # 校验名字真实存在（LLM 可能编造）——不存在视为无匹配
        if name and any(t.name == name for t in self._registry.all()):
            return name
        return None
