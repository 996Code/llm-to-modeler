"""GraphState — LangGraph StateGraph 的状态定义。

模块定位
========

本模块定义整条 LangGraph 图共享的状态 schema(只有一个类 ``GraphState``)。
所有节点函数 ``(state: GraphState) -> dict`` 读这同一份 schema 的字段、
返回增量字段,框架自动 merge 回 state。这是图节点之间唯一的“通信总线”。

替代旧 ``graph/state.py`` 的 ``AgentState``,适配多工具插件化架构:
- 不再硬编码表单字段(guide/template_names 等):每个工具自己定义内部 state
- 工具内部状态通过 ``tool_state`` 透传,Graph 层不读它的内部结构(开闭原则)
- 支持 LangGraph interrupt/restore 的追问机制(暂停 → 问用户 → 恢复)

状态流转
========

::

    START → classify_intent → execute_tool → handle_result → END
                                  ↑  interrupt(追问)  ↓
                                  └─── resume ────────┘

每个箭头经过后,LangGraph 都会把当前 GraphState 快照到 checkpointer。

Java 类比
========

- ``GraphState`` ≈ Spring 的 ``@SessionAttributes`` / 流程引擎的 ProcessVariables
  / Redux 的 Store shape。
- TypedDict ≈ Java 的 record/POJO,但只做静态类型提示,运行期就是普通 ``dict``。
- 节点返回增量而非全量 ≈ Redux reducer 返回 partial state,框架做 spread merge。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

# ToolResult:工具执行的标准化返回类型,工具内部产出它,Graph 把它序列化进 tool_result
from sdk.tool import ToolResult


class GraphState(TypedDict, total=False):
    """LangGraph 图状态(整条图的共享数据总线)。

    设计模式:状态对象 / Blackboard Pattern。所有节点读写同一块共享状态,
    而不是相互传参或维护私有权重。Java 类比:Activiti BPMN 的 ProcessVariables,
    或一个带若干字段的 ``@Data`` POJO 当作流程变量容器。

    ── 为什么用 ``TypedDict + total=False`` ──

    * ``TypedDict``:给 Python ``dict`` 加静态类型提示,IDE / mypy 能检查字段拼写和类型,
      但运行期仍是普通 dict(不带方法、不可变约束)。Java 类比:像用 ``Map<String,Object>``
      但带类型注解,介于 Map 和 POJO 之间。
    * ``total=False``:声明**所有字段可选**。这是关键:LangGraph 的节点只返回**增量**
      (它改了哪些字段就返回哪些),没有节点会一次返回全部字段。若不设 ``total=False``,
      mypy 会要求构造时填满所有字段,节点函数就写不出来了。
    * **不用 Pydantic BaseModel 的 WHY**:LangGraph 的 channel 机制要求 state 是
      dict-like(走 ``dict.update`` 风格的 reducer 合并),Pydantic v1 BaseModel 不是
      ``Mapping`` 子类,合并语义对不上。dict 是最贴合框架的数据结构。
    * 字段类型注解同时被 LangGraph 用来推断每个 channel 的 reducer(默认是“后写覆盖前写”,
      也可声明 ``Annotated[list, add_messages]`` 之类的累加 reducer,本 schema 用默认覆盖语义)。

    字段分组(按生命周期/职责划分,顺序 = 数据流向)
    ============================================

    ── 输入(会话开始时由调用方注入,多轮间复用) ──
    - user_input:           本轮用户消息原文
    - conversation_history: 对话历史 [{role, content}] 注入 LLM 上下文
    - compressed_history:   压缩后的历史文本(节省 token,对标 Claude Code 的 history compression)
    - conversation_id:      会话 ID,作为 checkpoint 的 thread_id(决定取哪份快照)
    - forward_headers:      嵌入(embed)模式透传的请求头(如上游租户、trace id)
    - context_artifact:     对话的上下文制品(宿主下发的画布,pack 路由判断画布状态,
                            modify 类工具读它做增量基线;存储层字段名仍叫 current_config)

    ── 意图识别(classify_intent 节点产出) ──
    - tool_name:            选中的工具名(决定路由到哪个工具)
    - intent_reason:        LLM 给出的判断理由(可观测/调试/审计用)

    ── 工具执行(execute_tool 节点产出) ──
    - tool_state:           工具内部 state(透传,Graph 不读内部结构,开闭原则)
    - tool_result:          工具执行结果(ToolResult 序列化后的 dict)

    ── 追问(LangGraph interrupt/restore 的握手字段) ──
    - pending_questions:    interrupt 的 value(工具向用户提的追问问题列表)
    - clarify_answers:      resume 的 value(用户回答,框架恢复时注入)

    ── SSE 事件(handle_result 等节点累积) ──
    - sse_events:           节点产出的事件列表,由 stream.py 消费推给前端
    """

    # ── 输入 ──
    # 本轮用户输入,贯穿全流程;classify_intent 据此判断意图
    user_input: str
    # 历史消息列表,每条 {role, content},注入 prompt 拼接成对话上下文
    conversation_history: List[Dict[str, str]]
    # 压缩后的历史(长会话超过 token 阈值时由压缩器产出),替代 conversation_history 节省 token
    compressed_history: str
    # 会话 ID;同时用作 LangGraph checkpoint 的 thread_id,保证同一会话取同一份快照
    conversation_id: str
    # 嵌入(embed)模式下上游透传的请求头,工具可能需要(如多租户 token)
    forward_headers: Dict[str, str]
    # 对话的上下文参数(宿主下发的当前制品)——pack 路由据此判断画布状态,
    # 修改类工具读它做增量基线。结构由 pack 各自消化(引擎不解析内部字段)。
    context_artifact: Optional[Dict[str, Any]]

    # ── 意图识别(classify_intent 节点写) ──
    # 选中的工具名;条件边 route_by_tool 据此决定走 execute_tool 还是 END
    tool_name: str
    # LLM 给出的判断理由;落日志/审计用,帮助排查“为什么走错工具”
    intent_reason: str

    # ── 工具执行(execute_tool 节点写) ──
    # 工具私有 state,Graph 层不解析其内部结构(开闭原则:新工具加字段不动 Graph)
    tool_state: Dict[str, Any]
    # 工具执行结果,ToolResult.model_dump() 序列化后的 dict;handle_result 据此产出 SSE 事件
    tool_result: Optional[Dict[str, Any]]  # ToolResult.model_dump()

    # ── 追问(LangGraph interrupt/restore 握手) ──
    # interrupt(value=pending_questions) 的 value:工具向用户提的追问问题列表,图在此暂停
    pending_questions: List[Dict[str, Any]]
    # Command(resume=clarify_answers) 的 value:用户回答,框架恢复时注入工具
    clarify_answers: Dict[str, Any]

    # ── SSE 事件(节点累积,stream.py 消费) ──
    # 各节点追加的事件列表,前端按顺序消费;类比 Java 里生产者往 BlockingQueue 放事件
    sse_events: List[Dict[str, Any]]
