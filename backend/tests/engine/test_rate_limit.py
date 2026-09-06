# -*- coding: utf-8 -*-
"""LLM 全局限速器单测:令牌桶语义 / 429 退避闸门 / 错误分类。

长文档导入的产品化支撑(服务商 TPM/RPM 限额):
- RPM 桶:容量内直通,耗尽后按补充速率等待
- 429 退避:全局闸门,所有线程等到恢复时刻
- 错误分类:429 限流(可等待)vs 致命(欠费/鉴权,不重试)vs 普通
"""
import time

from llm.rate_limit import (
    RateLimiter, estimate_tokens, is_fatal_llm_error, is_rate_limit_error,
)


class TestTokenBucket:

    def test_unlimited_passthrough(self):
        rl = RateLimiter(rpm=0, tpm=0)
        t0 = time.monotonic()
        rl.acquire(1_000_000)
        assert time.monotonic() - t0 < 0.1  # 零开销直通

    def test_rpm_bucket_burst_then_wait(self):
        # RPM=6:容量 6 立即放行,第 7 个等 ~10s(补充 0.1/s)
        rl = RateLimiter(rpm=6, tpm=0)
        t0 = time.monotonic()
        for _ in range(6):
            rl.acquire(1)
        assert time.monotonic() - t0 < 0.5  # 容量内直通
        t0 = time.monotonic()
        rl.acquire(1)
        waited = time.monotonic() - t0
        assert 8.0 < waited < 13.0, f"第 7 个应等 ~10s,实际 {waited:.1f}s"

    def test_configure_updates_limits(self):
        rl = RateLimiter(rpm=0, tpm=0)
        assert rl.limits == (0, 0)
        rl.configure(rpm=100, tpm=50000)
        assert rl.limits == (100, 50000)


class TestBackoffGate:

    def test_429_backoff_blocks_all(self):
        rl = RateLimiter(rpm=0, tpm=0)
        rl._backoff_until = time.monotonic() + 2.0  # 模拟刚撞 429
        t0 = time.monotonic()
        rl.acquire(1)
        waited = time.monotonic() - t0
        assert 1.5 < waited < 3.5

    def test_on_rate_limited_grows_exponentially(self):
        rl = RateLimiter(rpm=0, tpm=0)
        # 连续撞 429:退避窗口指数增长 30 → 60 → 120 → 240
        seq = [rl.on_rate_limited() for _ in range(4)]
        for got, want in zip(seq, [30, 60, 120, 240]):
            assert want - 1.0 <= got <= want, f"退避序列异常: {seq}"

    def test_on_success_clears_gate(self):
        rl = RateLimiter(rpm=0, tpm=0)
        rl.on_rate_limited()
        rl.on_success()
        t0 = time.monotonic()
        rl.acquire(1)
        assert time.monotonic() - t0 < 0.1


class TestErrorClassification:

    def test_rate_limit_errors(self):
        assert is_rate_limit_error(RuntimeError("Error code: 429 - too many requests"))
        assert is_rate_limit_error(RuntimeError("Rate limit reached"))
        assert not is_rate_limit_error(RuntimeError("connection timeout"))
        assert not is_rate_limit_error(RuntimeError("Error code: 500"))

    def test_fatal_errors(self):
        assert is_fatal_llm_error(RuntimeError("Error code: 401 - invalid api key"))
        assert is_fatal_llm_error(RuntimeError("403 forbidden"))
        assert is_fatal_llm_error(RuntimeError("insufficient quota / billing"))
        assert is_fatal_llm_error(RuntimeError("模型余额不足"))
        # 普通错误不致命(可自动续跑)
        assert not is_fatal_llm_error(RuntimeError("LLM timeout"))
        assert not is_fatal_llm_error(RuntimeError("connection reset"))
        # 429 限流不是致命(等待后可恢复)
        assert not is_fatal_llm_error(RuntimeError("429 too many requests"))


class TestTokenEstimation:

    def test_cjk_vs_ascii(self):
        cjk = estimate_tokens("你好世界")       # 4 个中文字
        ascii_ = estimate_tokens("hello world")  # 11 个英文字符
        assert 0 < cjk < 10
        assert 0 < ascii_ < 10
        assert estimate_tokens("") == 0
