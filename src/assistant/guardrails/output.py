"""Output verification.

A deterministic check on the model's final answer, run before the user sees it.
It cannot judge whether an answer is *good*, but it can catch the specific
failure that matters most in an asset system: stating an asset code, or citing a
policy, that no tool actually returned.

Cheap, deterministic, and it fires exactly when something is wrong — which is
why it is worth having alongside the prompt instruction that says the same thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ASSET_CODE = re.compile(r"\bAST\d{3,}\b", re.I)
CITATION = re.compile(r"\[[^\]\n]{2,80}\]")


@dataclass
class OutputVerdict:
    ok: bool
    text: str
    issues: list[str] = field(default_factory=list)
    ungrounded_codes: list[str] = field(default_factory=list)
    missing_citations: bool = False

    @property
    def should_retry(self) -> bool:
        """Worth one regeneration: the model asserted something unsupported."""
        return bool(self.ungrounded_codes) or self.missing_citations


def _codes_in(value: Any) -> set[str]:
    """Every asset code appearing anywhere in a tool result payload."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group().upper() for match in ASSET_CODE.finditer(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= _codes_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= _codes_in(item)
    return found


def check_reply(
    reply: str,
    *,
    tool_results: list[dict[str, Any]],
    user_message: str = "",
    policy_citations: list[str] | None = None,
    policy_was_searched: bool = False,
) -> OutputVerdict:
    issues: list[str] = []

    grounded = set()
    for payload in tool_results:
        grounded |= _codes_in(payload)
    grounded |= {m.group().upper() for m in ASSET_CODE.finditer(user_message)}

    mentioned = {m.group().upper() for m in ASSET_CODE.finditer(reply)}
    ungrounded = sorted(mentioned - grounded)
    if ungrounded:
        issues.append(
            f"reply mentions asset code(s) no tool returned: {', '.join(ungrounded)}"
        )

    # If policy was retrieved and the answer leans on it, the citation must be there.
    missing_citations = False
    if policy_was_searched and policy_citations:
        has_marker = bool(CITATION.search(reply))
        names_used = any(citation.lower() in reply.lower() for citation in policy_citations)
        if not has_marker and not names_used:
            missing_citations = True
            issues.append("reply uses retrieved policy but cites no source")

    return OutputVerdict(
        ok=not issues,
        text=reply,
        issues=issues,
        ungrounded_codes=ungrounded,
        missing_citations=missing_citations,
    )


def correction_note(verdict: OutputVerdict) -> str:
    """Instruction fed back for the single regeneration attempt."""
    parts = ["Your previous draft was not fully grounded and was not shown to the user."]
    if verdict.ungrounded_codes:
        parts.append(
            f"It referred to {', '.join(verdict.ungrounded_codes)}, which no tool returned. "
            "Use only asset codes present in tool results, or say the information is not available."
        )
    if verdict.missing_citations:
        parts.append(
            "It used retrieved policy without citing it. Cite the `citation` value of every "
            "passage you rely on."
        )
    parts.append("Rewrite the answer.")
    return " ".join(parts)


def apply_caveat(text: str, verdict: OutputVerdict) -> str:
    """Last resort when a regenerated answer is still ungrounded.

    Degrading visibly beats presenting an unverified claim as fact.
    """
    if not verdict.ungrounded_codes:
        return text
    return (
        f"{text}\n\n> ⚠️ I could not verify "
        f"{', '.join(verdict.ungrounded_codes)} against the asset database. "
        "Please double-check those before acting on them."
    )
