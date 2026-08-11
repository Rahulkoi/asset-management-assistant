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


def get_client(provider: str | None = None) -> LLMClient:
    """Build the configured provider client.

    Imports are local so a missing optional dependency for one provider never
    breaks the other.
    """
    name = (provider or get_settings().llm_provider).lower()

    if name == "gemini":
        from assistant.llm.gemini import GeminiClient

        return GeminiClient()
    if name in {"openai_compat", "groq", "openrouter"}:
        from assistant.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient()

    raise LLMError(f"Unknown LLM_PROVIDER {name!r}. Use 'gemini' or 'openai_compat'.")
