"""压缩 forked sidechain 测试(C.2-D + 三级保护)。"""
import time
import pytest
from unittest.mock import MagicMock, patch

from engine.compression import (
    estimate_tokens,
    estimate_messages_tokens,
    should_compress,
    get_effective_context_window,
    CompressionCircuitBreaker,
    CompressionSidechain,
)


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_english(self):
        # ~4 chars/token
        tokens = estimate_tokens("a" * 400)
        assert 80 <= tokens <= 120

    def test_chinese(self):
        # 每字 ~1.5 token
        tokens = estimate_tokens("销" * 100)  # 100 字
        assert tokens == 150


class TestShouldCompress:
    def test_below_threshold(self):
        """token < 70% 不压缩。"""
        msgs = [{"role": "user", "content": "x" * 100}]  # ~25 tokens
        # 有效窗口 = 10000 - 20000 = -10000(负数,说明 model_limit 太小)
        # 改用合理 model_limit:有效窗口 = 100000 - 20000 = 80000,70% = 56000
        assert not should_compress(msgs, model_limit=100_000)

    def test_above_threshold(self):
        """token > 70% 触发。"""
        msgs = [{"role": "user", "content": "x" * 40_000}]
        assert should_compress(msgs, model_limit=10_000)

    def test_empty_messages(self):
        assert not should_compress([])

    def test_effective_window(self):
        """有效窗口 = 总窗口 - 20K 预留。"""
        effective = get_effective_context_window(200_000)
        assert effective == 180_000


class TestCircuitBreaker:
    def test_below_threshold_not_tripped(self):
        cb = CompressionCircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_tripped()

    def test_at_threshold_tripped(self):
        cb = CompressionCircuitBreaker(threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_tripped()

    def test_success_resets(self):
        cb = CompressionCircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert not cb.is_tripped()

    def test_half_open_recovery(self):
        """熔断后经过冷却期 -> 半开恢复。"""
        # cooldown=0 会导致 is_tripped 立即半开恢复,无法测试"已熔断"状态
        # 改用 cooldown=1,先熔断,等 1 秒后半开
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=1)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_tripped()  # 熔断中
        time.sleep(1.1)  # 等冷却
        assert not cb.is_tripped()  # 半开恢复


class TestCompressionSidechain:
    """C.2-D: forked sidechain 隔离 + 不阻塞主对话流。"""

    def test_compress_async_returns_keep_recent_immediately(self):
        """compress_async 立即返回 keep-recent,不等压缩。"""
        llm = MagicMock()
        llm.chat.return_value = "摘要"
        conv = MagicMock()
        sidechain = CompressionSidechain(llm, conv)

        messages = [
            {"role": "user", "content": f"问题{i}"}
            for i in range(20)
        ] + [
            {"role": "assistant", "content": f"回答{i}"}
            for i in range(20)
        ]

        start = time.monotonic()
        recent = sidechain.compress_async("conv1", messages)
        elapsed = time.monotonic() - start

        # 应立即返回(不等 LLM 调用)
        assert elapsed < 0.1
        # 返回最近 6 条(3 轮)
        assert len(recent) == 6

    def test_compress_async_skipped_when_circuit_tripped(self):
        """熔断器触发 -> 跳过压缩,只返回 recent。"""
        llm = MagicMock()
        conv = MagicMock()
        cb = CompressionCircuitBreaker(threshold=1)
        cb.record_failure()  # 触发熔断
        sidechain = CompressionSidechain(llm, conv, circuit_breaker=cb)

        messages = [{"role": "user", "content": "x" * 100}] * 20
        recent = sidechain.compress_async("conv1", messages)
        assert len(recent) == 6
        # LLM 不应被调用
        llm.chat.assert_not_called()

    def test_compress_writes_compacted_and_trace(self):
        """压缩成功 -> 写 compacted + compact_trace 事件。"""
        llm = MagicMock()
        llm.chat.return_value = "用户创建了请假表"
        conv = MagicMock()
        sidechain = CompressionSidechain(llm, conv)

        messages = [
            {"role": "user", "content": f"问题{i}"}
            for i in range(20)
        ]

        # 同步执行(等线程完成)
        sidechain._do_compress("conv1", messages, tool=None, on_complete=None)

        # 应写 compacted + compact_trace
        assert conv.append.call_count == 2
        kinds = [call.args[1] for call in conv.append.call_args_list]
        assert "compacted" in kinds
        assert "compact_trace" in kinds

    def test_compress_failure_records_degraded_trace(self):
        """压缩失败 -> 写降级 compact_trace。"""
        llm = MagicMock()
        llm.chat.side_effect = Exception("LLM down")
        conv = MagicMock()
        sidechain = CompressionSidechain(llm, conv)

        messages = [
            {"role": "user", "content": f"问题{i}"}
            for i in range(20)
        ]

        sidechain._do_compress("conv1", messages, tool=None, on_complete=None)

        # 应写 compact_trace(降级标记)
        trace_calls = [
            call for call in conv.append.call_args_list
            if call.args[1] == "compact_trace"
        ]
        assert len(trace_calls) == 1
        assert trace_calls[0].args[2]["degraded"] is True

    def test_ptl_defense_retries_on_too_long(self):
        """PTL 防御:摘要本身超限 -> 剥洋葱重试。"""
        llm = MagicMock()
        # 前 2 次 PTL,第 3 次成功
        llm.chat.side_effect = [
            Exception("prompt too long"),
            Exception("prompt too long"),
            "最终摘要",
        ]
        sidechain = CompressionSidechain(llm)

        result = sidechain._compress_with_ptl_defense("历史内容" * 100)

        assert result == "最终摘要"
        assert llm.chat.call_count == 3

    def test_ptl_defense_degrades_after_max_retries(self):
        """PTL 重试耗尽 -> 降级截断。"""
        llm = MagicMock()
        llm.chat.side_effect = Exception("prompt too long")
        sidechain = CompressionSidechain(llm)

        result = sidechain._compress_with_ptl_defense("历史内容" * 100)

        # 降级:返回截断文本
        assert len(result) <= 500
        assert llm.chat.call_count == 3  # 重试 3 次
