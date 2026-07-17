"""
LLM Client

Synchronous OpenAI-compatible client. All methods are SYNC so they can
be called directly inside synchronous LangGraph nodes.

Handles Qwen3 reasoning models: when content is empty (finish_reason=length),
falls back to reasoning_content. Uses high max_tokens (16384) to give the
reasoning process enough room.

Supports local LM Studio / OpenAI / any OpenAI-compatible API via base_url.
"""

import json
import logging
import os
import re
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
    max_tokens: int = 16384  # Reasoning models need room for thinking + output
    timeout: int = 300       # Local models can be slow


class LLMClient:
    """Synchronous OpenAI-compatible LLM client."""

    def __init__(self, config: Optional[LLMConfig] = None):
        if config is None:
            config = LLMConfig(
                base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
                api_key=os.getenv("LLM_API_KEY", ""),
                model=os.getenv("LLM_MODEL", "qwen/qwen3.6-35b-a3b"),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "16384")),
                timeout=int(os.getenv("LLM_TIMEOUT", "300")),
            )

        self.config = config

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

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Send a chat completion request (sync).

        Handles Qwen3 reasoning models that put output in reasoning_content.
        """
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

            return content or ""

        except Exception as e:
            logger.error(f"LLM chat failed: {e}")
            raise

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Send a chat request and parse the response as JSON.

        Strategy for models without structured output support:
        1. Try json_object response_format
        2. Fall back to plain text + manual JSON extraction

        Returns:
            Parsed JSON dict.
        """
        temp = self.config.temperature if temperature is None else temperature

        # Add explicit JSON instruction
        guided_messages = list(messages)
        guided_messages.append({
            "role": "system",
            "content": (
                "重要：你必须只输出有效的 JSON，不要输出任何其他内容。"
                "不要输出思考过程、解释或 markdown 代码块。直接输出 JSON。"
            ),
        })

        # Try json_object mode first
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=guided_messages,
                temperature=temp,
                max_tokens=self.config.max_tokens,
                response_format={"type": "json_object"},
            )
            return self._extract_json(response)
        except Exception:
            pass

        # Fall back to plain text + extraction
        logger.info("json_object mode not supported, using plain text")
        raw = self.chat(guided_messages, temperature=temp)
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
