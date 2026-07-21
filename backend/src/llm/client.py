"""
LLM Client

Synchronous OpenAI-compatible client. All methods are SYNC so they can
be called directly inside synchronous LangGraph nodes.

Handles Qwen3 reasoning models: when content is empty (finish_reason=length),
falls back to reasoning_content. Uses high max_tokens (200000) to give the
reasoning process enough room.

Supports local LM Studio / OpenAI / any OpenAI-compatible API via base_url.

LLM 调用日志自动持久化到 call_logs 表。
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class LLMConfig(BaseModel):
    """LLM configuration."""
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = ""
    model: str = "qwen/qwen3.6-35b-a3b"
    temperature: float = 0.1
    max_tokens: int = 200000  # Max limit for this LLM service
    timeout: int = 300       # Local models can be slow


class LLMClient:
    """Synchronous OpenAI-compatible LLM client."""

    def __init__(self, config: Optional[LLMConfig] = None, conversation_store=None):
        if config is None:
            config = LLMConfig(
                base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
                api_key=os.getenv("LLM_API_KEY", ""),
                model=os.getenv("LLM_MODEL", "qwen/qwen3.6-35b-a3b"),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "200000")),
                timeout=int(os.getenv("LLM_TIMEOUT", "300")),
            )

        self.config = config
        self._conversation_store = conversation_store

        if not config.api_key:
            logger.warning(
                "LLM_API_KEY not set — LLM calls will fail until configured."
            )

        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "placeholder-key",
            timeout=config.timeout,
        )

        logger.info(
            f"LLM client: model={config.model}, base_url={config.base_url}, "
            f"max_tokens={config.max_tokens}"
        )

    def _log_call(
        self,
        endpoint: str,
        request_data: Optional[Dict] = None,
        response_data: Optional[Dict] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        conv_id: Optional[str] = None,
    ):
        """持久化 LLM 调用日志到数据库。"""
        if not self._conversation_store:
            return
        try:
            self._conversation_store.save_call_log(
                call_type="llm",
                endpoint=endpoint,
                request_data=request_data,
                response_data=response_data,
                status_code=status_code,
                duration_ms=duration_ms,
                error_message=error_message,
                conv_id=conv_id,
            )
        except Exception as e:
            logger.warning(f"Failed to save LLM call log: {e}")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        conv_id: Optional[str] = None,
    ) -> str:
        """
        Send a chat completion request (sync).

        Handles Qwen3 reasoning models that put output in reasoning_content.
        """
        start_time = time.time()
        endpoint = f"{self.config.base_url}/chat/completions"
        request_data = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature if temperature is None else temperature,
                max_tokens=self.config.max_tokens if max_tokens is None else max_tokens,
            )

            choice = response.choices[0]
            message = choice.message

            content = message.content

            # Fallback for reasoning models (Qwen3): when content is empty
            # and finish_reason is 'length', the output is in reasoning_content
            if not content:
                rc = getattr(message, "reasoning_content", None)
                if rc:
                    logger.warning(
                        f"content empty (finish_reason={choice.finish_reason}), "
                        f"using reasoning_content fallback"
                    )
                    content = rc

            result = content or ""

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                "content": result[:500],  # 截断避免过大
                "finish_reason": choice.finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
            }
            self._log_call(
                endpoint=endpoint,
                request_data={"messages_count": len(messages), **{k: v for k, v in request_data.items() if k != "messages"}},
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
            )

            return result

        except Exception as e:
            # 记录失败日志
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"messages_count": len(messages), **{k: v for k, v in request_data.items() if k != "messages"}},
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
                conv_id=conv_id,
            )
            logger.error(f"LLM chat failed: {e}")
            raise

    def chat_json(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        conv_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a chat request and parse the response as JSON.

        Strategy for models without structured output support:
        1. Try json_object response_format
        2. Fall back to plain text + manual JSON extraction

        Supports multimodal messages (image_url content).

        Returns:
            Parsed JSON dict.
        """
        temp = self.config.temperature if temperature is None else temperature

        # Detect multimodal content (skip json_object mode if images present)
        has_images = any(
            isinstance(m.get("content"), list) for m in messages
        )

        # Add explicit JSON instruction
        guided_messages = list(messages)
        if not has_images:
            guided_messages.append({
                "role": "system",
                "content": (
                    "重要：你必须只输出有效的 JSON，不要输出任何其他内容。"
                    "不要输出思考过程、解释或 markdown 代码块。直接输出 JSON。"
                ),
            })

        # Try json_object mode first (only for text-only messages)
        start_time = time.time()
        endpoint = f"{self.config.base_url}/chat/completions"
        request_data = {
            "model": self.config.model,
            "messages": guided_messages,
            "temperature": temp,
            "max_tokens": self.config.max_tokens,
            "response_format": "json_object" if not has_images else None,
        }

        try:
            create_kwargs = {
                "model": self.config.model,
                "messages": guided_messages,
                "temperature": temp,
                "max_tokens": self.config.max_tokens,
            }
            if not has_images:
                create_kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**create_kwargs)
            result = self._extract_json(response)

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                "content": str(result)[:500],
                "finish_reason": response.choices[0].finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
            }
            self._log_call(
                endpoint=endpoint,
                request_data={"messages_count": len(guided_messages), **{k: v for k, v in request_data.items() if k != "messages"}},
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
            )

            return result
        except Exception:
            pass

        # Fall back to plain text + extraction
        logger.info("json_object mode not supported, using plain text")
        raw = self.chat(guided_messages, temperature=temp, conv_id=conv_id)
        return self._parse_json_from_text(raw)

    # ── JSON extraction helpers ────────────────────────────────

    def _extract_json(self, response) -> Dict[str, Any]:
        """Extract and parse JSON from an LLM response object."""
        choice = response.choices[0]
        message = choice.message

        content = message.content

        # Reasoning model fallback
        if not content:
            rc = getattr(message, "reasoning_content", None)
            if rc:
                content = rc

        if not content:
            raise ValueError("LLM returned empty content")

        return self._parse_json_from_text(content)

    def _parse_json_from_text(self, text: str) -> Dict[str, Any]:
        """
        Extract a JSON object from arbitrary text.

        Handles: pure JSON, markdown code blocks, JSON with surrounding prose.
        """
        text = text.strip()

        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Markdown code block
        code_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # First { to last }
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            try:
                return json.loads(text[first:last + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(
            f"Could not extract JSON from LLM response: {text[:200]}..."
        )
