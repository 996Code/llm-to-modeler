"""LLM 客户端适配层 —— 引擎 chat 返回 str → pack 统一契约 (content, meta)。

【为什么存在】
移植 ChatBI 时源 `llm_chat()` 返回 (content, meta) 二元组, 各栈服务按此
契约编写(测试 FakeLLM 同构)。引擎 LLMClient.chat 实际返回 str(日志/重试
由引擎内部处理, 无 meta 外泄)。本适配器在 pack 边界做一次转换:

    engine.chat(...) -> str   ⇒   adapter.chat(...) -> (content, {})

chat_json / embeddings 原样透传(签名语义一致)。所有 pack 服务函数的
`llm` 参数既可以收引擎 LLMClient 裸实例(兼容——服务里 `x, _ = llm.chat()`
的地方若直接收 str 会炸, 所以**调用方必须传适配器**), 也可以收本适配器。
统一约定: **runtime.get_llm() / AskDataTool._get_llm() 返回的一律是
LLMCompat 实例**, 服务层无感知。

【顺带能力】
`x, _ = llm.chat()` 若误收裸 str 会得到 "t, o, o ..." 的字符解包——
典型症状 "too many values to unpack"。出现该症状优先怀疑调用方
没有走本适配器。
"""
from __future__ import annotations

from typing import Any


class LLMCompat:
    """引擎 LLMClient 的 (content, meta) 契约适配器(鸭子透传其余属性)。"""

    def __init__(self, inner: Any):
        self._inner = inner

    def chat(self, messages=None, temperature=None, stage=None,
             conv_id=None, **kwargs) -> tuple:
        result = self._inner.chat(
            messages=messages, temperature=temperature,
            stage=stage, conv_id=conv_id, **kwargs)
        # 兼容两种内部形态: 引擎 LLMClient 返回 str(包装);
        # 测试 Fake/已适配实例返回 (content, meta) 元组(透传,防双重适配)
        if isinstance(result, tuple):
            return result
        return (result or ""), {}

    def chat_json(self, messages=None, temperature=None, stage=None,
                  conv_id=None, **kwargs) -> dict:
        return self._inner.chat_json(
            messages=messages, temperature=temperature,
            stage=stage, conv_id=conv_id, **kwargs)

    def embeddings(self, texts, **kwargs):
        return self._inner.embeddings(texts, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
