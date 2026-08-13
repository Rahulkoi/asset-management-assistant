"""Cerebras provider wiring tests; no network or real API key required."""

from __future__ import annotations

import pytest

from assistant.config import Settings
from assistant.llm import LLMError, get_client
from assistant.llm.cerebras import CerebrasClient


def test_cerebras_client_uses_its_own_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="cerebras",
        CEREBRAS_API_KEY="test-key",
        cerebras_base_url="https://api.cerebras.ai/v1",
        cerebras_model="gpt-oss-120b",
    )
    monkeypatch.setattr("assistant.llm.cerebras.get_settings", lambda: settings)

    client = CerebrasClient()

    assert client.model == "gpt-oss-120b"
    assert client._base_url == "https://api.cerebras.ai/v1"
    assert client._api_key == "test-key"


def test_cerebras_requires_a_cerebras_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, llm_provider="cerebras")
    monkeypatch.setattr("assistant.llm.cerebras.get_settings", lambda: settings)

    with pytest.raises(LLMError, match="CEREBRAS_API_KEY"):
        CerebrasClient()


def test_get_client_recognizes_cerebras(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr("assistant.llm.cerebras.CerebrasClient", lambda: sentinel)

    assert get_client("cerebras") is sentinel


def test_cerebras_health_configuration_is_provider_specific() -> None:
    settings = Settings(_env_file=None, llm_provider="cerebras", CEREBRAS_API_KEY="test-key")

    assert settings.active_model == "gpt-oss-120b"
    assert settings.llm_configured is True
