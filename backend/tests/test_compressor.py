"""
上下文压缩单元测试

对标 chat-bi 项目的测试结构，验证：
- Token 估算准确性
- 压缩触发条件
- 历史压缩功能
- 熔断器行为
"""

import pytest
from unittest.mock import Mock, patch
from src.ai.compressor import (
    estimate_tokens,
    should_compress,
    compact_history_sync,
    format_history_for_prompt,
    CompressionCircuitBreaker,
    CompactResult,
)


class TestEstimateTokens:
    """Token 估算测试"""

    def test_empty_text(self):
        """空文本返回 0"""
        assert estimate_tokens("") == 0

    def test_english_text(self):
        """英文文本：约 4 字符/token"""
        # 400 字符 ≈ 100 tokens
        text = "a" * 400
        tokens = estimate_tokens(text)
        assert 80 <= tokens <= 120

    def test_chinese_text(self):
        """中文文本：每字约 1.5 token"""
        # 100 个中文字 ≈ 150 tokens
        text = "测" * 100
        tokens = estimate_tokens(text)
        assert 130 <= tokens <= 170

    def test_mixed_text(self):
        """混合文本"""
        text = "本月销售额 SELECT * FROM orders"
        tokens = estimate_tokens(text)
        assert tokens > 5


class TestShouldCompress:
    """压缩触发条件测试"""

    def test_empty_messages(self):
        """空消息列表不触发压缩"""
        assert not should_compress([], model_limit=10000)

    def test_short_messages(self):
        """短消息不触发压缩"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        assert not should_compress(messages, model_limit=10000)

    def test_long_messages_trigger(self):
        """长消息触发压缩（超过 70% 阈值）"""
        # 创建约 8000 tokens 的消息（阈值 7000）
        long_content = "x" * 32000  # 约 8000 tokens
        messages = [{"role": "user", "content": long_content}]
        assert should_compress(messages, model_limit=10000)

    def test_custom_threshold(self):
        """自定义阈值"""
        long_content = "x" * 20000  # 约 5000 tokens
        messages = [{"role": "user", "content": long_content}]
        
        # 50% 阈值应该触发
        assert should_compress(messages, model_limit=10000, threshold=0.5)
        
        # 90% 阈值不应该触发
        assert not should_compress(messages, model_limit=10000, threshold=0.9)


class TestCompactHistorySync:
    """同步压缩功能测试"""

    def test_short_history_no_compress(self):
        """短历史不压缩"""
        messages = [
            {"role": "user", "content": "问题1"},
            {"role": "assistant", "content": "回答1"},
        ]
        llm_client = Mock()
        
        result = compact_history_sync(messages, llm_client, keep_recent=3)
        
        assert result.summary == ""
        assert len(result.recent_messages) == 2
        llm_client.chat.assert_not_called()

    def test_keep_recent_messages(self):
        """保留最近 N 轮对话"""
        # 创建 10 轮对话
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"问题{i}"})
            messages.append({"role": "assistant", "content": f"回答{i}"})
        
        llm_client = Mock()
        llm_client.chat.return_value = "用户询问了多个问题，涉及数据查询和配置"
        
        result = compact_history_sync(messages, llm_client, keep_recent=3)
        
        # 应该保留最近 6 条消息（3 轮）
        assert len(result.recent_messages) == 6
        # 应该有摘要
        assert result.summary != ""
        # 应该调用 LLM
        llm_client.chat.assert_called_once()

    def test_llm_failure_graceful(self):
        """LLM 失败时优雅降级"""
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"问题{i}"})
            messages.append({"role": "assistant", "content": f"回答{i}"})
        
        llm_client = Mock()
        llm_client.chat.side_effect = Exception("LLM 调用失败")
        
        result = compact_history_sync(messages, llm_client, keep_recent=3)
        
        # 应该有错误信息
        assert result.error is not None
        # 但仍然保留最近消息
        assert len(result.recent_messages) == 6
        # 摘要为空
        assert result.summary == ""

    def test_state_compensation(self):
        """状态补偿"""
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"问题{i}"})
            messages.append({"role": "assistant", "content": f"回答{i}"})
        
        llm_client = Mock()
        llm_client.chat.return_value = "用户创建了请假申请表"
        
        current_config = {
            "formName": "请假申请表",
            "formCode": "leave_application",
            "formFieldConfigVos": [
                {"fieldTitleText": "申请人", "fieldTitleKey": "applicant"},
                {"fieldTitleText": "请假类型", "fieldTitleKey": "leave_type"},
            ]
        }
        
        result = compact_history_sync(
            messages, llm_client, keep_recent=3, current_config=current_config
        )
        
        # 应该有状态补偿
        assert result.state_compensation != ""
        assert "请假申请表" in result.state_compensation


class TestFormatHistoryForPrompt:
    """历史格式化测试"""

    def test_without_compression(self):
        """无压缩时格式化"""
        messages = [
            {"role": "user", "content": "创建一个表单"},
            {"role": "assistant", "content": "好的，我来帮你创建"},
        ]
        
        result = format_history_for_prompt(messages, compact_result=None)
        
        assert "【最近对话】" in result
        assert "创建一个表单" in result

    def test_with_compression(self):
        """有压缩时格式化"""
        compact_result = CompactResult(
            summary="用户创建了请假申请表",
            recent_messages=[
                {"role": "user", "content": "添加一个字段"},
                {"role": "assistant", "content": "好的"},
            ],
            state_compensation="当前表单：请假申请表，2 个字段",
        )
        
        result = format_history_for_prompt(
            messages=[], compact_result=compact_result
        )
        
        assert "【历史摘要】" in result
        assert "用户创建了请假申请表" in result
        assert "【最近对话】" in result
        assert "【当前状态】" in result
        assert "请假申请表" in result


class TestCompressionCircuitBreaker:
    """熔断器测试"""

    def test_initial_state(self):
        """初始状态未熔断"""
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=60)
        assert not cb.is_tripped()

    def test_below_threshold(self):
        """失败次数未达阈值不熔断"""
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_tripped()

    def test_at_threshold_trips(self):
        """达到阈值时熔断"""
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_tripped()

    def test_success_resets(self):
        """成功时重置计数器"""
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert not cb.is_tripped()
        # 再失败一次不应该熔断
        cb.record_failure()
        assert not cb.is_tripped()

    def test_cooldown_recovery(self):
        """冷却期后恢复"""
        import time
        cb = CompressionCircuitBreaker(threshold=3, cooldown_seconds=1)
        
        # 触发熔断
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_tripped()
        
        # 等待冷却
        time.sleep(1.1)
        
        # 应该恢复
        assert not cb.is_tripped()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
