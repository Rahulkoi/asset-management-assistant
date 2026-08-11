"""Input-side checks, run before a turn reaches the model."""

from __future__ import annotations

from dataclasses import dataclass

from assistant.config import get_settings
from assistant.guardrails import injection


@dataclass
class InputVerdict:
    allowed: bool
    text: str
    reason: str | None = None
    rule: str | None = None
    injection_flagged: bool = False
    injection_detail: str = ""


def check_user_message(text: str) -> InputVerdict:
    settings = get_settings()
    cleaned = (text or "").strip()

    if not cleaned:
        return InputVerdict(
            allowed=False,
            text=cleaned,
            reason="Please type a question about an asset, a person, or asset policy.",
            rule="empty_message",
        )

    if len(cleaned) > settings.max_user_message_chars:
        return InputVerdict(
            allowed=False,
            text=cleaned,
            reason=(
                f"That message is {len(cleaned)} characters; the limit is "
                f"{settings.max_user_message_chars}. Please shorten it."
            ),
            rule="message_too_long",
        )

    # Control characters are a cheap way to smuggle formatting past a reviewer.
    if any(ord(char) < 9 for char in cleaned):
        cleaned = "".join(char for char in cleaned if ord(char) >= 9)

    scan = injection.scan(cleaned)
    # Flagged, not blocked: the turn continues with a reminder, and the real
    # protection (no write without a confirmation token) is unaffected either way.
    return InputVerdict(
        allowed=True,
        text=cleaned,
        injection_flagged=scan.suspicious,
        injection_detail=scan.describe(),
    )
