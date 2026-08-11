"""Conversation memory.

Holds the message history plus a small amount of extracted state (last asset,
last person, last location) that lets the model resolve "it" and "they" on the
next turn. The extraction is a side effect of tool results, not of parsing the
user's text — we record what was actually looked up, which is more reliable than
guessing what a pronoun pointed at.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from assistant.config import get_settings
from assistant.llm.base import (
    AssistantMessage,
    Message,
    SystemNote,
    ToolResultMessage,
    UserMessage,
)


@dataclass
class SessionState:
    session_id: str
    messages: list[Message] = field(default_factory=list)

    # Referents for pronoun resolution on the following turn.
    last_asset_code: str | None = None
    last_asset_name: str | None = None
    last_employee: str | None = None
    last_location: str | None = None

    pending_confirmation: str | None = None
    turn_count: int = 0

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def observe_tool_result(self, name: str, payload: dict[str, Any]) -> None:
        """Update referents from what the tools actually returned."""
        asset = payload.get("asset")
        if isinstance(asset, dict) and asset.get("asset_code"):
            self.last_asset_code = asset["asset_code"]
            self.last_asset_name = asset.get("asset_name")
            self.last_location = asset.get("location") or self.last_location
            holder = asset.get("holder")
            if isinstance(holder, dict) and holder.get("name"):
                self.last_employee = holder["name"]

        employee = payload.get("employee")
        if isinstance(employee, dict) and employee.get("name"):
            self.last_employee = employee["name"]
            self.last_location = employee.get("base_location") or self.last_location

        # A single search hit is unambiguous enough to become the referent.
        for key in ("results", "recommendations"):
            rows = payload.get(key)
            if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
                row = rows[0]
                if row.get("asset_code"):
                    self.last_asset_code = row["asset_code"]
                    self.last_asset_name = row.get("asset_name")
                    self.last_location = row.get("location") or self.last_location

        if payload.get("status") == "needs_confirmation":
            self.pending_confirmation = payload.get("summary")
        elif payload.get("status") == "committed":
            self.pending_confirmation = None

    def trim(self, max_turns: int | None = None) -> None:
        """Bound context growth.

        Keeps whole user->assistant->tool groups rather than slicing the list,
        because an assistant turn separated from its tool results is an invalid
        conversation for every provider.
        """
        limit = max_turns or get_settings().max_history_turns
        user_indices = [i for i, m in enumerate(self.messages) if isinstance(m, UserMessage)]
        if len(user_indices) <= limit:
            return
        cutoff = user_indices[-limit]
        self.messages = self.messages[cutoff:]

    def history_for_model(self, context_note: str | None = None) -> list[Message]:
        history = list(self.messages)
        if context_note:
            history.append(SystemNote(text=context_note))
        return history

    def transcript(self) -> list[dict[str, Any]]:
        """Readable history for the UI and for trace inspection."""
        rendered = []
        for message in self.messages:
            if isinstance(message, UserMessage):
                rendered.append({"role": "user", "text": message.text})
            elif isinstance(message, AssistantMessage):
                rendered.append(
                    {
                        "role": "assistant",
                        "text": message.text,
                        "tool_calls": [c.name for c in message.tool_calls],
                    }
                )
            elif isinstance(message, ToolResultMessage):
                rendered.append(
                    {"role": "tool", "name": message.name, "is_error": message.is_error}
                )
        return rendered


class SessionStore:
    """Thread-safe in-process session store.

    Deliberately simple. The FastAPI layer treats it as the single source of
    conversation state; swapping it for Redis means implementing get/reset.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState(session_id=session_id)
            return self._sessions[session_id]

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def active_sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)
