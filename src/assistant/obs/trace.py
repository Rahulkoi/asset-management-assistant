"""Structured tracing.

Every turn produces a trace: which tools ran, with what arguments, how long they
took, how many tokens were spent, and which guardrails fired. It powers three
things — the tool-call panel in the UI, the assertions in the eval harness, and
being able to answer "why did it say that?" after the fact.

Traces are written as JSONL. Arguments are recorded as a hash plus a redacted
copy so a trace file never becomes a place secrets accumulate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Values that should never reach a log file, even by accident.
_SECRET_PATTERNS = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # e-mail
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),                          # Google API key
    re.compile(r"\b(sk|gsk|xox[baprs])-[A-Za-z0-9\-_]{10,}\b"),          # common key prefixes
)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("[redacted]", value)
        return value
    if isinstance(value, dict):
        return {k: ("[redacted]" if k == "confirm_token" else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


@dataclass
class ToolSpan:
    tool: str
    arguments: dict[str, Any]
    args_fingerprint: str
    ok: bool
    error_code: str | None
    duration_ms: float
    result_preview: str


@dataclass
class GuardrailEvent:
    stage: str            # input | tool | loop | output
    rule: str
    action: str           # blocked | flagged | rewritten | capped
    detail: str


@dataclass
class TurnTrace:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    user_message: str = ""
    started_at: float = field(default_factory=time.time)
    model: str = ""
    iterations: int = 0
    tool_spans: list[ToolSpan] = field(default_factory=list)
    guardrails: list[GuardrailEvent] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    outcome: str = "ok"          # ok | blocked | error | budget_exceeded
    reply_preview: str = ""

    def record_tool(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        ok: bool,
        error_code: str | None,
        duration_ms: float,
        result: Any,
    ) -> None:
        preview = json.dumps(redact(result), default=str)[:300]
        self.tool_spans.append(
            ToolSpan(
                tool=tool,
                arguments=redact(arguments),
                args_fingerprint=fingerprint(arguments),
                ok=ok,
                error_code=error_code,
                duration_ms=round(duration_ms, 1),
                result_preview=preview,
            )
        )

    def record_guardrail(self, stage: str, rule: str, action: str, detail: str = "") -> None:
        self.guardrails.append(
            GuardrailEvent(stage=stage, rule=rule, action=action, detail=detail[:300])
        )

    @property
    def tools_used(self) -> list[str]:
        return [span.tool for span in self.tool_spans]

    def finish(self, reply: str, outcome: str = "ok") -> None:
        self.duration_ms = round((time.time() - self.started_at) * 1000, 1)
        self.outcome = outcome
        self.reply_preview = redact(reply)[:500]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["user_message"] = redact(self.user_message)[:500]
        return data


class TraceSink:
    """Appends traces to JSONL. Failures here must never break a conversation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, trace: TurnTrace) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace.to_dict(), default=str) + "\n")
        except OSError:
            logger.warning("Could not write trace %s", trace.trace_id, exc_info=True)
