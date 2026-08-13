"""FallbackClient: a rate-limited provider must not end the turn."""

from __future__ import annotations

import pytest

from assistant.llm.base import AssistantTurn, LLMError, LLMRateLimited, UserMessage
from assistant.llm.fallback import FallbackClient


class _Stub:
    """A provider that either answers or raises a preset error."""

    def __init__(self, model: str, *, raises: Exception | None = None, reply: str = "") -> None:
        self.model = model
        self._raises = raises
        self._reply = reply
        self.calls = 0

    def generate(self, *, system, history, tools=None) -> AssistantTurn:  # noqa: ANN001
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return AssistantTurn(text=self._reply)


def _ask(client: FallbackClient) -> AssistantTurn:
    return client.generate(system="s", history=[UserMessage(text="hi")], tools=None)


def test_primary_answers_without_touching_the_fallback() -> None:
    primary = _Stub("groq", reply="from groq")
    backup = _Stub("nvidia", reply="from nvidia")
    client = FallbackClient([("groq", primary), ("nvidia", backup)])

    assert _ask(client).text == "from groq"
    assert backup.calls == 0  # the fallback is never called when the primary works
    assert client.model == "groq"


def test_rate_limited_primary_falls_through_to_the_backup() -> None:
    primary = _Stub("groq", raises=LLMRateLimited("quota"))
    backup = _Stub("nvidia", reply="from nvidia")
    client = FallbackClient([("groq", primary), ("nvidia", backup)])

    turn = _ask(client)
    assert turn.text == "from nvidia"
    assert primary.calls == 1 and backup.calls == 1
    # `model` reflects whoever actually answered, so /healthz and the UI are honest.
    assert client.model == "nvidia"


def test_a_generic_provider_error_also_falls_through() -> None:
    primary = _Stub("groq", raises=LLMError("upstream 500"))
    backup = _Stub("nvidia", reply="recovered")
    client = FallbackClient([("groq", primary), ("nvidia", backup)])

    assert _ask(client).text == "recovered"


def test_when_every_provider_is_rate_limited_the_type_is_preserved() -> None:
    # The runtime keys its "rate limited, try again" message off this type, so a
    # whole-chain exhaustion must still surface as LLMRateLimited, not a generic
    # error that reads like a bug.
    client = FallbackClient(
        [
            ("groq", _Stub("groq", raises=LLMRateLimited("groq quota"))),
            ("nvidia", _Stub("nvidia", raises=LLMRateLimited("nvidia quota"))),
        ]
    )
    with pytest.raises(LLMRateLimited):
        _ask(client)


def test_an_empty_chain_is_a_construction_error() -> None:
    with pytest.raises(LLMError):
        FallbackClient([])
