"""
MCP（Model Context Protocol）服务模块。

把本服务的"表单配置生成/校验"能力以 MCP 工具（Tools）和资源（Resources）的形式
暴露给外部 AI 客户端（如 Claude Code、Cursor），让这些客户端能调用本服务的能力。

核心概念（Java 视角）：
  - MCP：类比一种"AI 领域的 RPC 协议"，专门给 AI 客户端调用外部工具用。
    客户端通过 MCP 握手发现有哪些 tool/resource，再按需调用。
  - FastMCP：MCP 官方的 Python 服务端框架，类比 Spring 的 @RestController，
    用 @mcp.tool() / @mcp.resource() 装饰器注册对外能力。
  - Tool：可调用的"函数"，类比 RPC 方法。AI 客户端传入参数，返回结果字符串。
  - Resource：可读取的"数据"，类比 REST 的 GET 资源。AI 客户端按 URI 读取。

暴露的工具（Tools）：
  get_form_config(description)    → 由自然语言生成 FormConfig
  validate_form(config)           → 调用上游做校验
  list_templates()                → 列出可用模板
  get_template(name)              → 取模板 JSON
  get_guide()                     → 取填写指南 JSON

暴露的资源（Resources）：
  njmind://guide                  → 填写指南 JSON
  njmind://templates              → 模板列表
"""

import json
import logging
import uuid
from typing import Any

# FastMCP：MCP 官方 Python SDK，提供装饰器式工具/资源注册
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def create_mcp_server(upstream, graph=None):
    """创建 MCP 服务实例并注册所有工具和资源。

    用工厂函数（而非全局单例）创建，类比 Spring 的 @Bean 工厂方法，
    可以显式注入 upstream 和 graph 依赖。

    Args:
        upstream: UpstreamClient 实例，用于调用上游 njmind-modeler（校验/模板等）。
        graph: 已编译的 LangGraph StateGraph 实例，用于自然语言生成表单配置。
               为 None 时，get_form_config 工具会返回错误（降级）。

    Returns:
        FastMCP 实例，调用方挂到 HTTP 路径（main.py 里 app.mount("/mcp", ...)）。
    """
    # 创建 MCP 服务，名字会出现在客户端的工具列表里
    mcp = FastMCP("llm-form-modeler")

    # ── Tools（可调用的工具函数）──────────────────────────────────

    @mcp.tool()
    def get_form_config(description: str) -> str:
        """根据自然语言描述生成表单配置 JSON。

        这是本服务最核心的工具：AI 客户端传入一段自然语言需求，
        返回一个可用的 FormConfig。

        核心逻辑：
          1. 构造 LangGraph 初始状态（模拟一次完整对话的输入）
          2. graph.invoke 同步执行图：意图分类 → 工具执行 → 结果处理
          3. 从结果状态提取 tool_result，转成 JSON 字符串返回
          4. graph 未配置 / 执行异常 / 无结果时，返回降级错误 JSON（不抛异常）

        Fail-Closed 设计：所有错误路径都返回 {"error": ...} JSON，
        不抛异常给 MCP 框架（避免客户端收到无法解析的错误）。

        Args:
            description: 自然语言表单描述，如 "创建一个请假申请表，包含申请人、日期、原因"。

        Returns:
            JSON 字符串：
              成功：{"config": {...}, "valid": bool, "errors": [...]}
              失败：{"error": "..."}
        """
        # graph 未注入时降级（Fail-Closed，返回错误而非抛异常）
        if not graph:
            return json.dumps({"error": "Graph not configured"}, ensure_ascii=False)

        # 每次调用生成独立 thread_id，保证 LangGraph 会话隔离（线程本地状态不串）
        thread_id = str(uuid.uuid4())
        # 构造 LangGraph 初始状态：所有字段必须给齐（StateGraph 要求）
        # 这些字段对应 nodes.py 里 State 的所有键
        input_data = {
            "user_input": description,
            "conversation_history": [],      # 全量对话历史（MCP 单轮调用为空）
            "compressed_history": "",        # 压缩后的历史摘要（首轮为空）
            "conversation_id": f"mcp_{thread_id}",
            "forward_headers": {},           # 透传给上游的鉴权头（MCP 无外部用户，空）
            "current_config": None,          # 已有配置（编辑场景用，生成场景为空）
            "tool_name": "",                 # 由 classify_intent 节点填入
            "intent_reason": "",             # 意图分类的 LLM 推理过程
            "tool_state": {},                # 工具内部状态机
            "tool_result": None,             # 最终产物（节点填充）
            "pending_questions": [],         # 需要追问用户的问题（澄清用）
            "clarify_answers": {},           # 用户对澄清问题的回答
            "sse_events": [],                # SSE 事件收集（MCP 模式不用，节点会忽略）
        }
        # LangGraph 的线程配置：thread_id 用于状态持久化与隔离
        config = {"configurable": {"thread_id": f"mcp_{thread_id}"}}

        try:
            # 同步执行整个图：classify_intent → execute_tool → handle_result
            result_state = graph.invoke(input_data, config)
        except Exception as e:
            # Fail-Closed：图执行异常时返回错误 JSON，不向上抛
            logger.exception("Graph invocation failed")
            return json.dumps({"error": f"Graph invocation failed: {e}"}, ensure_ascii=False)

        # 从最终状态提取工具产物
        tool_result = result_state.get("tool_result")
        if tool_result is None:
            return json.dumps({"error": "No config generated"}, ensure_ascii=False)

        # 优先返回生成的 artifact（表单配置）
        # extra.validation_errors 存的是上游校验发现的问题列表
        if tool_result.artifact:
            return json.dumps({
                "config": tool_result.artifact,
                "valid": len(tool_result.extra.get("validation_errors", [])) == 0,
                "errors": tool_result.extra.get("validation_errors", []),
            }, ensure_ascii=False, indent=2)
        # 无 artifact 但有错误信息（如 LLM 判断无法生成），返回给 LLM 的错误文案
        elif tool_result.error_for_llm:
            return json.dumps({"error": tool_result.error_for_llm}, ensure_ascii=False)
        return json.dumps({"error": "No config generated"}, ensure_ascii=False)

    @mcp.tool()
    def validate_form(config: str, mode: str = "CREATE") -> str:
        """通过上游 API 校验表单配置。

        让 AI 客户端在不经过 LangGraph 的情况下，直接调上游校验某个配置。
        适用于"客户端已有配置，想检查是否合法"的场景。

        Args:
            config: FormConfig JSON 字符串（客户端传入的是字符串而非对象，
                    因为 MCP 工具参数只支持基本类型，复杂结构用 JSON 字符串传）。
            mode: 校验模式，"CREATE"（新建）或 "UPDATE"（更新），默认 CREATE。

        Returns:
            校验结果 JSON 字符串：{valid, errors, warnings}。
            传入非法 JSON 时返回 {valid: False, error: ...}（Fail-Closed）。
        """
        try:
            form_config = json.loads(config)
        except json.JSONDecodeError as e:
            # 客户端传入非法 JSON：返回明确的失败结果，不抛异常
            return json.dumps({"valid": False, "error": f"Invalid JSON: {e}"})

        result = upstream.validate_form(form_config, mode=mode)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_templates() -> str:
        """列出上游所有可用模板名。"""
        templates = upstream.list_templates()
        return json.dumps(templates, ensure_ascii=False)

    @mcp.tool()
    def get_template(name: str) -> str:
        """按名称从上游获取模板 JSON。

        Args:
            name: 模板名（如 "simple_form"），可不带 .json 后缀。
        """
        template = upstream.get_template(name)
        if not template:
            return json.dumps({"error": f"Template '{name}' not found"})
        return json.dumps(template, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_guide() -> str:
        """从上游获取字段类型填写指南。"""
        guide = upstream.get_guide()
        if not guide:
            return json.dumps({"error": "Guide not found"})
        return json.dumps(guide, ensure_ascii=False, indent=2)

    # ── Resources（可读取的数据资源，按 URI 访问）──────────────────

    @mcp.resource("njmind://guide")
    def guide_resource() -> str:
        """字段类型填写指南和关键词索引资源。

        AI 客户端通过 URI `njmind://guide` 读取，作为上下文增强生成质量。
        与 tool 不同，resource 是"被动数据"，客户端按需读取。
        """
        guide = upstream.get_guide()
        return json.dumps(guide or {}, ensure_ascii=False, indent=2)

    @mcp.resource("njmind://templates")
    def templates_resource() -> str:
        """可用模板列表资源。"""
        return json.dumps(upstream.list_templates(), ensure_ascii=False)

    return mcp
