"""LangGraph StateGraph 构建 + 编排。

模块定位
========

本模块是整个 LLM 编排引擎的“装配车间”:把若干个无状态的节点函数
(``nodes.classify_intent_node`` 等)按拓扑拼装成一张有向图,编译成可执行的
``CompiledStateGraph``,供上层 (``engine/api.py`` 或 stream 入口) 调用
``.stream()`` / ``.invoke()`` 驱动一轮对话。

替代旧 ``engine/dispatcher.py`` 用手写 if/else 调度,改用 LangGraph 的 StateGraph
声明式描述工具调度流程:

- classify_intent(意图识别) → route(路由判断) → execute_tool(执行工具) → handle_result(结果处理)
- interrupt/restore 追问机制:工具执行中途可暂停向用户提问,拿到回答后恢复
- checkpoint 自动持久化状态:每过一个节点自动落盘,断点续跑/双击恢复靠它

核心设计
========

1. **图即控制流**:节点之间的边(含条件边)就是 if/else + 跳转。Java 类比:
   想象一个用 Spring StateMachine 或 Activiti BPMN 流程引擎定义的流程图,
   节点是 ``Action``/``Service`` 实现,边是 transition。本模块相当于 BPMN 的
   ``BpmnModel`` 构建器——只描述拓扑,业务逻辑写在节点里。
2. **节点是无状态纯函数**:状态全部挂在 ``GraphState`` 这个 dict-like 对象上
   (见 ``graph_state.py``),节点 ``(state) -> state`` 每次读 state、算结果、
   返回增量 state,框架负责 merge。Java 类比:节点像 reducer / @MessageMapping,
   state 像 Redux Store / Spring 会话属性。
3. **checkpoint = 事务日志**:每经过一个节点,LangGraph 把 state 快照写入
   ``InMemorySaver``。崩溃/重启/追问恢复时按 ``thread_id`` 回放,类似数据库
   WAL 或 Spring Batch 的 JobRepository。

拓扑结构
========

::

    START
      │
      ▼
    classify_intent ──[route_by_tool]──┐ "tool" → execute_tool
      │                                └ "end"  → END (无可执行工具,纯闲聊)
      ▼
    execute_tool ──→ handle_result ──[route_after_result]──┐ "rerun" → execute_tool
                            ▲                              └ "done"  → END
                            │
                       (interrupt/Command(resume): 工具追问时图被挂起,
                        用户回答后从这里恢复,可能再跑一次 execute_tool)

三个节点:
  - ``classify_intent``: 调 LLM 判断用户要干什么、该用哪个工具
  - ``execute_tool``:    执行工具(含 interrupt 向用户追问)
  - ``handle_result``:   收尾:产出 SSE 事件 / 决定是否重跑

两条条件边(就是 Java 里的 if 路由):
  - ``route_by_tool``:      classify_intent 之后,有工具就走 execute_tool,没工具直接 END
  - ``route_after_result``: handle_result 之后,要重跑(如用户追问后参数变了)回 execute_tool,否则 END
"""
import logging
import os
from typing import Any, Optional

# StateGraph:LangGraph 的核心图容器,类比 Spring 的 BeanDefinition / Flow 定义
from langgraph.graph import StateGraph, START, END

from engine.graph_state import GraphState
from engine import nodes

logger = logging.getLogger(__name__)


def build_graph(
    registry: Any,
    llm_client: Any,
    asset_client: Any,
    conversation: Any = None,
    prompt_loader: Any = None,
    pack_routers: dict = None,
) -> Any:
    """构建并编译 LangGraph StateGraph。

    这是引擎装配的唯一入口:把外部依赖注入到节点模块,声明节点和边,最后编译
    成带 checkpoint 的可执行图。Java 类比:类似 ``@Bean`` 工厂方法,
    ``@Configuration`` 里把若干 ``@Component`` 装到一张流程图里返回。

    Args:
        registry: ToolRegistry 实例,工具注册中心(类比 Spring 的
            ``ApplicationContext.getBean(Router.class)`` 的 router,负责按名找工具)
        llm_client: LLMClient 实例,大模型客户端(类比封装 HttpClient 的 Service)
        asset_client: AssetClient 实例,产物存储客户端(写 artifact)
        conversation: ConversationManager 实例,多轮对话历史管理(见 conversation.py)
        prompt_loader: PromptLoader 实例,Prompt 模板装配器(见 prompt_loader.py)

    Returns:
        CompiledStateGraph:编译后的图。调用方传 ``config={"configurable": {"thread_id": ...}}``
        后即可 ``.stream()`` / ``.invoke()``。thread_id 是会话级,决定 checkpoint 取哪一份。

    Failure:
        - 节点函数本身抛异常会中断当前 stream,checkpoint 停在出错前一步,可重放。
        - 依赖未注入(nodes.configure 没给 registry)→ 节点运行时 AttributeError。
    """
    # 1. 注入共享依赖到 nodes 模块
    # 节点函数是无状态模块级函数(不是 class),无法用构造器注入,
    # 这里通过模块全局 setter 把依赖装进 nodes 命名空间。
    # Java 类比:Spring 的 @Configurable / field injection;或 ServletContext.setAttribute。
    # 之所以用这种方式而非传参,是因为 LangGraph 的 add_node 签名只接受 (state)->state 函数,
    # 没法在调用时传额外 context。
    nodes.configure(
        registry=registry,
        pack_routers=pack_routers or {},
        llm_client=llm_client,
        asset_client=asset_client,
        conversation=conversation,
        prompt_loader=prompt_loader,
    )

    # 2. 构建 StateGraph
    # StateGraph(GraphState):GraphState 是这条图专用的状态 schema(TypedDict),
    # LangGraph 用它做 channel 类型推断 + reducer 合并。类比 Spring Integration 的 channel 类型声明。
    workflow = StateGraph(GraphState)

    # ── 注册三个节点 ──
    # add_node(name, fn):fn 签名 (state: GraphState) -> dict,返回增量字段,框架自动 merge。
    # 注意节点是按 name 标识,边的 source/target 也用 name 引用。
    workflow.add_node("classify_intent", nodes.classify_intent_node)
    workflow.add_node("execute_tool", nodes.execute_tool_node)
    workflow.add_node("handle_result", nodes.handle_result_node)

    # ── 注册边(确定流程骨架) ──
    # add_edge(a, b):无条件边,a 执行完必然走到 b。类比 BPMN 的 sequence flow。
    workflow.add_edge(START, "classify_intent")

    # ── 条件边 1:classify_intent 之后路由 ──
    # add_conditional_edges(src, router_fn, mapping):
    #   router_fn(state) 返回一个字符串 key,mapping 把 key 映射到目标节点。
    #   Java 类比:Spring Integration 的 router / Activiti 的 exclusiveGateway。
    # route_by_tool:LLM 判到要调工具 → "tool";没合适工具(纯闲聊/打招呼)→ "end"。
    workflow.add_conditional_edges(
        "classify_intent",
        nodes.route_by_tool,
        {"tool": "execute_tool", "end": END},
    )

    # execute_tool 执行完必然走到 handle_result 收尾(无条件)
    workflow.add_edge("execute_tool", "handle_result")

    # ── 条件边 2:handle_result 之后路由(决定是否重跑) ──
    # route_after_result:返回 "rerun" → 回到 execute_tool 再跑一遍(如追问后参数变化);
    # 返回 "done" → END。这是图的回环点,让单次 stream 能跑多轮工具。
    # 对标 Claude Code query.ts 的 while(true):条件边就是循环条件。
    workflow.add_conditional_edges(
        "handle_result",
        nodes.route_after_result,
        {"rerun": "execute_tool", "done": END},
    )

    # 3. 编译(带 checkpoint)
    # ── SqliteSaver 的 WHY（P6 生产化）──
    # checkpointer 的作用:每经过一个节点,自动把完整 state 快照存起来。
    # 好处:
    #   (a) 追问恢复:interrupt 暂停后,用户回答时带同一个 thread_id,框架自动恢复现场,
    #       工具拿到 resume value 接着跑。Java 类比:类似流程引擎持久化到 JobRepository,
    #       或会话对象序列化到 HttpSession/Redis。
    #   (b) 断点续跑:崩溃后能从最后一步重放,不用从头调 LLM(省钱省时)。
    #   (c) 调试/审计:可按 thread_id dump 任意一步的 state。
    # 为什么用 SqliteSaver(而非 InMemorySaver):
    #   - 生产场景进程会重启/多 worker,InMemorySaver 重启即丢失追问现场,
    #     用户在嵌入侧栏改到一半刷新就丢——已从「开发期够用」升级为「生产必需」。
    #   - SqliteSaver 把 checkpoint 落盘到与会话同库的 SQLite 文件,零外部依赖。
    #   - 依赖包 langgraph-checkpoint-sqlite 已加入 requirements.txt。
    #   - 关键前提:checkpoint 的可恢复性依赖 thread_id 唯一标识会话,
    #     调用方必须保证同一会话始终传同一 thread_id(见 conversation_id 字段)。
    #
    # 连接生命周期：from_conn_string 的 with 语义会在退出时 close 连接，
    # 而编译后的图在后续每次请求 stream 时仍要用 checkpointer 读写 checkpoint——
    # 所以这里显式创建进程级长连接（check_same_thread=False：LangGraph 可能跨线程调度节点），
    # 由 app 生命周期（lifespan）持有并随进程退出释放。与 ConversationStore 同一思路。
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = os.getenv("DATABASE_PATH", "data/conversations.db")
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn=_conn)

    # compile 把 StateGraph 转成不可变、可调用的 CompiledStateGraph
    graph = workflow.compile(checkpointer=checkpointer)

    logger.info(
        f"LangGraph StateGraph compiled: "
        f"{len(registry.all())} tools, checkpointer=SqliteSaver"
    )

    return graph
