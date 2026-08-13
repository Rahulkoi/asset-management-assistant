"""Ollama adapter — a model running locally, with no rate limit at all.

Ollama serves an OpenAI-compatible endpoint on localhost, so the shared HTTP
adapter is enough. This is the answer to "I keep hitting free-tier limits while
testing": the model runs on this machine, so there is no per-minute cap, no
daily quota, and no token budget to exhaust. The trade-off is quality — a 7B
local model is weaker than a hosted frontier model at multi-step tool chains —
so it is ideal for unlimited iteration and the hosted provider is better for a
polished demo.

Ollama ignores the API key, but the shared adapter expects a non-empty string,
so a placeholder is used.
"""

from __future__ import annotations

from assistant.config import get_settings
from assistant.llm.openai_compat import OpenAICompatClient


class OllamaClient(OpenAICompatClient):
    def __init__(self) -> None:
        settings = get_settings()
        super().__init__(
            api_key=settings.ollama_api_key or "ollama",  # unused by Ollama, but must be non-empty
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
        )
