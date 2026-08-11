"""Gemini adapter (google-genai, Interactions API).

Two deliberate choices:

* `store=False` — we keep the conversation ourselves. The confirm-then-commit
  gate suspends a turn between two HTTP requests, so the harness must own the
  history rather than a server-side session.
* raw step replay — assistant turns are fed back as the exact step dicts the API
  returned, which avoids reconstruction drift across SDK versions.

Response parsing is written defensively: step shapes are read through helpers
that tolerate both attribute and dict access, so an SDK field rename degrades
into a missing value rather than a stack trace mid-conversation.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

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


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read a field whether the SDK hands back an object or a dict."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _as_dict(step: Any) -> dict[str, Any]:
    if isinstance(step, dict):
        return step
    dump = getattr(step, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    return {k: v for k, v in vars(step).items() if not k.startswith("_")}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Arguments arrive as a dict or a JSON string depending on transport."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {}
    return {}


class GeminiClient:
    """Primary provider."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        key = api_key or settings.resolved_gemini_key
        if not key:
            raise LLMError(
                "No Gemini API key found. Set GEMINI_API_KEY in .env "
                "(get one free at https://aistudio.google.com/apikey)."
            )
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover
            raise LLMError("google-genai is not installed. Run: pip install google-genai") from exc

        self._genai = genai
        self._client = genai.Client(api_key=key)
        self.model = model or settings.llm_model

    # -- history translation ------------------------------------------------

    def _render_history(self, history: list[Message]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for message in history:
            if isinstance(message, UserMessage):
                rendered.append(
                    {"type": "user_input", "content": [{"type": "text", "text": message.text}]}
                )
            elif isinstance(message, SystemNote):
                # No dedicated system role mid-conversation, so it is rendered as
                # a clearly fenced input block. The system prompt tells the model
                # this channel is application state, never a user instruction.
                rendered.append(
                    {
                        "type": "user_input",
                        "content": [
                            {
                                "type": "text",
                                "text": f"<session_context>\n{message.text}\n</session_context>",
                            }
                        ],
                    }
                )
            elif isinstance(message, AssistantMessage):
                if message.provider_steps:
                    rendered.extend(message.provider_steps)
                elif message.text:
                    rendered.append(
                        {"type": "message", "content": [{"type": "text", "text": message.text}]}
                    )
            elif isinstance(message, ToolResultMessage):
                rendered.append(
                    {
                        "type": "function_result",
                        "name": message.name,
                        "call_id": message.call_id,
                        "result": [{"type": "text", "text": message.as_text()}],
                    }
                )
        return rendered

    @staticmethod
    def _render_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]

    # -- response parsing ---------------------------------------------------

    @staticmethod
    def _extract(interaction: Any) -> AssistantTurn:
        steps = _get(interaction, "steps", default=[]) or []
        raw_steps: list[dict[str, Any]] = []
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []

        for index, step in enumerate(steps):
            raw_steps.append(_as_dict(step))
            step_type = _get(step, "type", default="")

            if step_type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=str(_get(step, "id", "call_id", default=f"call_{index}")),
                        name=str(_get(step, "name", default="")),
                        arguments=_parse_arguments(_get(step, "arguments", "args")),
                    )
                )
            else:
                for block in _get(step, "content", default=[]) or []:
                    if _get(block, "type", default="text") == "text":
                        piece = _get(block, "text", default="")
                        if piece:
                            text_parts.append(str(piece))

        text = _get(interaction, "output_text", default="") or "\n".join(text_parts)

        usage_raw = _get(interaction, "usage", "usage_metadata", default=None)
        usage = Usage(
            input_tokens=int(_get(usage_raw, "input_tokens", "prompt_token_count", default=0) or 0),
            output_tokens=int(
                _get(usage_raw, "output_tokens", "candidates_token_count", default=0) or 0
            ),
        )

        return AssistantTurn(
            text=str(text).strip(),
            tool_calls=tool_calls,
            provider_steps=raw_steps,
            usage=usage,
            finish_reason="tool_use" if tool_calls else "stop",
        )

    # -- public API ---------------------------------------------------------

    def generate(
        self,
        *,
        system: str,
        history: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AssistantTurn:
        request: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "input": self._render_history(history),
            "system_instruction": system,
        }
        rendered_tools = self._render_tools(tools)
        if rendered_tools:
            request["tools"] = rendered_tools

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                interaction = self._client.interactions.create(**request)
                return self._extract(interaction)
            except Exception as exc:  # noqa: BLE001 - normalised below
                last_error = exc
                if not _is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    break
                # Free tiers rate-limit aggressively; jittered backoff keeps a
                # burst of eval cases from turning into a cascade of failures.
                delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Gemini call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, MAX_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)

        if last_error and _is_rate_limit(last_error):
            raise LLMRateLimited(
                "Gemini rate limit reached. Wait a moment and try again, or switch "
                "LLM_PROVIDER to a fallback in .env."
            ) from last_error
        raise LLMError(f"Gemini request failed: {last_error}") from last_error


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text or "quota" in text


def _is_retryable(exc: Exception) -> bool:
    if _is_rate_limit(exc):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in ("500", "502", "503", "504", "unavailable", "timeout"))
