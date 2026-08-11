"""Provider-agnostic LLM interface.

The agent runtime speaks only these types. No vendor SDK is imported outside
`llm/`, so swapping Gemini for an OpenAI-compatible endpoint is a config change
rather than a refactor — which matters when the model behind the app is a free
tier that can run out.

Conversation history is neutral, with one concession to fidelity: an assistant
turn carries the provider's own raw representation alongside the normalised
fields. When replaying history to the same provider we hand back exactly what it
gave us, instead of hoping our reconstruction round-trips.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


# --- Neutral conversation history ------------------------------------------


@dataclass
class UserMessage:
    text: str
    role: str = "user"


@dataclass
class AssistantMessage:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_steps: list[dict[str, Any]] | None = None
    role: str = "assistant"


@dataclass
class ToolResultMessage:
    call_id: str
    name: str
    content: dict[str, Any]
    is_error: bool = False
    role: str = "tool"

    def as_text(self) -> str:
        return json.dumps(self.content, default=str, separators=(",", ":"))


@dataclass
class SystemNote:
    """Operator-authority context injected mid-conversation.

    Used for the session context block (last asset discussed, pending
    confirmations). Kept distinct from user text so retrieved data and app state
    can never be confused with something the user actually said.
    """

    text: str
    role: str = "system_note"


Message = UserMessage | AssistantMessage | ToolResultMessage | SystemNote


@dataclass
class AssistantTurn:
    """One model response."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_steps: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> AssistantMessage:
        return AssistantMessage(
            text=self.text, tool_calls=self.tool_calls, provider_steps=self.provider_steps
        )


class LLMError(Exception):
    """Provider call failed in a way the caller should surface, not retry blindly."""


class LLMRateLimited(LLMError):
    """Retryable: quota or throughput limit. Free tiers hit this routinely."""


@runtime_checkable
class LLMClient(Protocol):
    """What the agent runtime requires of any provider."""

    model: str

    def generate(
        self,
        *,
        system: str,
        history: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AssistantTurn:
        ...
