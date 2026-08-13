"""Transparent provider fallback.

A single free tier is a single point of failure: Gemini caps at 20 generate
requests per day, Groq at 8,000 tokens per minute. When the active provider says
"no", the turn should not die — it should try the next provider that is
configured and move on.

`FallbackClient` wraps an ordered list of clients. On a rate-limit (or any
provider error) it advances to the next one and retries the *same* call. It is
built only from providers that share a wire format, because the conversation
history carries provider-specific tool-call steps: an OpenAI-format step handed
to Gemini mid-tool-loop would be rejected. Groq, NVIDIA NIM and Cerebras are all
OpenAI-compatible, so falling between them is safe on every call, including the
second half of a tool round-trip. Mixing families is prevented in `get_client`,
not here.
"""

from __future__ import annotations

import logging

from assistant.llm.base import (
    AssistantTurn,
    LLMClient,
    LLMError,
    LLMRateLimited,
    Message,
    ToolSpec,
)

logger = logging.getLogger("assistant.llm.fallback")


class FallbackClient:
    """Try each provider in order; advance on failure, surface the last error."""

    def __init__(self, clients: list[tuple[str, LLMClient]]) -> None:
        if not clients:
            raise LLMError("FallbackClient needs at least one provider.")
        self._clients = clients
        # `model` reflects whoever answered most recently, so /healthz and the UI
        # name the provider that actually produced the reply, not a fixed guess.
        self.model = clients[0][1].model

    @property
    def provider_names(self) -> list[str]:
        return [name for name, _ in self._clients]

    def generate(
        self,
        *,
        system: str,
        history: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AssistantTurn:
        last_error: Exception | None = None
        for index, (name, client) in enumerate(self._clients):
            try:
                turn = client.generate(system=system, history=history, tools=tools)
                self.model = client.model
                if index > 0:
                    logger.info("Answered via fallback provider %r", name)
                return turn
            except LLMRateLimited as exc:
                last_error = exc
                logger.warning(
                    "Provider %r rate-limited; trying next of %s", name, self.provider_names
                )
            except LLMError as exc:
                last_error = exc
                logger.warning("Provider %r failed (%s); trying next", name, exc)

        # Every provider refused. Re-raise the last cause, preserving its type so
        # the runtime still reports "rate limited" rather than a generic failure
        # when that is what actually happened across the board.
        if isinstance(last_error, LLMRateLimited):
            raise last_error
        raise LLMError(
            f"All providers failed ({', '.join(self.provider_names)}). Last error: {last_error}"
        ) from last_error
