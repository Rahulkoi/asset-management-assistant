"""Cerebras adapter.

Cerebras exposes the OpenAI chat-completions and function-calling format, so
the shared HTTP adapter is enough.  This thin class keeps its credentials and
defaults provider-specific.
"""

from __future__ import annotations

from assistant.config import get_settings
from assistant.llm.base import LLMError
from assistant.llm.openai_compat import OpenAICompatClient


class CerebrasClient(OpenAICompatClient):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.cerebras_api_key:
            raise LLMError(
                "No CEREBRAS_API_KEY set. Create a key at https://cloud.cerebras.ai "
                "and add it to .env."
            )
        super().__init__(
            api_key=settings.cerebras_api_key,
            base_url=settings.cerebras_base_url,
            model=settings.cerebras_model,
        )
