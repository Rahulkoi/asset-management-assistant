"""Confirmation tokens for write operations.

The headline write guardrail. A write tool called without a token does not
write — it returns a preview plus a token. Only a second call carrying that
token commits.

The token is bound to (session_id, tool_name, payload_hash) and is single-use
with a short TTL, which closes the obvious holes:

  * it cannot reuse an approval for a second write — single use
  * it cannot get approval for one change and then commit a different one —
    the payload hash is part of the binding
  * a token left lying around in history goes stale — TTL

Binding alone is not enough. The token is handed back to the model inside the
tool result, so "the model cannot invent one" is true but beside the point — it
does not need to invent one, it is given one. Without a further control the
model can preview a write and redeem its own token in the *same* turn, before
the user has seen anything. That is not a hypothetical; the live model did it.

So a token is inert until `release_for_session` marks it approved, and that is
called once per user turn, at the top of `run_turn`. A token issued during the
current turn therefore cannot be redeemed during the current turn: committing
always requires the user to have seen the preview and sent another message.
The turn boundary is the human's veto point, and it is enforced here rather
than left to the model's discretion.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from assistant.config import get_settings


class ConfirmationError(Exception):
    """Raised when a token is missing, stale, spent, or bound to different arguments."""


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the tool arguments, ignoring the token field itself."""
    material = {k: v for k, v in payload.items() if k != "confirm_token"}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass
class PendingConfirmation:
    token: str
    session_id: str
    tool_name: str
    fingerprint: str
    preview: dict[str, Any]
    created_at: float
    consumed: bool = False
    # False until a later user turn releases it. Set only by
    # ConfirmationStore.release_for_session, never by anything the model emits.
    approved: bool = False


@dataclass
class ConfirmationStore:
    """In-process store. A multi-worker deployment would back this with Redis;
    the interface is deliberately narrow so that swap is a one-file change."""

    ttl_seconds: float = field(default_factory=lambda: get_settings().confirmation_ttl_seconds)
    _pending: dict[str, PendingConfirmation] = field(default_factory=dict)

    def issue(
        self, *, session_id: str, tool_name: str, payload: dict[str, Any], preview: dict[str, Any]
    ) -> PendingConfirmation:
        self._evict_expired()
        record = PendingConfirmation(
            token=secrets.token_urlsafe(12),
            session_id=session_id,
            tool_name=tool_name,
            fingerprint=payload_fingerprint(payload),
            preview=preview,
            created_at=time.time(),
        )
        self._pending[record.token] = record
        return record

    def redeem(
        self, *, token: str, session_id: str, tool_name: str, payload: dict[str, Any]
    ) -> PendingConfirmation:
        self._evict_expired()
        record = self._pending.get(token)
        if record is None:
            raise ConfirmationError(
                "That confirmation code is not valid. Ask the user to confirm again, "
                "then use the token from the new preview."
            )
        if record.consumed:
            raise ConfirmationError("That confirmation code has already been used.")
        if not record.approved:
            raise ConfirmationError(
                "Nothing has been changed. You have only just shown this change to the "
                "user — you cannot approve it on their behalf. End your turn by asking "
                "them to confirm, and commit on the next turn if they agree."
            )
        if record.session_id != session_id:
            raise ConfirmationError("That confirmation code belongs to a different conversation.")
        if record.tool_name != tool_name:
            raise ConfirmationError("That confirmation code was issued for a different action.")
        if record.fingerprint != payload_fingerprint(payload):
            raise ConfirmationError(
                "The details changed since the user approved this action. "
                "Show the new details and ask them to confirm again."
            )
        record.consumed = True
        return record

    def release_for_session(self, session_id: str) -> None:
        """Make this session's outstanding previews redeemable.

        Called once per user turn, before the model runs. Everything previewed
        in an earlier turn becomes redeemable; anything previewed during the
        turn that follows does not, which is what stops same-turn
        self-confirmation.
        """
        self._evict_expired()
        for record in self._pending.values():
            if record.session_id == session_id and not record.consumed:
                record.approved = True

    def peek(self, token: str) -> PendingConfirmation | None:
        self._evict_expired()
        return self._pending.get(token)

    def pending_for_session(self, session_id: str) -> list[PendingConfirmation]:
        self._evict_expired()
        return [
            r for r in self._pending.values() if r.session_id == session_id and not r.consumed
        ]

    def _evict_expired(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for token in [t for t, r in self._pending.items() if r.created_at < cutoff]:
            del self._pending[token]
