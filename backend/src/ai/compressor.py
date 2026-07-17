"""
上下文压缩 — 压缩 + 状态补偿 + 熔断器

对标 chat-bi 项目设计:
  - estimate_tokens: 粗估 token (不依赖 tiktoken, ~4字符/token)
  - should_compress: token > 70% 模型上限 → True
  - compact_history: 旧轮次 → LLM 摘要, 保留最近 keep_recent 轮
  - 状态补偿: 压缩后从 current_config 重注入关键信息
  - CompressionCircuitBreaker: 连续3次失败熔断

数据流:
  conversation_store.get_messages() → format_history() → [压缩] → 注入 prompt
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """粗估 token 数 (不依赖 tiktoken)。

    近似: 英文 ~4 字符/token, 中文每字 ~1.5 token。
    混合文本取中间值。够用于 70% 阈值判断。
    """
    if not text:
        return 0
    # 数中文字符 (每个 ~1.5 token)
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    # 非中文字符 (~4 字符/token)
    other_chars = len(text) - cjk_count
    return int(cjk_count * 1.5 + other_chars / 4)


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """估算 messages 列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        # role 开销 ~4 token
        total += 4
    return total


def should_compress(
    messages: List[Dict[str, str]],
    model_limit: int = 200000,
    threshold: float = 0.70,
) -> bool:
    """token 超过模型上限的 70% → 触发压缩。

    Args:
        messages: 对话历史 (role + content)
        model_limit: 模型上下文上限 (默认 200K for Qwen3)
        threshold: 触发阈值 (默认 0.70)
    """
    if not messages:
        return False
    current_tokens = estimate_messages_tokens(messages)
    trigger_at = int(model_limit * threshold)
    should = current_tokens > trigger_at
    if should:
        logger.info(
            f"触发压缩: {current_tokens} tokens > {trigger_at} "
            f"({threshold*100:.0f}% of {model_limit})"
        )
    return should


@dataclass
class CompactResult:
    """压缩结果。"""
    summary: str = ""  # 旧轮次的一句话摘要
    recent_messages: List[Dict[str, str]] = field(default_factory=list)  # 保留的最近 N 轮
    state_compensation: str = ""  # 状态补偿文本 (当前配置摘要)
    error: Optional[str] = None  # 压缩失败时的错误


_COMPACT_PROMPT = """你是对话压缩器。将下面的对话历史压缩成一句话摘要, 保留关键信息 (创建了什么表单、修改了哪些字段、配置结果)。

对话历史:
{history}

只返回一句话摘要, 不要解释:"""


def compact_history_sync(
    messages: List[Dict[str, str]],
    llm_client,
    keep_recent: int = 3,
    current_config: Optional[Dict[str, Any]] = None,
) -> CompactResult:
    """同步版压缩对话历史: 旧轮次 → 摘要, 保留最近 N 轮。

    失败降级: LLM 失败 → 简单截断 + warning (不崩)。

    Args:
        messages: 完整对话历史 [{role, content}]
        llm_client: LLMClient 实例 (同步)
        keep_recent: 保留最近几轮 (默认 3)
        current_config: 当前配置 (用于状态补偿)

    Returns:
        CompactResult — summary + recent_messages + state_compensation
    """
    # 不够长 → 不压缩
    if len(messages) <= keep_recent * 2:
        return CompactResult(recent_messages=messages)

    # 分割: 保留最近 keep_recent*2 条
    split_at = len(messages) - keep_recent * 2
    old_messages = messages[:split_at]
    recent_messages = messages[split_at:]

    # LLM 生成摘要
    history_text = "\n".join(
        f"{m['role']}: {m.get('content', '')[:200]}"  # 截断长内容
        for m in old_messages
    )
    compact_prompt = _COMPACT_PROMPT.format(history=history_text)

    try:
        summary = llm_client.chat(
            messages=[{"role": "user", "content": compact_prompt}],
            temperature=0.0,
        )
        summary = summary.strip()
        logger.info(f"对话压缩成功: {len(old_messages)} 条 → 摘要 {len(summary)} 字")

        # 状态补偿: 从 current_config 提取关键信息
        state_compensation = _build_state_compensation(current_config)

        return CompactResult(
            summary=summary,
            recent_messages=recent_messages,
            state_compensation=state_compensation,
        )
    except Exception as e:
        logger.warning(f"对话压缩 LLM 失败, 降级截断: {e}")
        # 降级: 无摘要, 只保留 recent (信息损失但可用)
        return CompactResult(
            recent_messages=recent_messages,
            state_compensation=_build_state_compensation(current_config),
            error=str(e),
        )


def _build_state_compensation(config: Optional[Dict[str, Any]]) -> str:
    """从当前配置构建状态补偿文本。

    防止压缩后 LLM 忘记当前在做什么表单。
    """
    if not config:
        return ""

    parts = []
    form_name = config.get("formName", "")
    form_code = config.get("formCode", "")
    if form_name:
        parts.append(f"当前表单: {form_name} ({form_code})")

    fields = config.get("formFieldConfigVos", [])
    if fields:
        field_summary = ", ".join(
            f"{f.get('fieldTitleText', '')} ({f.get('fieldTitleKey', '')})"
            for f in fields[:10]  # 最多 10 个字段
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        parts.append(f"字段列表: {field_summary}")

    return "\n".join(parts)


def format_history_for_prompt(
    messages: List[Dict[str, str]],
    compact_result: Optional[CompactResult] = None,
) -> str:
    """将历史格式化为可注入 prompt 的文本。

    格式:
      [历史摘要]           ← 如果有压缩
      [最近 N 轮完整历史]
      [当前状态]           ← 状态补偿
    """
    parts = []

    # 压缩摘要
    if compact_result and compact_result.summary:
        parts.append(f"【历史摘要】\n{compact_result.summary}")

    # 最近消息
    recent = compact_result.recent_messages if compact_result else messages
    if recent:
        history_lines = []
        for m in recent:
            role = "用户" if m["role"] == "user" else "助手"
            content = m.get("content", "")
            # 截断过长的 assistant 回复 (配置 JSON 摘要)
            if m["role"] == "assistant" and len(content) > 300:
                content = content[:300] + "..."
            history_lines.append(f"{role}: {content}")
        parts.append("【最近对话】\n" + "\n".join(history_lines))

    # 状态补偿
    if compact_result and compact_result.state_compensation:
        parts.append(f"【当前状态】\n{compact_result.state_compensation}")

    return "\n\n".join(parts)


class CompressionCircuitBreaker:
    """压缩熔断器 (连续失败停止)。

    连续失败 >= threshold → 熔断 (停止压缩, 避免浪费 API)。
    成功 → 重置。有半开恢复。
    """

    def __init__(self, threshold: int = 3, cooldown_seconds: int = 120):
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._tripped_at: float = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._tripped_at = time.monotonic()
            logger.error(
                f"压缩熔断器触发: 连续 {self._consecutive_failures} 次失败"
            )

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
            self._tripped_at = 0.0

    def is_tripped(self) -> bool:
        if self._consecutive_failures < self._threshold:
            return False
        # 半开恢复
        if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown_seconds:
            self._tripped_at = 0.0
            return False
        return True


# 模块级熔断器单例
_compression_cb: Optional[CompressionCircuitBreaker] = None


def get_compression_circuit_breaker() -> CompressionCircuitBreaker:
    """获取全局熔断器实例。"""
    global _compression_cb
    if _compression_cb is None:
        import os
        threshold = int(os.getenv("COMPRESSION_MAX_FAILURES", "3"))
        _compression_cb = CompressionCircuitBreaker(threshold=threshold)
    return _compression_cb
