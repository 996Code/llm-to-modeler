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
    # 数中文字符 (每个 ~1.5 token):CJK 基本区范围
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    # 非中文字符 (~4 字符/token):英文/标点/空格
    other_chars = len(text) - cjk_count
    # 加权近似:中文 1.5 + 英文 /4,够判断 70% 阈值即可(不需精确)
    return int(cjk_count * 1.5 + other_chars / 4)


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """估算 messages 列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        # role 开销 ~4 token:每条消息的 role 标记 + 分隔符固定开销
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
        return False  # 无消息不压缩
    current_tokens = estimate_messages_tokens(messages)  # 当前 token 估算
    trigger_at = int(model_limit * threshold)  # 触发线 = 上限 × 阈值(默认 70%)
    should = current_tokens > trigger_at  # 严格大于才触发,留缓冲
    if should:
        # 记录触发日志:便于排查为何频繁压缩(可能是阈值过低或对话过长)
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


_COMPACT_PROMPT = """你是对话压缩器。将下面的对话历史压缩成一句话摘要, 保留关键信息 (用户做了什么、修改了哪些内容、最终结果)。

对话历史:
{history}

只返回一句话摘要, 不要解释:"""


def compact_history_sync(
    messages: List[Dict[str, str]],
    llm_client,
    keep_recent: int = 3,
    current_config: Optional[Dict[str, Any]] = None,
    summarize_artifact_fn: Optional[callable] = None,
) -> CompactResult:
    """同步版压缩对话历史: 旧轮次 → 摘要, 保留最近 N 轮。

    失败降级: LLM 失败 → 简单截断 + warning (不崩)。

    Args:
        messages: 完整对话历史 [{role, content}]
        llm_client: LLMClient 实例 (同步)
        keep_recent: 保留最近几轮 (默认 3)
        current_config: 当前配置 (用于状态补偿)
        summarize_artifact_fn: 工具的 summarize_artifact 钩子(可选),
            签名: (artifact: dict) -> str。
            不传则使用通用兜底逻辑。

    Returns:
        CompactResult — summary + recent_messages + state_compensation
    """
    # 不够长 → 不压缩(消息数 <= 保留数×2 说明全是 recent,无旧历史可压缩)
    if len(messages) <= keep_recent * 2:
        return CompactResult(recent_messages=messages)

    # 分割: 保留最近 keep_recent*2 条(user+assistant 各一条算一轮)
    split_at = len(messages) - keep_recent * 2
    old_messages = messages[:split_at]  # 旧历史(要被压成摘要)
    recent_messages = messages[split_at:]  # 最近 N 轮(完整保留)

    # LLM 生成摘要:把旧历史拼成文本喂给 LLM
    history_text = "\n".join(
        f"{m['role']}: {m.get('content', '')[:200]}"  # 截断长内容,避免单条撑爆 prompt
        for m in old_messages
    )
    compact_prompt = _COMPACT_PROMPT.format(history=history_text)

    try:
        # 调 LLM 生成摘要:temperature=0.0 保证确定性输出
        summary = llm_client.chat(
            messages=[{"role": "user", "content": compact_prompt}],
            temperature=0.0,
        )
        summary = summary.strip()
        logger.info(f"对话压缩成功: {len(old_messages)} 条 → 摘要 {len(summary)} 字")

        # 状态补偿: 优先使用工具钩子,否则兜底
        # 防止压缩后 LLM 忘记"当前在做什么"(如已建的表单信息)
        state_compensation = _build_state_compensation(
            current_config, summarize_artifact_fn
        )

        return CompactResult(
            summary=summary,
            recent_messages=recent_messages,
            state_compensation=state_compensation,
        )
    except Exception as e:
        # 降级路径:LLM 失败不崩,只保留 recent(无摘要,信息有损但可用)
        logger.warning(f"对话压缩 LLM 失败, 降级截断: {e}")
        # 降级: 无摘要, 只保留 recent (信息损失但可用)
        return CompactResult(
            recent_messages=recent_messages,
            # 降级也要带状态补偿,否则 LLM 完全丢失上下文
            state_compensation=_build_state_compensation(
                current_config, summarize_artifact_fn
            ),
            error=str(e),
        )


def _build_state_compensation(
    config: Optional[Dict[str, Any]],
    summarize_artifact_fn: Optional[callable] = None,
) -> str:
    """从当前配置构建状态补偿文本。

    防止压缩后 LLM 忘记当前在做什么。
    
    优先使用工具的 summarize_artifact 钩子(插件化),
    不传则使用通用兜底逻辑(不读制品内部特定字段)。
    """
    if not config:
        return ""  # 无配置无需补偿

    # 优先使用工具钩子(插件化):工具知道自己制品的关键字段
    if summarize_artifact_fn:
        try:
            result = summarize_artifact_fn(config)
            if result:
                return result  # 钩子成功,直接用其产出
        except Exception as e:
            # 钩子失败降级:不崩,走通用兜底
            logger.warning(f"summarize_artifact 钩子失败, 降级兜底: {e}")

    # 兜底: 通用 JSON 摘要(不读特定字段名)
    # 只提取顶层键名 + 数组长度,避免硬编码 domain 字段(保持 pack 无关)
    parts = []
    for key, value in config.items():
        if isinstance(value, list):
            # 列表只报长度,不报内容(内容可能很大)
            parts.append(f"{key}: {len(value)} 项")
        elif isinstance(value, str) and value:
            parts.append(f"{key}: {value}")  # 非空字符串直接报值
        elif isinstance(value, (int, float)) and value:
            parts.append(f"{key}: {value}")  # 非零数值直接报值

    if parts:
        # 最多 8 个字段:防止补偿文本过长反而浪费 token
        return "当前配置: " + ", ".join(parts[:8])
    return ""


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

    # 压缩摘要:如果有压缩结果,把旧历史的摘要放最前(背景信息)
    if compact_result and compact_result.summary:
        parts.append(f"【历史摘要】\n{compact_result.summary}")

    # 最近消息:完整保留最近 N 轮(无压缩)
    recent = compact_result.recent_messages if compact_result else messages
    if recent:
        history_lines = []
        for m in recent:
            role = "用户" if m["role"] == "user" else "助手"
            content = m.get("content", "")
            # 截断过长的 assistant 回复 (配置 JSON 摘要):assistant 常含大段 JSON
            if m["role"] == "assistant" and len(content) > 300:
                content = content[:300] + "..."
            history_lines.append(f"{role}: {content}")
        parts.append("【最近对话】\n" + "\n".join(history_lines))

    # 状态补偿:当前制品的关键状态,防止 LLM 忘记在做什么
    if compact_result and compact_result.state_compensation:
        parts.append(f"【当前状态】\n{compact_result.state_compensation}")

    return "\n\n".join(parts)  # 用空行分隔三段,提升 LLM 可读性


class CompressionCircuitBreaker:
    """压缩熔断器 (连续失败停止)。

    连续失败 >= threshold → 熔断 (停止压缩, 避免浪费 API)。
    成功 → 重置。有半开恢复。
    """

    def __init__(self, threshold: int = 3, cooldown_seconds: int = 120):
        self._threshold = threshold  # 连续失败多少次触发熔断
        self._cooldown_seconds = cooldown_seconds  # 熔断后冷却秒数(过后半开试探)
        self._consecutive_failures = 0  # 当前连续失败计数
        self._tripped_at: float = 0.0  # 熔断触发时间点(0.0 表示未熔断)

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            # 达到连续失败阈值:记录熔断时间点,后续 is_tripped 返回 True
            self._tripped_at = time.monotonic()  # monotonic 不受系统时钟回拨影响
            logger.error(
                f"压缩熔断器触发: 连续 {self._consecutive_failures} 次失败"
            )

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            # 成功一次即清零:采用"连续失败"语义,非累计失败
            self._consecutive_failures = 0
            self._tripped_at = 0.0

    def is_tripped(self) -> bool:
        if self._consecutive_failures < self._threshold:
            return False  # 未达阈值:正常放行
        # 半开恢复:冷却时间过后自动尝试恢复(类比 Hystrix half-open 状态)
        if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown_seconds:
            self._tripped_at = 0.0
            return False  # 恢复:放行一次试探
        return True  # 熔断中:跳过压缩


# 模块级熔断器单例:全局共享一个实例(压缩失败跨会话累计)
_compression_cb: Optional[CompressionCircuitBreaker] = None


def get_compression_circuit_breaker() -> CompressionCircuitBreaker:
    """获取全局熔断器实例。"""
    global _compression_cb
    if _compression_cb is None:
        import os
        # 阈值可环境变量配置:默认 3 次,便于不同环境调优
        threshold = int(os.getenv("COMPRESSION_MAX_FAILURES", "3"))
        _compression_cb = CompressionCircuitBreaker(threshold=threshold)
    return _compression_cb
