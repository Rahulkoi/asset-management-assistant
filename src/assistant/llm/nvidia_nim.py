"""NVIDIA NIM adapter using its OpenAI-compatible serverless endpoint."""

from __future__ import annotations

from assistant.config import get_settings
from assistant.llm.base import LLMError
from assistant.llm.openai_compat import OpenAICompatClient


class NvidiaNIMClient(OpenAICompatClient):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.nvidia_api_key:
            raise LLMError(
                "No NVIDIA_API_KEY set. Create a key at https://build.nvidia.com "
                "and add it to .env."
            )
        super().__init__(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_nim_base_url,
            model=settings.nvidia_nim_model,
        )
