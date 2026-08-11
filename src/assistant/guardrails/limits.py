"""Rate limiting and loop budgets."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from assistant.config import get_settings


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Rate limit exceeded. Try again in {retry_after:.0f}s.")
        self.retry_after = retry_after


@dataclass
class RateLimiter:
    """Per-session sliding window.

    Protects the upstream free tier as much as the app: one runaway client
    should not exhaust the daily quota for everyone else.
    """

    max_requests: int = field(default_factory=lambda: get_settings().rate_limit_requests)
    window_seconds: float = field(default_factory=lambda: get_settings().rate_limit_window_seconds)
    _hits: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, session_id: str) -> None:
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(session_id, []) if now - t < self.window_seconds]
            if len(recent) >= self.max_requests:
                oldest = min(recent)
                raise RateLimitExceeded(retry_after=self.window_seconds - (now - oldest))
            recent.append(now)
            self._hits[session_id] = recent

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._hits.pop(session_id, None)


@dataclass
class TurnBudget:
    """Bounds a single turn so a confused model cannot loop forever.

    Every limit is a real failure mode: models retry failing tools, ping-pong
    between two tools, or keep 'checking one more thing' until the request times
    out. Hitting a budget is not an error — the agent is told to answer with what
    it has.
    """

    max_tool_calls: int = field(default_factory=lambda: get_settings().max_tool_calls_per_turn)
    max_iterations: int = field(default_factory=lambda: get_settings().max_loop_iterations)
    timeout_seconds: float = field(default_factory=lambda: get_settings().turn_timeout_seconds)

    tool_calls_used: int = 0
    iterations_used: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def tool_calls_remaining(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    def exhausted_reason(self) -> str | None:
        if self.iterations_used >= self.max_iterations:
            return f"reached the {self.max_iterations}-step limit for one question"
        if self.tool_calls_used >= self.max_tool_calls:
            return f"used all {self.max_tool_calls} tool calls allowed for one question"
        if self.elapsed >= self.timeout_seconds:
            return f"took longer than {self.timeout_seconds:.0f}s"
        return None
