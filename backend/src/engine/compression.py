"""压缩 forked sidechain(C.2-D)+ 三级保护 + 状态重启补偿。

对标 Claude Code autoCompact.ts + compact.ts:
- forked 线程执行(不阻塞主对话流)
- 三级保护:70% 阈值 + 熔断器 + PTL 防御
- compact_trace 条目记录轨迹(审计)
- 状态重启补偿(summarize_artifact + 能力复灌)

机制归 Engine,内容归 pack(summarize_artifact + compact prompt)。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from sdk.tool import Tool

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

# 模型上下文窗口(默认 200K,对标 Qwen3)
MODEL_CONTEXT_WINDOW = 200_000

# 预留给 Summary API 的最大输出 Token 数
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# 70% 阈值触发压缩
COMPRESSION_THRESHOLD = 0.70

# 保留最近 N 轮(每轮 user+assistant 两条)
KEEP_RECENT_TURNS = 3

# PTL 防御:剥洋葱最大重试次数
MAX_PTL_RETRIES = 3

# 熔断器:连续失败阈值 + 冷却时间
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN = 120  # 秒


def estimate_tokens(text: str) -> int:
    """粗估 token 数(不依赖 tiktoken)。

    近似:英文 ~4 字符/token,中文每字 ~1.5 token。
    """
    if not text:
        return 0
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cjk_count
    return int(cjk_count * 1.5 + other_chars / 4)


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """估算 messages 列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        total += 4  # role 开销
    return total


def get_effective_context_window(model_limit: int = MODEL_CONTEXT_WINDOW) -> int:
    """有效窗口 = 总窗口 - 预留 Summary Token。"""
    return model_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY


def should_compress(messages: List[Dict[str, str]], model_limit: int = MODEL_CONTEXT_WINDOW) -> bool:
    """token 超过有效窗口的 70% -> 触发压缩。"""
    if not messages:
        return False
    current = estimate_messages_tokens(messages)
    effective = get_effective_context_window(model_limit)
    trigger_at = int(effective * COMPRESSION_THRESHOLD)
    should = current > trigger_at
    if should:
        logger.info(f"触发压缩: {current} tokens > {trigger_at} ({COMPRESSION_THRESHOLD*100:.0f}%)")
    return should


class CompressionCircuitBreaker:
    """熔断器:连续失败停止压缩。"""

    def __init__(
        self,
        threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        cooldown_seconds: int = CIRCUIT_BREAKER_COOLDOWN,
    ):
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._tripped_at: float = 0.0
        self._lock = threading.Lock()

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._tripped_at = time.monotonic()
                logger.error(f"压缩熔断器触发: 连续 {self._failures} 次失败")

    def record_success(self) -> None:
        with self._lock:
            if self._failures > 0:
                self._failures = 0
                self._tripped_at = 0.0

    def is_tripped(self) -> bool:
        with self._lock:
            if self._failures < self._threshold:
                return False
            # 半开恢复
            if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown:
                self._tripped_at = 0.0
                self._failures = 0
                logger.info("压缩熔断器半开恢复")
                return False
            return True


class CompressionSidechain:
    """压缩 forked sidechain(C.2-D)。

    压缩在独立线程执行,主对话流不等待:
    1. 主对话流立即返回 keep-recent 历史
    2. 压缩结果异步写回(events 表 kind=compacted)
    3. compact_trace 条目记录轨迹(审计)
    4. 失败由三级保护兜底(熔断器/PTL/降级)
    """

    def __init__(
        self,
        llm_client: Any,
        conversation: Any = None,
        circuit_breaker: Optional[CompressionCircuitBreaker] = None,
    ):
        self._llm = llm_client
        self._conversation = conversation
        self._cb = circuit_breaker or CompressionCircuitBreaker()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compress")

    def compress_async(
        self,
        conv_id: str,
        messages: List[Dict[str, str]],
        tool: Optional[Tool] = None,
        on_complete: Optional[Callable[[Dict], None]] = None,
    ) -> List[Dict[str, str]]:
        """异步压缩:立即返回 keep-recent,压缩在后台执行。

        Args:
            conv_id: 会话 ID
            messages: 完整对话历史
            tool: 产出工具(用于 summarize_artifact 状态补偿)
            on_complete: 压缩完成回调

        Returns:
            keep-recent 消息(立即返回,不等压缩)
        """
        # 1. 立即返回 keep-recent
        keep_n = KEEP_RECENT_TURNS * 2
        recent = messages[-keep_n:] if len(messages) > keep_n else messages

        # 2. 熔断器检查
        if self._cb.is_tripped():
            logger.warning("压缩熔断器已触发,跳过本次压缩")
            return recent

        # 3. 提交后台压缩任务
        self._executor.submit(
            self._do_compress, conv_id, messages, tool, on_complete
        )
        return recent

    def _do_compress(
        self,
        conv_id: str,
        messages: List[Dict[str, str]],
        tool: Optional[Tool],
        on_complete: Optional[Callable],
    ) -> None:
        """实际压缩逻辑(在后台线程执行)。"""
        start_time = time.monotonic()
        tokens_before = estimate_messages_tokens(messages)

        try:
            # 1. 分割:保留最近 N 轮
            keep_n = KEEP_RECENT_TURNS * 2
            old_messages = messages[:-keep_n] if len(messages) > keep_n else []

            if not old_messages:
                logger.info("历史不足,无需压缩")
                return

            # 2. 状态补偿(调 tool.summarize_artifact)
            state_compensation = ""
            if tool:
                state_compensation = tool.summarize_artifact({})  # 简化:传空 artifact

            # 3. LLM 摘要旧历史
            history_text = "\n".join(
                f"{'用户' if m.get('role') == 'user' else '助手'}: {m.get('content', '')[:200]}"
                for m in old_messages
            )
            summary = self._compress_with_ptl_defense(history_text)

            # 4. 写 compacted + compact_trace
            if self._conversation:
                self._conversation.append(conv_id, "compacted", {
                    "summary": summary,
                    "state_compensation": state_compensation,
                    "tokens_before": tokens_before,
                })
                self._conversation.append(conv_id, "compact_trace", {
                    "tokens_before": tokens_before,
                    "tokens_after": estimate_tokens(summary),
                    "summary": summary[:200],
                    "degraded": False,
                    "protection_triggered": None,
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                })

            self._cb.record_success()
            logger.info(
                f"压缩完成: {tokens_before} -> {estimate_tokens(summary)} tokens "
                f"({len(old_messages)} 条旧历史 -> 摘要)"
            )

            if on_complete:
                on_complete({"summary": summary, "tokens_after": estimate_tokens(summary)})

        except Exception as e:
            logger.warning(f"压缩失败,降级: {e}")
            self._cb.record_failure()

            # 降级:写 compact_trace 标记失败
            if self._conversation:
                self._conversation.append(conv_id, "compact_trace", {
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_before,  # 未压缩
                    "summary": "",
                    "degraded": True,
                    "protection_triggered": "fallback_truncate",
                    "error": str(e),
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                })

    def _compress_with_ptl_defense(self, history_text: str) -> str:
        """LLM 摘要 + PTL 防御(摘要本身超限时剥洋葱重试)。

        PTL = Prompt Too Long:压缩 API 自己的输入超限。
        """
        compact_prompt = (
            "你是对话压缩器。将下面的对话历史压缩成一句话摘要,"
            "保留关键信息(创建了什么表单、修改了哪些字段、配置结果)。\n\n"
            f"对话历史:\n{history_text}\n\n"
            "只返回一句话摘要,不要解释:"
        )

        for attempt in range(MAX_PTL_RETRIES):
            try:
                summary = self._llm.chat([
                    {"role": "user", "content": compact_prompt}
                ], temperature=0.0)
                return summary.strip() if summary else ""
            except Exception as e:
                if "too long" in str(e).lower() or "prompt" in str(e).lower():
                    # PTL:剥掉 20% 旧内容重试
                    lines = history_text.split("\n")
                    cut = int(len(lines) * 0.2)
                    history_text = "\n".join(lines[cut:])
                    logger.warning(f"PTL 防御:剥掉 {cut} 行,第 {attempt+1} 次重试")
                    compact_prompt = (
                        "你是对话压缩器。将下面的对话历史压缩成一句话摘要。\n\n"
                        f"对话历史:\n{history_text}\n\n"
                        "只返回一句话摘要:"
                    )
                else:
                    raise

        # PTL 重试耗尽 -> 降级截断
        logger.warning(f"PTL 防御重试耗尽({MAX_PTL_RETRIES} 次),降级截断")
        return history_text[:500]  # 保留前 500 字符
