"""ConversationManager - 多轮/压缩/存储(append-only)。

封装 ConversationStore,提供 Engine 层 API:
- append/load/save:append-only 事件流
- list_meta:列表查询(不 JOIN events)
- pending_ask:追问现场持久化
- 压缩:阶段 4 Task 5 接压缩器
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationManager:
    """多轮对话管理器(append-only 事件流)。"""

    def __init__(self, store: Any = None):
        """store: ConversationStore 实例。"""
        self._store = store

    def append(self, conv_id: str, kind: str, payload: Dict[str, Any]) -> str:
        """追加一条事件(只 INSERT 不 UPDATE)。

        kind ∈ {user, assistant, tool_result, compacted, compact_trace, checkpoint, ask}
        """
        if not self._store:
            logger.warning("ConversationManager: no store configured")
            return ""
        return self._store.append_event(conv_id, kind, payload)

    def load(self, conv_id: str) -> Dict[str, Any]:
        """加载并重建会话状态。按 kind 分流:
        - user/assistant/tool_result 按序重建 messages
        - compacted 标记压缩点,其后为 keep-recent,其前为已压缩
        - compact_trace 压缩轨迹(审计用,不进 messages)
        - checkpoint 用于持久化 artifact 快照、active_tool 等
        - ask 持久化 pending_ask 现场
        """
        if not self._store:
            return {"messages": [], "pending_ask": None, "checkpoints": []}

        events = self._store.load_events(conv_id)
        messages = []
        checkpoints = []
        pending_ask = None
        last_compacted_idx = -1

        for i, event in enumerate(events):
            kind = event["kind"]
            payload = event["payload"]

            if kind in ("user", "assistant", "tool_result"):
                messages.append({
                    "role": payload.get("role", kind),
                    "content": payload.get("content", ""),
                    "config_snapshot": payload.get("config_snapshot"),
                })
            elif kind == "compacted":
                last_compacted_idx = len(messages)
            elif kind == "checkpoint":
                checkpoints.append(payload)
            elif kind == "ask":
                pending_ask = payload  # 取最新一条

        return {
            "messages": messages,
            "pending_ask": pending_ask,
            "checkpoints": checkpoints,
            "last_compacted_idx": last_compacted_idx,
        }

    def save(self, conv_id: str, user_input: str, result: Any) -> None:
        """保存一轮对话:ToolResult.summary 入历史,extra 不入。

        Args:
            conv_id: 会话 ID
            user_input: 用户输入
            result: ToolResult
        """
        if not self._store:
            return

        # 1. 追加用户输入
        self._store.append_event(conv_id, "user", {
            "role": "user",
            "content": user_input,
        })

        # 2. 追加助手回复(只 summary,不含 extra 避免膨胀)
        if result.reply:
            # 闲聊:reply 作为回复
            self._store.append_event(conv_id, "assistant", {
                "role": "assistant",
                "content": result.reply,
            })
        elif result.summary:
            # 工具产出:summary 作为回复
            self._store.append_event(conv_id, "assistant", {
                "role": "assistant",
                "content": result.summary,
            })

        # 3. artifact 写 checkpoint(不进 messages,避免膨胀)
        if result.artifact:
            self._store.append_event(conv_id, "checkpoint", {
                "action": "artifact_saved",
                "artifact": result.artifact,
            })

    def save_pending_ask(self, conv_id: str, tool_name: str, ask_spec: Dict, round_num: int) -> None:
        """保存追问现场。"""
        if not self._store:
            return
        self._store.append_event(conv_id, "ask", {
            "tool": tool_name,
            "ask": ask_spec,
            "round": round_num,
        })

    def load_pending_ask(self, conv_id: str) -> Optional[Dict[str, Any]]:
        """加载追问现场(最新一条)。"""
        if not self._store:
            return None
        return self._store.load_pending_ask(conv_id)

    def clear_pending_ask(self, conv_id: str) -> None:
        """清除追问现场。"""
        if not self._store:
            return
        self._store.clear_pending_ask(conv_id)

    def list_meta(self, user_id: str) -> List[Dict[str, Any]]:
        """列表查询:只查 session_meta,不 JOIN events。"""
        if not self._store:
            return []
        return self._store.list_conversations(user_id)

    def get_messages(self, conv_id: str) -> List[Dict[str, str]]:
        """获取会话的 messages(用于注入 LLM 上下文)。"""
        if not self._store:
            return []
        return self._store.get_messages(conv_id)
