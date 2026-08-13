"""Adapter for OpenAI-compatible endpoints (Cerebras, Groq, OpenRouter, vLLM...).

Exists so a exhausted free tier is a one-line config change rather than an
outage. Written against the wire format with httpx instead of pulling in another
vendor SDK — the surface we need is one endpoint.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import httpx

from assistant.config import get_settings
from assistant.llm.base import (
    AssistantMessage,
    AssistantTurn,
    LLMError,
    LLMRateLimited,
    Message,
    SystemNote,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 1.5


class OpenAICompatClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.openai_compat_api_key
        if not self._api_key:
            raise LLMError(
                "No OPENAI_COMPAT_API_KEY set. Add one to .env or switch "
                "LLM_PROVIDER back to 'gemini'."
            )
        self._base_url = (base_url or settings.openai_compat_base_url).rstrip("/")
        self.model = model or settings.openai_compat_model

    def _render_messages(self, system: str, history: list[Message]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in history:
            if isinstance(message, UserMessage):
                messages.append({"role": "user", "content": message.text})
            elif isinstance(message, SystemNote):
                messages.append(
                    {"role": "system", "content": f"<session_context>\n{message.text}\n</session_context>"}
                )
            elif isinstance(message, AssistantMessage):
                entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, default=str),
                            },
                        }
                        for call in message.tool_calls
                    ]
                messages.append(entry)
            elif isinstance(message, ToolResultMessage):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.call_id,
                        "name": message.name,
                        "content": message.as_text(),
                    }
                )
        return messages

    @staticmethod
    def _render_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def generate(
        self,
        *,
        system: str,
        history: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._render_messages(system, history),
        }
        rendered = self._render_tools(tools)
        if rendered:
            payload["tools"] = rendered
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = httpx.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=90.0,
                )
                if response.status_code == 429:
                    raise LLMRateLimited(f"Rate limited by {self._base_url}")
                response.raise_for_status()
                return self._extract(response.json())
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                if attempt == MAX_ATTEMPTS - 1:
                    break
                delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                logger.warning("Provider call failed, retrying in %.1fs: %s", delay, exc)
                time.sleep(delay)

        if isinstance(last_error, LLMRateLimited):
            raise last_error
        raise LLMError(f"Provider request failed: {last_error}") from last_error

    @staticmethod
    def _extract(body: dict[str, Any]) -> AssistantTurn:
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}

        tool_calls = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function", {}) or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=str(function.get("name", "")),
                    arguments=arguments or {},
                )
            )

        usage_raw = body.get("usage") or {}
        return AssistantTurn(
            text=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            provider_steps=[],
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            ),
            finish_reason="tool_use" if tool_calls else "stop",
        )
