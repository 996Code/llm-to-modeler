"""ConversationManager - 多轮对话管理(append-only 事件流)。

模块定位
========

封装底层 ``ConversationStore``,给 Engine 层提供多轮对话的统一 API。
对标 Claude Code 的对话历史管理 + LangGraph checkpoint 之外的“可读历史”。

核心数据模型:**append-only 事件流**(Event Sourcing 模式)。
会话不是一坨可变 JSON,而是一串不可变事件,按顺序追加。每次需要“当前状态”
就重放事件流重建出来。Java 类比:类似 Kafka 的 compacted topic,或领域驱动设计
(DDD)里的 Event Sourcing Aggregate —— 状态 = f(events)。

提供的 API
==========

- ``append`` / ``load`` / ``save``:append-only 事件流的写、读重建、一轮对话的批写
- ``list_meta``:列表查询(只查 session_meta,**不 JOIN events**,避免大数据量慢查询)
- ``pending_ask``:追问现场持久化(``save/load/clear_pending_ask``),配合 LangGraph interrupt
- 压缩:阶段 4 Task 5 接压缩器(目前留 ``compacted`` 事件类型)

为什么 append-only
==================

1. **审计可追溯**:任何状态都能回放到任意时刻,谁改了什么一目了然。
2. **多版本共存**:同一份对话不同视图(原始 / 压缩后)并存,不互相覆盖。
3. **简化并发**:只追加不改,无 update 竞态。Java 类比:Copy-on-Write 思想。

kind 取值(事件类型枚举)
========================

每条事件带 ``kind`` 字段,目前共 7 种,语义见 ``append`` 方法文档。
``load`` 时按 kind 分流重建不同视图(messages / checkpoints / pending_ask)。
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationManager:
    """多轮对话管理器(append-only 事件流)。

    设计模式:门面(Facade)+ 仓储代理(Repository Proxy)。
    对 Engine 层屏蔽 ConversationStore 的存储细节(SQL/文件/内存),
    对外只暴露领域语义方法。Java 类比:Spring 的 ``@Service`` 包一层 ``@Repository``。

    无状态:本类不缓存任何数据,每次调用都走 store(幂等)。
    """

    def __init__(self, store: Any = None):
        """初始化。

        Args:
            store: ConversationStore 实例(实际读写存储)。传 None 表示“无存储”,
                所有写操作静默降级(只 log warning,不抛错),用于开发期或无状态场景。
        """
        self._store = store

    def append(self, conv_id: str, kind: str, payload: Dict[str, Any]) -> str:
        """追加一条事件(append-only,只 INSERT 不 UPDATE)。

        这是事件流的原子写单位。Java 类比:类似往 Kafka topic produce 一条消息,
        或往 audit log 表 INSERT 一行。

        Args:
            conv_id: 会话 ID(决定事件归属哪条流)
            kind:    事件类型,取值见下表
            payload: 事件内容(dict,结构随 kind 变化)

        Returns:
            新事件 ID(由 store 分配);无 store 时返回空串。

        kind 取值(全枚举,固定 7 种)
        ============================

        =============== ======================================================
        kind            含义
        =============== ======================================================
        user            用户消息(一轮对话的输入)
        assistant       助手回复(闲聊 reply 或工具 summary)
        tool_result     工具执行结果明细(进 messages)
        compacted       压缩标记点:其前为已压缩历史,其后为 keep-recent
        compact_trace   压缩轨迹(审计用,**不进** messages)
        checkpoint      快照(artifact、active_tool 等大对象,**不进** messages)
        ask             追问现场(工具 interrupt 时持久化,最新一条生效)
        =============== ======================================================
        """
        if not self._store:
            # 无 store 降级:不抛错只 warning,保证无存储也能跑(开发期/无状态调用)
            logger.warning("ConversationManager: no store configured")
            return ""
        return self._store.append_event(conv_id, kind, payload)

    def load(self, conv_id: str) -> Dict[str, Any]:
        """加载并重建会话状态(从事件流回放出当前视图)。

        典型的 Event Sourcing “重放重建”操作:遍历该会话全部事件,按 kind 分流
        累积到不同字段。Java 类比:Aggregate 从 EventStore load + replay。

        kind 分流规则
        =============

        - ``user`` / ``assistant`` / ``tool_result``:按追加顺序重建 ``messages`` 列表
          (用于注入 LLM 上下文)。每条转成 ``{role, content, config_snapshot}``。
        - ``compacted``:**不进** messages,只记录“压缩点位置” ``last_compacted_idx``。
          其前的 messages 视为“已压缩历史”,其后的为 keep-recent。
          注入 LLM 时可用 ``compressed_history`` 替代其前部分以省 token。
        - ``compact_trace``:**完全忽略**(审计专用,不影响对话视图)。
        - ``checkpoint``:累积进 ``checkpoints`` 列表(artifact 快照、active_tool 等)。
        - ``ask``:取**最新一条**作为 ``pending_ask``(旧追问已被新追问覆盖语义)。

        Args:
            conv_id: 会话 ID

        Returns:
            重建出的视图 dict::

                {
                  "messages": [...],            # 按序的消息列表
                  "pending_ask": {...} | None,  # 最新未答追问(可能为 None)
                  "checkpoints": [...],         # 所有 checkpoint payload
                  "last_compacted_idx": int,    # 压缩点位置,-1 表示无压缩
                }

            无 store 时返回空视图(``messages=[]`` 等)。
        """
        if not self._store:
            return {"messages": [], "pending_ask": None, "checkpoints": []}

        events = self._store.load_events(conv_id)
        messages = []
        checkpoints = []
        pending_ask = None
        # -1 表示从未发生过压缩;一旦遇到 compacted 事件,更新为当前 messages 长度
        last_compacted_idx = -1

        # 重放事件流:遍历顺序就是事件追加顺序(时间序)
        for i, event in enumerate(events):
            kind = event["kind"]
            payload = event["payload"]

            if kind in ("user", "assistant", "tool_result"):
                # 对话类事件 → 累积成 messages;role 缺省用 kind,payload 可能带 config_snapshot
                messages.append({
                    "role": payload.get("role", kind),
                    "content": payload.get("content", ""),
                    "config_snapshot": payload.get("config_snapshot"),
                })
            elif kind == "compacted":
                # 压缩点:记录此处的 messages 长度(此后的消息是 keep-recent)
                last_compacted_idx = len(messages)
            elif kind == "checkpoint":
                # 大对象快照单独放,不混进 messages(避免 messages 体积膨胀)
                checkpoints.append(payload)
            elif kind == "ask":
                # 追问只保留最新一条(旧追问自然被覆盖)
                pending_ask = payload  # 取最新一条

        return {
            "messages": messages,
            "pending_ask": pending_ask,
            "checkpoints": checkpoints,
            "last_compacted_idx": last_compacted_idx,
        }

    def save(self, conv_id: str, user_input: str, result: Any) -> None:
        """保存一整轮对话(批量追加 user + assistant + 可选 artifact checkpoint)。

        这是 Engine 每轮结束后的标准落库动作,内部依次 append 三类事件。

        Args:
            conv_id:    会话 ID
            user_input: 用户输入原文
            result:     ToolResult(工具执行结果,见 sdk.tool)

        设计点(为什么这样存)
        =====================

        - **只存 summary,不存 extra**:extra 可能很大(原始数据、调试信息),
          全入库会导致会话体积爆炸。summary 是给用户/LLM 看的精炼版。
        - **reply 优先于 summary**:闲聊意图(GENERAL)没有工具产出,用 reply 作为助手回复;
          工具意图用 summary。两者互斥(``elif``),保证一轮只追加一条 assistant 事件。
        - **artifact 单独走 checkpoint**:artifact 是结构化产物,不该混进 messages
          (否则 messages 注入 LLM 时 token 爆炸)。需要时从 checkpoints 取。
        """
        if not self._store:
            return

        # 1. 追加用户输入(必写,一轮对话的起点)
        self._store.append_event(conv_id, "user", {
            "role": "user",
            "content": user_input,
        })

        # 2. 追加助手回复(只存精简内容,不含 extra 避免膨胀)
        if result.reply:
            # 闲聊场景:没有工具产出,reply 就是给用户的回复
            self._store.append_event(conv_id, "assistant", {
                "role": "assistant",
                "content": result.reply,
            })
        elif result.summary:
            # 工具产出场景:summary 是工具结果的摘要,作为助手回复
            self._store.append_event(conv_id, "assistant", {
                "role": "assistant",
                "content": result.summary,
            })

        # 3. artifact 写 checkpoint(不进 messages,避免膨胀,按需从 checkpoints 取)
        if result.artifact:
            self._store.append_event(conv_id, "checkpoint", {
                "action": "artifact_saved",  # 动作类型,未来可扩展
                "artifact": result.artifact,
            })

    def save_pending_ask(self, conv_id: str, tool_name: str, ask_spec: Dict, round_num: int) -> None:
        """保存追问现场。

        当工具用 LangGraph interrupt 暂停向用户提问时,把追问上下文落库,保证:
        - 用户回答回来时能恢复“是哪个工具、问的什么、第几轮”
        - 前端刷新/断线重连也能取回未答追问

        Args:
            conv_id:   会话 ID
            tool_name: 发起追问的工具名(恢复时要回到这个工具)
            ask_spec:  追问规格(问题列表、可选项等,结构由工具定义)
            round_num: 追问轮次(同一工具可能多轮追问)
        """
        if not self._store:
            return
        self._store.append_event(conv_id, "ask", {
            "tool": tool_name,
            "ask": ask_spec,
            "round": round_num,
        })

    def load_pending_ask(self, conv_id: str) -> Optional[Dict[str, Any]]:
        """加载追问现场(最新一条)。

        Args:
            conv_id: 会话 ID

        Returns:
            最新未答追问的 dict(结构同 save_pending_ask 的 payload),无则 None。
            语义上 ``ask`` 事件只取最新一条(见 ``load`` 的分流逻辑)。
        """
        if not self._store:
            return None
        return self._store.load_pending_ask(conv_id)

    def clear_pending_ask(self, conv_id: str) -> None:
        """清除追问现场。

        用途:用户回答完、或主动取消追问后调用。Java 类比:类似把“待办”标记完成。
        实现由 store 决定(物理删 / 逻辑标记),本层只发指令。
        """
        if not self._store:
            return
        self._store.clear_pending_ask(conv_id)

    def list_meta(self, user_id: str) -> List[Dict[str, Any]]:
        """查询某用户的会话列表(只查 session_meta,**不 JOIN events**)。

        Args:
            user_id: 用户 ID

        Returns:
            会话元信息列表(标题、最后更新时间等),**不含**事件内容。

        WHY 不 JOIN events:events 表可能巨大(一个会话成百上千条),
        列表查询 JOIN 会拖慢。需要详情时单独调 ``load``。
        Java 类比:列表页查 ``conversation`` 表,详情页才查 ``conversation_event``。
        """
        if not self._store:
            return []
        return self._store.list_conversations(user_id)

    def get_messages(self, conv_id: str) -> List[Dict[str, str]]:
        """获取会话的 messages 列表(用于注入 LLM 上下文)。

        轻量入口:相比 ``load`` 只拿 messages,不重建 checkpoints / pending_ask。
        调用方通常是拼 prompt 时要历史对话,用这个更省。

        Args:
            conv_id: 会话 ID

        Returns:
            ``[{role, content}, ...]`` 列表,按时间序。
        """
        if not self._store:
            return []
        return self._store.get_messages(conv_id)
