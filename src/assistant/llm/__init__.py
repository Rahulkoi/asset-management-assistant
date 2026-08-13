"""Provider selection."""

from __future__ import annotations

from assistant.config import get_settings
from assistant.llm.base import (
    AssistantMessage,
    AssistantTurn,
    LLMClient,
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

__all__ = [
    "AssistantMessage",
    "AssistantTurn",
    "LLMClient",
    "LLMError",
    "LLMRateLimited",
    "Message",
    "SystemNote",
    "ToolCall",
    "ToolResultMessage",
    "ToolSpec",
    "Usage",
    "UserMessage",
    "get_client",
]


# Providers that speak the OpenAI chat-completions wire format. Their tool-call
# history is interchangeable, so falling between them mid-turn is safe. Gemini
# is its own family and never mixed into an OpenAI fallback chain.
_OPENAI_FAMILY = ("openai_compat", "nvidia_nim", "cerebras")


def _canonical(name: str) -> str:
    if name in {"nvidia", "nim"}:
        return "nvidia_nim"
    if name in {"groq", "openrouter"}:
        return "openai_compat"
    if name in {"local"}:
        return "ollama"
    return name


def _build_single(name: str) -> LLMClient:
    """Construct exactly one provider client. Raises LLMError if unconfigured."""
    name = _canonical(name)
    if name == "gemini":
        from assistant.llm.gemini import GeminiClient

        return GeminiClient()
    if name == "ollama":
        from assistant.llm.ollama import OllamaClient

        return OllamaClient()
    if name == "nvidia_nim":
        from assistant.llm.nvidia_nim import NvidiaNIMClient

        return NvidiaNIMClient()
    if name == "cerebras":
        from assistant.llm.cerebras import CerebrasClient

        return CerebrasClient()
    if name == "openai_compat":
        from assistant.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient()

    raise LLMError(
        f"Unknown LLM_PROVIDER {name!r}. Use 'ollama', 'nvidia_nim', 'cerebras', "
        "'gemini', or 'openai_compat'."
    )


def get_client(provider: str | None = None) -> LLMClient:
    """Build the chat client, with same-family fallback when more than one
    provider is configured.

    The primary comes from `provider` or `LLM_PROVIDER`. If it is OpenAI-family,
    every other OpenAI-family provider that has a key is appended as a fallback,
    so a single provider's daily or per-minute cap no longer ends a turn — the
    call transparently drops to the next. Gemini is used alone, because its
    tool-call format is not interchangeable with the others (see fallback.py).

    Imports are local so a missing optional dependency for one provider never
    breaks another.
    """
    # An explicit provider request means exactly that provider — no chain. The
    # resilient chain is for the running app, which calls get_client() with no
    # argument; a caller naming a provider (a test, a one-off) wants it alone.
    if provider is not None:
        return _build_single(provider)

    settings = get_settings()
    primary = _canonical(settings.llm_provider.lower())

    if primary not in _OPENAI_FAMILY:
        return _build_single(primary)  # gemini (or any non-OpenAI family): no chain

    order = [primary] + [p for p in _OPENAI_FAMILY if p != primary]
    chain: list[tuple[str, LLMClient]] = []
    for name in order:
        try:
            chain.append((name, _build_single(name)))
        except LLMError:
            continue  # not configured; skip silently

    if not chain:
        return _build_single(primary)  # re-raise the primary's helpful "no key" error
    if len(chain) == 1:
        return chain[0][1]

    from assistant.llm.fallback import FallbackClient

    return FallbackClient(chain)
