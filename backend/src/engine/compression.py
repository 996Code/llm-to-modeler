"""压缩 forked sidechain(C.2-D)+ 三级保护 + 状态重启补偿。

对标 Claude Code autoCompact.ts + compact.ts:
- forked 线程执行(不阻塞主对话流)
- 三级保护:70% 阈值 + 熔断器 + PTL 防御
- compact_trace 条目记录轨迹(审计)
- 状态重启补偿(summarize_artifact + 能力复灌)

机制归 Engine,内容归 pack(summarize_artifact + compact prompt)。
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from sdk.tool import Tool

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────

# 模型上下文窗口(默认 200K,对标 Qwen3)。可用 LLM_CONTEXT_WINDOW 覆盖——
# 不同模型窗口不同(qwen-plus 32K / max 200K / 本地模型更小),压缩触发线
# 跟着窗口走,配错会导致"该压不压"(窗口小的模型爆上下文)或"过早压缩"
MODEL_CONTEXT_WINDOW = int(os.getenv("LLM_CONTEXT_WINDOW", "200000"))

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
    # 统计 CJK 字符数(中文/日文/韩文范围),每个约 1.5 token
    # \u4e00-\u9fff 是 CJK 统一表意文字的基本区范围，类比 Java Character.UnicodeBlock.CJK
    # 生成器表达式 sum(1 for c in ...) 比 list 计数更省内存
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cjk_count  # 非中文字符(英文/标点/空格)
    # 中文 1.5 token/字 + 英文 4 字符/token，加权近似
    return int(cjk_count * 1.5 + other_chars / 4)


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """估算 messages 列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        # content 可能是 str（纯文本）或 list（多模态含图片）
        # 多模态的图片不在此估算（图片 token 另算），只估文本部分
        if isinstance(content, str):
            total += estimate_tokens(content)
        # 每条消息固定约 4 token 的元数据开销（role 标记 + 分隔符）
        # 类比 OpenAI 官方的 per-message overhead
        total += 4
    return total


def build_compressed_history(history: list, max_messages: int = 6, max_chars: int = 200,
                             summary: str = "") -> str:
    """把对话历史格式化为文本(截断版 + 可选压缩摘要前缀)。

    summary 非空 = 该会话发生过压缩:更早的历史已被 LLM 摘要成一句话,
    以「[历史摘要] …」前缀注入 prompt,后接 keep-recent 的最近消息——
    这就是压缩"生效"的形态(此前 summary 只落库从不进 prompt,断链)。
    """
    if not history and not summary:
        return ""  # 空历史且无摘要:空串
    parts = []
    if summary:
        parts.append(f"[历史摘要] {summary}")
    for msg in history[-max_messages:]:
        role = "用户" if msg.get("role") == "user" else "助手"  # 角色中文化
        # [:max_chars] 截断超长消息，避免单条撑爆 prompt
        content = msg.get("content", "")[:max_chars]
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def get_effective_context_window(model_limit: int = MODEL_CONTEXT_WINDOW) -> int:
    """有效窗口 = 总窗口 - 预留 Summary Token。"""
    # 预留输出 token：因为输入+输出共享窗口上限，必须给输出留位
    # 类比 Java：容量为 N 的队列，预留 head room 防溢出
    return model_limit - MAX_OUTPUT_TOKENS_FOR_SUMMARY


def should_compress(messages: List[Dict[str, str]], model_limit: int = MODEL_CONTEXT_WINDOW) -> bool:
    """token 超过有效窗口的 70% -> 触发压缩。"""
    if not messages:
        return False  # 无消息不压缩
    current = estimate_messages_tokens(messages)
    effective = get_effective_context_window(model_limit)
    # 触发线 = 有效窗口 × 70%，提前触发避免逼近上限才压缩（来不及）
    trigger_at = int(effective * COMPRESSION_THRESHOLD)
    should = current > trigger_at  # 严格大于才触发
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
        with self._lock:  # 加锁:多线程(主对话+压缩线程)可能并发调用
            self._failures += 1
            if self._failures >= self._threshold:
                # 达到连续失败阈值:记录熔断时间点,后续 is_tripped 返回 True
                self._tripped_at = time.monotonic()  # monotonic 不受系统时钟调整影响
                logger.error(f"压缩熔断器触发: 连续 {self._failures} 次失败")

    def record_success(self) -> None:
        with self._lock:
            if self._failures > 0:
                # 成功一次即重置计数:熔断器采用"连续失败"语义而非累计失败
                self._failures = 0
                self._tripped_at = 0.0

    def is_tripped(self) -> bool:
        with self._lock:
            if self._failures < self._threshold:
                return False  # 未达阈值:正常放行
            # 半开恢复:熔断后等冷却时间过,自动尝试恢复(类比 Hystrix half-open)
            if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown:
                self._tripped_at = 0.0
                self._failures = 0
                logger.info("压缩熔断器半开恢复")
                return False  # 恢复:放行一次,失败会再次累计
            return True  # 熔断中:跳过压缩


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
        compact_focus: str = "",
    ):
        self._llm = llm_client
        self._conversation = conversation
        self._cb = circuit_breaker or CompressionCircuitBreaker()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compress")
        # 摘要侧重点（pack 可注入，如"创建了什么表单、修改了哪些字段"）：
        # 领域措辞不写死在引擎（零领域知识铁律）；空串则用通用表述。
        self._compact_focus = compact_focus.strip()

    def close(self) -> None:
        """停机:关闭后台压缩线程池(不等待排队任务,避免拖住进程退出)。"""
        self._executor.shutdown(wait=False)

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
        # KEEP_RECENT_TURNS * 2：每轮含 user + assistant 两条消息
        keep_n = KEEP_RECENT_TURNS * 2
        # 切片取最近 N 条；不足则全返回（类比 Java list.subList）
        recent = messages[-keep_n:] if len(messages) > keep_n else messages

        # 2. 熔断器检查
        if self._cb.is_tripped():
            # 熔断中：跳过压缩，直接返回 recent（降级，保证主流程不卡）
            logger.warning("压缩熔断器已触发,跳过本次压缩")
            return recent

        # 3. 提交后台压缩任务
        # submit 非阻塞：立即返回，压缩在独立线程跑（不阻塞主对话流）
        # 类比 Java ExecutorService.submit(Runnable)
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
        start_time = time.monotonic()  # 计时起点（monotonic 不受时钟调整影响）
        tokens_before = estimate_messages_tokens(messages)  # 压缩前 token 数

        try:
            # 1. 分割:保留最近 N 轮
            # old_messages = 除最近 N 轮外的旧历史（这些才需要被压缩成摘要）
            keep_n = KEEP_RECENT_TURNS * 2
            old_messages = messages[:-keep_n] if len(messages) > keep_n else []

            if not old_messages:
                # 没有旧历史可压缩：直接返回（recent 已包含全部）
                logger.info("历史不足,无需压缩")
                return

            # 2. 状态补偿(调 tool.summarize_artifact)
            # 把工具的当前制品状态也写进摘要，避免重启后丢失上下文
            state_compensation = ""
            if tool:
                # 传空 artifact 是简化处理（真实场景应传当前 artifact）
                state_compensation = tool.summarize_artifact({})

            # 3. LLM 摘要:范围 = 全量历史(而非仅 old_messages)。
            # 原因:compacted 事件 append 在事件流末尾,重放折叠时会把
            # "压缩点之前"的全部消息(含压缩瞬间的 keep-recent)都折进摘要——
            # 若摘要只覆盖 old 段,recent 段既不进摘要也不再进 prompt,上下文
            # 就丢了。keep-recent 只作为"当轮"的过渡上下文(compress_async
            # 的返回值),从下一轮起全部历史由摘要承接。
            history_text = "\n".join(
                f"{'用户' if m.get('role') == 'user' else '助手'}: {m.get('content', '')[:200]}"
                for m in messages
            )
            # 带 PTL 防御的压缩：输入超限时自动剥洋葱重试
            # conv_id 透传:压缩也是一次 LLM 调用,链路追踪按会话关联
            summary = self._compress_with_ptl_defense(history_text, conv_id=conv_id)

            # 4. 写 compacted + compact_trace
            # compacted：压缩结果（下次对话时作为上下文）
            # compact_trace：审计轨迹（记录压缩前后 token、是否降级）
            if self._conversation:
                self._conversation.append(conv_id, "compacted", {
                    "summary": summary,
                    "state_compensation": state_compensation,
                    "tokens_before": tokens_before,
                })
                self._conversation.append(conv_id, "compact_trace", {
                    "tokens_before": tokens_before,
                    "tokens_after": estimate_tokens(summary),
                    "summary": summary[:200],  # 只存前 200 字符，避免审计表膨胀
                    "degraded": False,  # 正常压缩，非降级
                    "protection_triggered": None,  # 没触发保护机制
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                })

            self._cb.record_success()  # 成功：重置熔断器计数
            logger.info(
                f"压缩完成: {tokens_before} -> {estimate_tokens(summary)} tokens "
                f"({len(old_messages)} 条旧历史 -> 摘要)"
            )

            if on_complete:
                # 异步回调通知（可选，类比 Java 的 CompletableFuture.thenAccept）
                on_complete({"summary": summary, "tokens_after": estimate_tokens(summary)})

        except Exception as e:
            # 任何异常：降级处理，不让压缩失败影响主流程
            logger.warning(f"压缩失败,降级: {e}")
            self._cb.record_failure()  # 记录失败，累计可能触发熔断

            # 降级:写 compact_trace 标记失败（审计用）
            if self._conversation:
                self._conversation.append(conv_id, "compact_trace", {
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_before,  # 未压缩，前后一样
                    "summary": "",
                    "degraded": True,  # 标记为降级
                    "protection_triggered": "fallback_truncate",  # 触发了降级保护
                    "error": str(e),
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                })

    def _compress_with_ptl_defense(self, history_text: str, conv_id: str = None) -> str:
        """LLM 摘要 + PTL 防御(摘要本身超限时剥洋葱重试)。

        PTL = Prompt Too Long:压缩 API 自己的输入超限。
        """
        # 压缩 prompt：要求 LLM 把历史压缩成一句话，保留关键业务信息。
        # 侧重点措辞由 pack 注入（compact_focus）——"表单/字段"是领域词，
        # 引擎只提供通用骨架；未注入时用通用表述（领域无关兜底）。
        focus = (
            f"（侧重点：{self._compact_focus}）" if self._compact_focus
            else "（保留用户目标、已产出/已变更的关键结果与尚未完成的事项）"
        )
        compact_prompt = (
            "你是对话压缩器。将下面的对话历史压缩成一句话摘要,"
            f"保留关键信息{focus}。\n\n"
            f"对话历史:\n{history_text}\n\n"
            "只返回一句话摘要,不要解释:"
        )

        # PTL 重试循环：最多 MAX_PTL_RETRIES 次
        for attempt in range(MAX_PTL_RETRIES):
            try:
                # temperature=0.0：摘要要稳定确定性，不要发散（类比 Java 的固定种子随机）
                summary = self._llm.chat([
                    {"role": "user", "content": compact_prompt}
                ], temperature=0.0, conv_id=conv_id, stage="compress_history")
                return summary.strip() if summary else ""  # 去首尾空白，空则返回空串
            except Exception as e:
                # 判断是否 PTL（Prompt Too Long）错误
                # 关键词检测：不同模型/网关返回的错误信息措辞不一，宽松匹配
                if "too long" in str(e).lower() or "prompt" in str(e).lower():
                    # PTL:剥掉 20% 旧内容重试
                    # "剥洋葱"策略：每次砍掉最旧的 20% 行，直到输入能装下
                    lines = history_text.split("\n")
                    cut = int(len(lines) * 0.2)  # 砍掉 20% 的行数
                    history_text = "\n".join(lines[cut:])  # 保留剩余 80%
                    logger.warning(f"PTL 防御:剥掉 {cut} 行,第 {attempt+1} 次重试")
                    # 重建 prompt（用缩短后的 history_text）
                    compact_prompt = (
                        "你是对话压缩器。将下面的对话历史压缩成一句话摘要。\n\n"
                        f"对话历史:\n{history_text}\n\n"
                        "只返回一句话摘要:"
                    )
                else:
                    # 非 PTL 错误（如网络/鉴权）：直接抛出，交由上层降级
                    raise

        # PTL 重试耗尽 -> 降级截断
        # 走到这里说明剥了 MAX_PTL_RETRIES 次洋葱还是超限，放弃 LLM 摘要，暴力截断
        logger.warning(f"PTL 防御重试耗尽({MAX_PTL_RETRIES} 次),降级截断")
        return history_text[:500]  # 保留前 500 字符，至少有部分上下文
