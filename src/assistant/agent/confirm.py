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

But a turn boundary is only a *delay*, not a veto, and treating it as consent
was a real defect here. Releasing on any next message meant "no, cancel that"
approved the very write it refused — the UI's own Cancel button armed the
change it was meant to stop — and the only thing left standing between a
refusal and a commit was the model choosing to be polite. That is exactly the
discretion this design exists to remove. The eval case for cancellation passed
throughout, for the wrong reason.

So the *content* of the user's answer decides, `interpret_confirmation` reads
it, and the default is deny: only a clear affirmative releases a preview.
Anything else — a refusal, a question, a change of subject — discards it, so a
preview the user never accepted cannot be redeemed later.
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


# Assent words only. Ordinary verbs an instruction happens to contain — "do",
# "make", "go", "transfer", "add" — are deliberately absent: with them in the
# set, "transfer AST1003 to Amit instead" parsed as approval of a *different*
# pending transfer. A new instruction is not an answer.
_AFFIRMATIVE = frozenset(
    {
        "yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm",
        "confirmed", "approve", "approved", "proceed", "correct", "commit",
        "fine", "agreed", "affirmative", "accept", "accepted",
    }
)

# Assent that only exists as a phrase; a bare "go" or "do" means nothing.
_AFFIRMATIVE_PHRASES = (
    "go ahead", "go for it", "do it", "please do", "make it so",
    "carry on", "sounds good", "looks good", "that's right", "thats right",
)

_NEGATIVE = frozenset(
    {
        "no", "n", "nope", "nah", "cancel", "stop", "abort", "don't", "dont",
        "do not", "never", "nevermind", "discard", "reject", "decline",
        "undo", "wait", "hold",
    }
)

_MAX_DECISION_WORDS = 10


def _tokens(message: str) -> list[str]:
    cleaned = "".join(ch if ch.isalnum() or ch in "' " else " " for ch in message.lower())
    return cleaned.split()


def interpret_confirmation(message: str) -> bool | None:
    """Read the user's answer to a pending preview. Deny by default.

    Returns True only for a clear approval, False for a clear refusal, and None
    when the message expresses no decision at all. Callers must treat both False
    and None as "do not commit" — the distinction exists so the trace can say
    which happened, not so that None can be treated as consent.

    Deliberately deterministic and deliberately narrow. This parses the *user's*
    words in code, which is a different thing from asking the model nicely to
    respect them: the model never sees this decision and cannot talk it round.
    The cost of the narrowness is that an approval phrased unusually reads as
    "no decision", and the write simply has to be requested again — a safe
    failure, and the correct direction to fail in.
    """
    words = _tokens(message)
    if not words:
        return None

    # A long message is a new instruction, not an answer to a yes/no question.
    if len(words) > _MAX_DECISION_WORDS:
        return None

    normalised = " ".join(words)
    negative = any(w in _NEGATIVE for w in words)
    affirmative = any(w in _AFFIRMATIVE for w in words) or any(
        phrase in normalised for phrase in _AFFIRMATIVE_PHRASES
    )

    # "no, cancel that" and "yes but not that one" both contain an affirmative
    # token somewhere. A refusal anywhere in a short answer wins outright.
    if negative:
        return False
    if not affirmative:
        return None

    # A question is a request for information, never an approval: "ok, but who
    # is using it?" must not commit a transfer.
    if "?" in message:
        return None
    return True


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

        Call only when the user has actually approved. A turn boundary on its
        own is not approval — see `discard_for_session` and the module
        docstring.
        """
        self._evict_expired()
        for record in self._pending.values():
            if record.session_id == session_id and not record.consumed:
                record.approved = True

    def discard_for_session(self, session_id: str) -> int:
        """Destroy this session's outstanding previews. Returns how many.

        Used when the user's answer is anything other than a clear approval.
        Discarding rather than merely leaving the token unapproved matters: an
        unapproved token still sits in the conversation, and the next user turn
        would otherwise be a second chance to release it. A preview the user
        declined should stop existing.
        """
        doomed = [
            token
            for token, record in self._pending.items()
            if record.session_id == session_id and not record.consumed
        ]
        for token in doomed:
            del self._pending[token]
        return len(doomed)

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
