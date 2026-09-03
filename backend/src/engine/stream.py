"""SSE 桥接 - 把 LangGraph StateGraph 的执行结果桥接到 SSE 流。

【模块定位】
本模块是"控制器层"和"工作流引擎层"之间的适配器,负责把 LangGraph
(StateGraph)的同步执行结果,实时转换成浏览器可消费的 SSE(Server-Sent
Events)事件流。

【Java 类比】
  - SSE ≈ Spring WebFlux 的 ``Flux<ServerSentEvent>`` / 响应式流式推送
  - ``stream_graph`` 异步生成器 ≈ 返回 ``Flux<String>`` 的 Controller 方法
  - 整体架构 ≈ Spring 的 ``SseEmitter`` + 后台线程池 + 阻塞队列

【核心难点:同步 → 异步桥接】
  LangGraph 的 ``graph.stream(input, config)`` 是**同步阻塞**的生成器,
  它会在当前线程里逐个产出 chunk。但 FastAPI 的 SSE 要求**异步产出**。
  两者不能直接对接,必须用线程池把同步代码跑在后台线程,再用
  ``loop.call_soon_threadsafe`` 把事件"安全地"传回事件循环。

  类比 Java:
    - 同步阻塞 API 放进 ``CompletableFuture.runAsync(() -> ..., executor)``
    - 然后用 ``SseEmitter.send(...)`` 推给前端
    - 本模块的 StreamManager 等价于 SseEmitter + 内部阻塞队列

【两条执行路径】
  1. 首次提问:input 是一个完整的 state dict
  2. 追问恢复:input 是 ``Command(resume=answers)``,告诉 LangGraph
     "用户回答了之前的追问,从这里继续跑"
     (类比 Activiti 流程引擎的 ``runtimeService.signal(...)``)

【线程模型 & 请求级隔离（踩坑记录）】
  graph.stream 跑在 run_in_executor 的工作线程上；鉴权头（forward_headers）、
  宿主服务地址（services）、实时 SSE 推送（emitter）都用 threading.local
  做请求级隔离——**必须在 _run_graph 内部（工作线程上）绑定**，绑在事件
  循环线程读不到（早期 bug：token/服务地址"随机丢失"）。finally 中清理。

【SSE 三道保障】
  ① 15s 心跳（_heartbeat）：长生成期间保持字节流动，防代理空闲断连
  ② 响应头 no-transform：防 rsbuild 等代理的 gzip 中间件缓冲 event-stream
     （真实事故：30 秒事件同一秒到达，前端表现为"一直无响应"）
  ③ 前端 60s 空闲看门狗：以字节到达判断死活，超时主动断开并解锁输入
"""
import asyncio
import logging
import threading
from typing import Any, AsyncGenerator, Dict, List, Optional

# Command 是 LangGraph 的控制指令:用于 resume / goto / update state 等
# 类比 Activiti 的 SignalEvent,用来"唤醒"被中断的流程
from langgraph.types import Command

from api.sse import StreamManager
from engine.compression import build_compressed_history
from engine.state_keys import STATE_CONTEXT_ARTIFACT
from services.call_context import bind_conversation, clear_conversation
from services.upstream_client import set_forward_headers, set_request_services

# 模块级 logger
logger = logging.getLogger(__name__)


def _mask_header_value(value: str) -> str:
    """遮蔽透传头的值入链(可追溯 ≠ 可泄露)。

    只保留前 8 个字符供辨识(如 "Bearer e" / tenant 前缀),其余打码;
    短值(≤8)整体打码——追查时需要的是「哪个头到了、值变没变」,
    不是值本身,Authorization 原文落库本身就是审计面扩大。
    """
    v = str(value)
    return f"{v[:8]}****" if len(v) > 8 else "****"


def _request_context_trace(
    store, conv_id: str, user_id: str, services: dict,
    forward_headers: dict, context_artifact, answers, image_base64,
) -> None:
    """请求初始化参数入链(kind=trace;管理端链路视图按轮展示)。

    记录本轮 chat 请求携带的:宿主服务地址表(services,原文——URL 非敏感,
    且正是排障要看的)、透传鉴权头(仅键名 + 遮蔽值)、画布上下文规格、
    恢复/图片标记——每轮请求"到底带了什么"事后一眼可查。

    Fail-Open:与 nodes._append_trace 同策略,写失败只记日志不影响主流程。
    """
    if not store or not conv_id:
        return
    try:
        store.append_event(conv_id, "trace", {
            "stage": "request_context",
            "title": "请求初始化参数",
            "status": "ok",
            "detail": {
                "user_id": user_id,
                "services": dict(services or {}),
                "forward_headers": {
                    k: _mask_header_value(v)
                    for k, v in (forward_headers or {}).items()
                },
                "context_artifact_bytes": len(str(context_artifact)) if context_artifact else 0,
                "resume_answers": bool(answers),
                "with_image": bool(image_base64),
            },
        })
    except Exception as e:
        logger.warning(f"request_context trace write failed: {e}")


async def stream_graph(
    graph,
    user_input: str,
    conversation_id: str,
    user_id: str,
    answers: dict = None,
    image_base64: str = None,
    conversation_store=None,
    conversation_history: list = None,
    compressed_summary: str = "",
    context_artifact: dict = None,
    forward_headers: dict = None,
    services: dict = None,
) -> AsyncGenerator[str, None]:
    """走 LangGraph StateGraph 的 SSE 流(异步生成器)。

    【核心设计】
      - ``graph.stream`` 是同步 API,丢到线程池里跑(不阻塞事件循环)
      - 每产出一个 chunk,立刻通过 StreamManager 推一个 SSE 事件(实时进度)
      - 遇到 ``interrupt`` 时 ``graph.stream`` 会自动停下,需通过
        ``graph.get_state(config)`` 检查是否真的被中断,取回中断现场
      - 追问恢复:传 ``Command(resume=answers)`` 让 graph 从中断点继续

    【Java 类比】
      方法签名 ``AsyncGenerator[str, None]`` ≈ ``Flux<String>``,
      调用方(FastAPI 路由)用 ``async for`` 消费,等价于 ``flux.subscribe(...)``。

    Args:
        graph:               CompiledStateGraph 实例(已编译的工作流)
        user_input:          用户本次的消息文本
        conversation_id:     会话 ID,同时用作 LangGraph checkpoint 的 thread_id
        user_id:             用户 ID(落库用)
        answers:             追问回答;非空时走 ``Command(resume=answers)`` 路径
        image_base64:        图片 base64(供多模态工具使用)
        conversation_store:  ConversationStore 实例(可空,空则不落库)
        conversation_history:历史对话(给 LLM 当上下文)
        context_artifact:     对话的上下文参数(宿主下发的当前制品)
        forward_headers:     嵌入模式下透传给上游的请求头(鉴权等)

    Yields:
        SSE 格式的字符串,直接写给 HTTP 响应体。
    """
    # 拿到当前协程的事件循环,后续跨线程回传事件要用
    loop = asyncio.get_running_loop()
    # StreamManager 内部维护一个异步队列,等价于 SseEmitter + BlockingQueue
    sm = StreamManager(loop)

    # ── 懒创建:首条消息不带会话 ID 时在此自动建会话 ──
    # 必须发生在 input_data/config 构造之前:conversation_id 同时是 LangGraph
    # checkpoint 的 thread_id 与链路日志的会话归属,跑图后再换 id 会把
    # 追问现场和调用日志全部关联错。新 id 随 result 事件回传给前端,
    # 前端后续轮次带上(此前的前端是"点新建就落库",制造了大量 0 消息
    # 空会话——见管理端会话列表)。
    if not conversation_id and conversation_store and user_id:
        try:
            created = conversation_store.create_conversation(user_id, "")
            conversation_id = created["id"]
            logger.info(f"lazy-created conversation {conversation_id} for user {user_id}")
        except Exception as e:
            # 创建失败不阻断对话(消息不落库,但用户当轮还能收到回复)
            logger.warning(f"lazy create conversation failed: {e}")

    # 构建 graph 的输入
    if answers:
        # 路径 2:追问恢复 —— 用 Command 告诉 graph "这是对之前 interrupt 的回答"
        # graph 会从上次中断的节点继续执行
        input_data = Command(resume=answers)
    else:
        # 路径 1:首次提问 —— 构造完整的初始 state
        # 这就是 StateGraph 的"工作内存",所有节点共享读写
        input_data = {
            "user_input": user_input,
            "conversation_history": conversation_history or [],
            # 压缩后的历史(超长对话被摘要,以[历史摘要]前缀 + 最近消息注入),省 token
            "compressed_history": build_compressed_history(
                conversation_history, summary=compressed_summary
            ),
            # 注意键名必须是 snake_case 的 conversation_id:GraphState 按此声明,
            # 驼峰键会被 LangGraph 过滤丢弃,节点内 state 就拿不到会话 ID
            # (真实事故:统一 SSE 键风格的批量替换误伤过此键,链路打点全断)
            "conversation_id": conversation_id,
            "forward_headers": forward_headers or {},
            STATE_CONTEXT_ARTIFACT: context_artifact,
            "tool_name": "",
            "intent_reason": "",
            # 图片等工具私有状态,只在需要时填充
            "tool_state": {"image_base64": image_base64} if image_base64 else {},
            "tool_result": None,
            "pending_questions": [],
            "clarify_answers": {},
            # sse_events 是节点产出的"待发送事件列表",由本桥接层消费
            "sse_events": [],
        }

    # Checkpoint config:用 conversation_id 做 thread_id
    # LangGraph 按 thread_id 持久化 checkpoint,下次同一 thread_id 进来能恢复状态
    # (类比 Activiti 按 processInstanceId 持久化流程变量)
    config = {"configurable": {"thread_id": conversation_id or "default"}}

    # 用于在线程(同步)和异步之间传递结果:
    #   - last_result:   最后一次工具产出的结果数据
    #   - had_interrupt: 是否发生了 interrupt(决定是否落库"已答复"结果)
    result_holder = {"last_result": None, "had_interrupt": False}

    async def execute():
        """后台任务:在线程池跑 graph,推 SSE,处理中断,落库。"""
        try:
            # ── 在线程池中执行 graph.stream,逐 chunk 实时推 SSE ──

            def _process_chunk(chunk):
                """在线程池(后台线程)中处理单个 chunk。

                通过 ``loop.call_soon_threadsafe`` 把协程调度回事件循环线程,
                这是"从同步线程往异步循环发消息"的标准做法(线程安全)。
                类比 Java:在 worker 线程里调 ``executor.submit(() -> emitter.send(...))``。

                Args:
                    chunk: graph 产出的字典,格式通常为 {node_name: state_update}
                """
                # 错误 chunk:graph 内部抛了异常,会被包成 {"__error__": ...}
                if "__error__" in chunk:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(sm.emit_error(chunk["__error__"]))
                    )
                    return

                # chunk 格式: {node_name: state_update}
                # 一个 chunk 可能含多个节点的更新
                for node_name, state_update in chunk.items():
                    if not isinstance(state_update, dict):
                        continue

                    # 1. 处理 sse_events(节点产出的事件列表)
                    #    各节点把要推给前端的事件塞进 state["sse_events"],
                    #    由这里统一分发(集中式 SSE 出口)
                    sse_events = state_update.get("sse_events", [])
                    for event in sse_events:
                        event_type = event.get("type", "")

                        if event_type == "stage":
                            # 阶段进度:如"正在识别意图""正在调用工具"
                            sm.stage(
                                event.get("stage", ""),
                                event.get("message", ""),
                            )

                        elif event_type == "pipeline_definition":
                            # 管线定义:告诉前端"本次会跑 N 个步骤"
                            data = event.get("data", {})
                            sm.pipeline_definition(
                                data.get("tool", ""),
                                data.get("steps", []),
                            )

                        elif event_type == "result":
                            # 最终结果:工具完成的产物
                            # 注意 result 是异步推送的,要用 call_soon_threadsafe
                            result_data = event.get("data", {})
                            # 注入会话 ID:懒创建场景前端靠它拿到新会话并沿用
                            result_data = {**result_data, "conversationId": conversation_id}
                            # 记下来,后面判断是否落库用
                            result_holder["last_result"] = result_data
                            loop.call_soon_threadsafe(
                                lambda rd=result_data: asyncio.ensure_future(sm.emit_result(rd))
                            )

                        elif event_type == "error":
                            # 工具内部主动报错(非异常)
                            # 默认参绑定 error 文本：闭包晚绑定会让同一 chunk 内
                            # 多个 error 全展示最后一个的内容（对照上面 result 的 rd= 写法）
                            err_text = event.get("data", {}).get("error", "未知错误")
                            loop.call_soon_threadsafe(
                                lambda et=err_text: asyncio.ensure_future(sm.emit_error(et))
                            )

            def _run_graph():
                """在线程池中执行 graph.stream,逐 chunk 处理。

                这是真正的"同步阻塞"调用:``for chunk in graph.stream(...)``
                会一直阻塞,直到 graph 跑完或遇到 interrupt。
                放进线程池正是为了避免阻塞 asyncio 事件循环。

                【线程绑定（关键）】
                上游客户端的透传头/服务地址表是 threading.local（请求级隔离），
                而本函数运行在**线程池工作线程**上——如果在外层（事件循环线程）
                绑定，工作线程读不到。所以必须在这里、跑图之前绑定，
                跑完（含异常）在 finally 里清理，防止线程复用串请求。
                会话 ID 同理绑定(call_context):插件经 ctx.asset_client /
                ctx.llm_client 的底层调用日志自动关联会话,链路不断。
                """
                set_forward_headers(forward_headers or {})
                set_request_services(services)
                bind_conversation(conversation_id)
                # 本轮请求的初始化参数入链(services 表/掩蔽鉴权头/上下文规格)——
                # 打点紧跟绑定:时间线上先于意图路由,按轮可追溯
                _request_context_trace(
                    conversation_store, conversation_id, user_id, services,
                    forward_headers or {}, context_artifact, answers, image_base64,
                )

                # 实时进度通道：把 StreamManager 绑给工具 emit（同线程），
                # 工具每推进一步前端立刻收到——不再等节点 chunk。
                # sm.stage / sm.pipeline_definition 内部 call_soon_threadsafe，线程安全。
                from engine import nodes as _nodes

                def _realtime(kind: str, payload, message):
                    """实时 SSE 回调——工具在【工作线程】执行时经此直接推进度。

                    链路：工具 ctx.emit → thread-local emitter（本函数）→
                    call_soon_threadsafe → 事件循环 → StreamManager → SSE。
                    不经 sse_events 列表中转——那条路要等节点结束才 flush，
                    30 秒的 LLM 调用用户只能看到"正在生成"不动（真实踩坑）。
                    """
                    try:
                        if kind == "pipeline_definition":
                            sm.pipeline_definition(payload.get("tool", ""), payload.get("steps", []))
                        else:
                            sm.stage(payload, message or "")
                    except Exception:
                        # 实时通道故障不阻断工具执行（列表兜底已因绑定而关闭，
                        # 但丢一条进度事件远比崩掉生成流程可接受）
                        pass

                _nodes.set_realtime_emitter(_realtime)
                # 心跳任务：图执行期间每 15s 推一条 SSE 注释行。作用：
                #   a) 长生成期间连接始终有字节流动（防代理空闲断连）；
                #   b) 前端「空闲看门狗」以字节到达为准判断死活——连接真死时
                #      60s 无任何字节（含心跳）→ 前端主动断开并解锁输入。
                # 【线程安全（关键）】本函数跑在 executor 工作线程上，而
                # asyncio.Event / loop.create_task 只允许在事件循环线程调用
                # （跨线程调用行为未定义，可能丢唤醒/竞态）。因此：
                #   - 停止标记用 threading.Event（set()/is_set() 线程安全）；
                #   - 心跳协程用 run_coroutine_threadsafe 提交回事件循环
                #     （返回 concurrent.futures.Future，其 cancel() 也是线程安全的）。
                _hb_stop = threading.Event()

                async def _heartbeat():
                    """SSE 心跳循环（跑在事件循环上，每 15s 一条 : ping）。"""
                    while not _hb_stop.is_set():
                        await asyncio.sleep(15)
                        sm.heartbeat()

                _hb_task = asyncio.run_coroutine_threadsafe(_heartbeat(), loop)
                try:
                    for chunk in graph.stream(input_data, config):
                        _process_chunk(chunk)
                except Exception as e:
                    logger.exception(f"Graph execution failed: {e}")
                    # 异常也要回传给前端,不能吞掉
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(sm.emit_error(str(e)))
                    )
                finally:
                    # 先设停止标记再 cancel：协程若正卡在 sleep 会被直接取消，
                    # 若已退出则 cancel 是 no-op——两条路径都不泄漏任务
                    _hb_stop.set()
                    _hb_task.cancel()
                    # 清理本线程的请求级上下文（防串到线程池的下一个任务）
                    set_forward_headers(None)
                    set_request_services(None)
                    clear_conversation()
                    _nodes.set_realtime_emitter(None)

            # 关键:把同步函数丢进默认线程池执行,await 等它跑完
            # 类比 Java:CompletableFuture.supplyAsync(worker, executor).join()
            await loop.run_in_executor(None, _run_graph)

            # ── 检查是否有 interrupt 未处理 ──
            # graph.stream 遇到 interrupt 会停下(不抛异常),需要主动查状态
            try:
                state_snapshot = graph.get_state(config)
                # state_snapshot.tasks 里有未完成的 task,interrupt 信息挂在 task.interrupts
                if state_snapshot and state_snapshot.tasks:
                    for task in state_snapshot.tasks:
                        if task.interrupts:
                            # 确认发生中断:标记,后续不再走"正常落库"分支
                            result_holder["had_interrupt"] = True
                            # 落库只做一次（移出 interrupt 循环：fan-out 并行节点
                            # 场景单 task 可能带多个 interrupt，循环内落会把同一句
                            # user 消息存 N 次；当前图为线性单 interrupt，此为防御）
                            first_intr_value = None
                            for intr in task.interrupts:
                                # intr.value 是 interrupt 时传入的 payload(通常是问题列表)
                                intr_value = intr.value if hasattr(intr, 'value') else intr
                                if isinstance(intr_value, dict):
                                    # 推一个 needsClarification 事件给前端:弹追问界面
                                    # (首轮即追问的场景,前端同样靠这里的 id 采纳新会话)
                                    await sm.emit_result({
                                        "needsClarification": True,
                                        "questions": intr_value.get("questions", []),
                                        "summary": intr_value.get("summary", "需要补充信息"),
                                        "conversationId": conversation_id,
                                    })
                                    first_intr_value = first_intr_value or intr_value
                            if first_intr_value is not None:
                                # 中断场景也落库(把追问问题当作 assistant 消息存下)
                                _save_conversation(
                                    conversation_store, conversation_id, user_id,
                                    user_input, first_intr_value.get("summary", "需要补充信息"),
                                )
            except Exception as e:
                # 状态查询失败不影响主流程,只记日志
                logger.warning(f"Failed to check graph state for interrupts: {e}")

            # 保存正常结果的对话(只有非中断、且确实有结果时才存)
            if result_holder["last_result"] and not result_holder["had_interrupt"]:
                _save_result_conversation(
                    conversation_store, conversation_id, user_id,
                    user_input, result_holder["last_result"], context_artifact,
                )

            # 无论成功 / 失败 / 中断,都要发 done 让前端关闭连接
            await sm.emit_done()

        except Exception as e:
            # 最外层兜底:任何未捕获异常都转成 SSE error + done,保证前端不断连
            logger.exception("Graph stream execution failed")
            await sm.emit_error(str(e), type=type(e).__name__)
            await sm.emit_done()

    # 启动后台任务(不会立即 await,先让它跑起来)
    task = asyncio.create_task(execute())

    # 流式产出 SSE 事件:从 StreamManager 的队列里逐个 yield 给 HTTP 响应
    # 这是真正的"流式"——前端会实时收到 chunk,而不是等全部跑完
    async for event in sm.stream():
        yield event

    # 等后台任务彻底结束(确保所有副作用如落库都完成)
    await task


# ── 辅助函数 ──────────────────────────────────────────────


def _save_conversation(store, conv_id, user_id, user_input, assistant_content):
    """保存一次问答到 store(简单版,异常不崩)。

    【容错策略】落库失败只记 warning,不影响主流程——SSE 已经把结果推给前端了,
    落库只是审计用途,失败可接受。
    """
    if not store or not conv_id or not user_id:
        return
    try:
        store.add_message(conv_id=conv_id, role="user", content=user_input)
        store.add_message(conv_id=conv_id, role="assistant", content=assistant_content)
    except Exception as e:
        logger.warning(f"Failed to save conversation: {e}")


def _save_result_conversation(store, conv_id, user_id, user_input, result_data, context_artifact):
    """保存工具执行结果到对话历史(含配置快照)。

    【逻辑】
      1. 落 user / assistant 两条消息
      2. 如果结果里带 config(配置制品),额外存一条带 config_snapshot 的消息
         (审计用:记录"这次操作把配置改成了什么样")
      3. 更新会话的 current_config(存储层字段名,不改):
         - 已有配置 → 直接 update
         - 首次配置 → 用制品标题(pack 的 format_result 钩子输出的通用 title 键)起会话名
    """
    if not store or not conv_id or not user_id:
        return
    try:
        summary = result_data.get("summary", "")

        store.add_message(conv_id=conv_id, role="user", content=user_input)
        store.add_message(conv_id=conv_id, role="assistant", content=summary)

        # 配置结果:存 config_snapshot + 更新对话配置
        config = result_data.get("config")
        if config:
            # 再追加一条带配置快照的 assistant 消息(用于审计追溯)
            store.add_message(
                conv_id=conv_id, role="assistant",
                content=summary, config_snapshot=config,
            )
            if context_artifact:
                # 修改场景:已有制品,更新会话存储的当前制品
                store.update_conversation_config(conv_id, config)
            else:
                # 创建场景:首次产出配置,用 format_result 输出的通用 title 键起标题
                # (不读领域字段——制品标题之类的键属于 pack,引擎只认钩子产出)
                title = result_data.get("title", "新对话")
                store.update_conversation_config(conv_id, config, title=title)
    except Exception as e:
        logger.warning(f"Failed to save result conversation: {e}")
