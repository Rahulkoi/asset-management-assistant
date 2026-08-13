"""NVIDIA NIM provider wiring tests; no network or real API key required."""

from __future__ import annotations

import pytest

from assistant.config import Settings
from assistant.llm import LLMError, get_client
from assistant.llm.nvidia_nim import NvidiaNIMClient


def test_nvidia_nim_client_uses_its_own_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="nvidia_nim",
        NVIDIA_API_KEY="test-key",
        nvidia_nim_base_url="https://integrate.api.nvidia.com/v1",
        nvidia_nim_model="meta/llama-3.3-70b-instruct",
    )
    monkeypatch.setattr("assistant.llm.nvidia_nim.get_settings", lambda: settings)

    client = NvidiaNIMClient()

    assert client.model == "meta/llama-3.3-70b-instruct"
    assert client._base_url == "https://integrate.api.nvidia.com/v1"
    assert client._api_key == "test-key"


def test_nvidia_nim_requires_an_nvidia_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, llm_provider="nvidia_nim")
    monkeypatch.setattr("assistant.llm.nvidia_nim.get_settings", lambda: settings)

    with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
        NvidiaNIMClient()


def test_get_client_recognizes_nvidia_nim(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr("assistant.llm.nvidia_nim.NvidiaNIMClient", lambda: sentinel)

    assert get_client("nvidia_nim") is sentinel


def test_nvidia_nim_health_configuration_is_provider_specific() -> None:
    settings = Settings(_env_file=None, llm_provider="nvidia_nim", NVIDIA_API_KEY="test-key")

    assert settings.active_model == "meta/llama-3.3-70b-instruct"
    assert settings.llm_configured is True
