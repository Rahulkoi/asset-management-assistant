"""The agent runtime.

A hand-written tool loop rather than an SDK's automatic function-calling helper,
for one concrete reason: the confirm-then-commit gate has to suspend a turn
between two HTTP requests. The preview is returned to the user, the turn ends,
and the commit happens on a later request with the conversation intact. An
automatic loop that runs tools to completion inside one call cannot express that.

Owning the loop also puts the guardrail checks where they belong — between the
model deciding to act and the action happening:

    input check -> [ model -> guardrails -> tools -> guardrails ]* -> output check
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.agent import prompts
from assistant.agent.confirm import ConfirmationStore
from assistant.agent.memory import SessionState, SessionStore
from assistant.config import get_settings
from assistant.guardrails import injection
from assistant.guardrails import output as output_guard
from assistant.guardrails.input import check_user_message
from assistant.guardrails.limits import RateLimiter, RateLimitExceeded, TurnBudget
from assistant.llm import LLMClient, LLMError, LLMRateLimited
from assistant.llm.base import SystemNote, ToolResultMessage, ToolSpec, Usage, UserMessage
from assistant.obs.trace import TraceSink, TurnTrace
from assistant.tools.catalog import build_registry
from assistant.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    reply: str
    trace_id: str
    tools_used: list[str] = field(default_factory=list)
    tool_spans: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    guardrails: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    outcome: str = "ok"
    duration_ms: float = 0.0


class AgentRuntime:
    def __init__(
        self,
        client: LLMClient,
        *,
        registry: ToolRegistry | None = None,
        sessions: SessionStore | None = None,
        retriever: Any = None,
        db_path: Path | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        settings = get_settings()
        self.client = client
        self.registry = registry or build_registry()
        self.sessions = sessions or SessionStore()
        self.confirmations = ConfirmationStore()
        self.rate_limiter = RateLimiter()
        self.retriever = retriever
        self.db_path = db_path
        self.trace_sink = trace_sink or TraceSink(settings.trace_path)
        self._tool_specs = [
            ToolSpec(name=s["name"], description=s["description"], parameters=s["parameters"])
            for s in self.registry.specs(db_path=db_path)
        ]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_turn(
        self,
        session_id: str,
        user_message: str,
        actor: str = "user",
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> TurnResult:
        """Run one turn.

        `on_event` receives progress events (tool started/finished, guardrail
        fired) as they happen, which is what the SSE endpoint and the UI trace
        panel consume. It is optional and never affects the result.
        """
        emit = on_event or (lambda _event: None)
        trace = TurnTrace(session_id=session_id, user_message=user_message, model=self.client.model)

        # --- input guardrails -----------------------------------------
        try:
            self.rate_limiter.check(session_id)
        except RateLimitExceeded as exc:
            trace.record_guardrail("input", "rate_limit", "blocked", str(exc))
            return self._blocked(trace, str(exc))

        verdict = check_user_message(user_message)
        if not verdict.allowed:
            trace.record_guardrail("input", verdict.rule or "input", "blocked", verdict.reason or "")
            return self._blocked(trace, verdict.reason or "That message could not be processed.")

        if verdict.injection_flagged:
            trace.record_guardrail(
                "input", "prompt_injection", "flagged", verdict.injection_detail
            )

        # A new user message is the only thing that can release a preview issued
        # in an earlier turn. Anything previewed later in *this* turn stays
        # inert, so the model cannot approve its own write. See confirm.py.
        self.confirmations.release_for_session(session_id)

        session = self.sessions.get(session_id)
        session.turn_count += 1
        session.add(UserMessage(text=verdict.text))
        if verdict.injection_flagged:
            session.add(SystemNote(text=injection.INJECTION_REMINDER))

        ctx = ToolContext(
            session_id=session_id,
            actor=actor,
            db_path=self.db_path,
            confirmations=self.confirmations,
            retriever=self.retriever,
        )

        try:
            return self._loop(session, ctx, trace, emit)
        except LLMRateLimited as exc:
            trace.record_guardrail("loop", "provider_rate_limit", "blocked", str(exc))
            return self._blocked(trace, str(exc), outcome="error")
        except LLMError as exc:
            logger.exception("Provider error")
            trace.record_guardrail("loop", "provider_error", "blocked", str(exc))
            return self._blocked(
                trace,
                "I could not reach the language model just now. Please try again in a moment.",
                outcome="error",
            )

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def _loop(
        self,
        session: SessionState,
        ctx: ToolContext,
        trace: TurnTrace,
        emit: Callable[[dict[str, Any]], None],
    ) -> TurnResult:
        budget = TurnBudget()
        usage = Usage()
        tool_payloads: list[dict[str, Any]] = []
        citations: list[str] = []
        policy_searched = False
        pending: dict[str, Any] | None = None
        system_prompt = prompts.build_system_prompt(policy_available=self.retriever is not None)

        reply = ""
        while True:
            budget.iterations_used += 1
            trace.iterations = budget.iterations_used

            exhausted = budget.exhausted_reason()
            # On the final permitted pass, take tools away so the model is forced
            # to produce prose from what it already has, instead of stopping mid-plan.
            offer_tools = exhausted is None
            if exhausted:
                trace.record_guardrail("loop", "turn_budget", "capped", exhausted)
                session.add(
                    SystemNote(
                        text=(
                            f"You have {exhausted}. Answer now using only what the tools have "
                            "already returned. If that is not enough, say what you could not "
                            "determine. Do not request more tools."
                        )
                    )
                )

            context_note = prompts.build_session_context(session)
            turn = self.client.generate(
                system=system_prompt,
                history=session.history_for_model(context_note),
                tools=self._tool_specs if offer_tools else None,
            )
            usage = usage + turn.usage
            session.add(turn.to_message())
            reply = turn.text

            if not turn.tool_calls or not offer_tools:
                break

            # --- execute the requested tools --------------------------
            allowed = turn.tool_calls[: budget.tool_calls_remaining]
            if len(allowed) < len(turn.tool_calls):
                trace.record_guardrail(
                    "tool",
                    "tool_call_budget",
                    "capped",
                    f"{len(turn.tool_calls)} requested, {len(allowed)} allowed",
                )

            for call in allowed:
                budget.tool_calls_used += 1
                emit({"type": "tool_start", "tool": call.name, "arguments": call.arguments})
                started = time.time()
                outcome = self.registry.dispatch(call.name, call.arguments, ctx)
                duration_ms = (time.time() - started) * 1000
                emit(
                    {
                        "type": "tool_end",
                        "tool": call.name,
                        "ok": outcome.ok,
                        "error_code": outcome.error_code,
                        "duration_ms": round(duration_ms, 1),
                    }
                )

                trace.record_tool(
                    tool=call.name,
                    arguments=call.arguments,
                    ok=outcome.ok,
                    error_code=outcome.error_code,
                    duration_ms=duration_ms,
                    result=outcome.data,
                )

                if outcome.ok:
                    session.observe_tool_result(call.name, outcome.data)
                    tool_payloads.append(outcome.data)
                    if call.name == "search_policy":
                        policy_searched = True
                        citations.extend(
                            p["citation"] for p in outcome.data.get("passages", [])
                        )
                    if outcome.data.get("status") == "needs_confirmation":
                        pending = {
                            "tool": call.name,
                            "summary": outcome.data.get("summary"),
                            "preview": outcome.data.get("preview"),
                            "confirm_token": outcome.data.get("confirm_token"),
                        }
                        trace.record_guardrail(
                            "tool", "write_confirmation", "blocked",
                            f"{call.name} previewed, awaiting user approval",
                        )
                    if outcome.data.get("status") == "committed":
                        pending = None

                session.add(
                    ToolResultMessage(
                        call_id=call.id,
                        name=call.name,
                        content=outcome.data,
                        is_error=outcome.is_error,
                    )
                )

        # --- output guardrails ----------------------------------------
        checked = output_guard.check_reply(
            reply,
            tool_results=tool_payloads,
            user_message=trace.user_message,
            policy_citations=citations,
            policy_was_searched=policy_searched,
        )

        if checked.should_retry:
            trace.record_guardrail(
                "output", "grounding", "flagged", "; ".join(checked.issues)
            )
            session.add(SystemNote(text=output_guard.correction_note(checked)))
            retry = self.client.generate(
                system=system_prompt,
                history=session.history_for_model(None),
                tools=None,  # no new evidence — rewrite from what is already there
            )
            usage = usage + retry.usage
            session.add(retry.to_message())
            reply = retry.text

            recheck = output_guard.check_reply(
                reply,
                tool_results=tool_payloads,
                user_message=trace.user_message,
                policy_citations=citations,
                policy_was_searched=policy_searched,
            )
            if recheck.ungrounded_codes:
                # Second attempt still unsupported — surface the uncertainty
                # rather than quietly passing it off as fact.
                trace.record_guardrail(
                    "output", "grounding", "rewritten", "caveat appended after retry"
                )
                reply = output_guard.apply_caveat(reply, recheck)

        session.trim()
        trace.input_tokens = usage.input_tokens
        trace.output_tokens = usage.output_tokens
        outcome_label = "budget_exceeded" if budget.exhausted_reason() else "ok"
        trace.finish(reply, outcome=outcome_label)
        self.trace_sink.write(trace)

        return TurnResult(
            reply=reply or "I could not produce an answer for that. Please rephrase.",
            trace_id=trace.trace_id,
            tools_used=trace.tools_used,
            tool_spans=[
                {
                    "tool": span.tool,
                    "arguments": span.arguments,
                    "ok": span.ok,
                    "error_code": span.error_code,
                    "duration_ms": span.duration_ms,
                }
                for span in trace.tool_spans
            ],
            citations=sorted(set(citations)),
            pending_confirmation=pending,
            guardrails=[
                {"stage": g.stage, "rule": g.rule, "action": g.action, "detail": g.detail}
                for g in trace.guardrails
            ],
            usage={"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            outcome=outcome_label,
            duration_ms=trace.duration_ms,
        )

    # ------------------------------------------------------------------

    def _blocked(self, trace: TurnTrace, message: str, outcome: str = "blocked") -> TurnResult:
        trace.finish(message, outcome=outcome)
        self.trace_sink.write(trace)
        return TurnResult(
            reply=message,
            trace_id=trace.trace_id,
            guardrails=[
                {"stage": g.stage, "rule": g.rule, "action": g.action, "detail": g.detail}
                for g in trace.guardrails
            ],
            outcome=outcome,
            duration_ms=trace.duration_ms,
        )
