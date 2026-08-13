"""Streamlit chat UI.

Built to make the agent's behaviour *visible*, not just usable: every turn shows
which tools ran, which guardrails fired, and what a pending write would change
before it happens. For a system whose main claim is "it is safe to point at a
real database", showing the machinery is the point.

Run with:  streamlit run src/assistant/ui/streamlit_app.py
"""

from __future__ import annotations

import streamlit as st

from assistant.agent.runtime import AgentRuntime
from assistant.config import get_settings
from assistant.db import repo
from assistant.llm import LLMError, get_client

st.set_page_config(page_title="XYZ Asset Assistant", page_icon="💻", layout="wide")

SUGGESTIONS = [
    "Show details of AST1002",
    "Where is AST1002 located?",
    "Who is using AST1002, and who is that employee's manager?",
    "Find an available laptop in Bangalore",
    "How many printers do we have in Mumbai?",
    "What is the laptop refresh cycle?",
    "Who approves a transfer between departments?",
    "Transfer AST1002 to Priya Singh",
]


@st.cache_resource(show_spinner="Starting the assistant…")
def load_runtime() -> tuple[AgentRuntime | None, str | None]:
    """Built once per process. Returns (runtime, error) so the UI can explain itself."""
    try:
        from assistant.rag.retriever import build_retriever

        try:
            retriever = build_retriever()
        except Exception as exc:  # noqa: BLE001 - optional capability
            st.warning(f"Policy search unavailable: {exc}")
            retriever = None

        return AgentRuntime(get_client(), retriever=retriever), None
    except LLMError as exc:
        return None, str(exc)


def render_sidebar(runtime: AgentRuntime | None) -> None:
    settings = get_settings()
    with st.sidebar:
        st.subheader("Session")
        st.caption(f"`{st.session_state.session_id}`")
        if st.button("Reset conversation", use_container_width=True):
            if runtime:
                runtime.sessions.reset(st.session_state.session_id)
                runtime.rate_limiter.reset(st.session_state.session_id)
            st.session_state.messages = []
            st.rerun()

        st.divider()
        st.subheader("System")
        try:
            total = repo.count_assets()
            available = repo.count_assets(status="Available")
            st.metric("Assets tracked", total)
            st.metric("Available to allocate", available)
        except repo.RepoError:
            st.error("Database not seeded. Run `make seed`.")

        st.caption(f"Model: `{settings.active_model}`")
        st.caption(f"Provider: `{settings.llm_provider}`")
        if runtime and runtime.retriever:
            mode = "hybrid (BM25 + embeddings)" if runtime.retriever.uses_embeddings else "BM25 only"
            st.caption(f"Policy index: {len(runtime.retriever.chunks)} chunks, {mode}")

        st.divider()
        st.subheader("Try asking")
        for suggestion in SUGGESTIONS:
            if st.button(suggestion, use_container_width=True, key=f"sug-{suggestion}"):
                st.session_state.pending_input = suggestion
                st.rerun()


def render_trace(entry: dict) -> None:
    """The tool-call panel — what the agent actually did to produce the answer."""
    tools = entry.get("tool_calls") or []
    guardrails = entry.get("guardrails") or []
    if not tools and not guardrails:
        return

    label = " · ".join(t["tool"] for t in tools) or "guardrails only"
    with st.expander(f"🔍 {label}", expanded=False):
        for span in tools:
            icon = "✅" if span["ok"] else "⚠️"
            st.markdown(f"{icon} **`{span['tool']}`** · {span['duration_ms']:.0f} ms")
            if span.get("arguments"):
                st.json(span["arguments"], expanded=False)
            if not span["ok"]:
                st.caption(f"returned: {span.get('error_code')}")

        for event in guardrails:
            st.markdown(
                f"🛡️ **{event['rule']}** ({event['stage']}) — _{event['action']}_"
                + (f": {event['detail']}" if event.get("detail") else "")
            )

        if entry.get("citations"):
            st.caption("Policy cited: " + ", ".join(f"`{c}`" for c in entry["citations"]))
        usage = entry.get("usage") or {}
        if usage:
            st.caption(
                f"tokens in/out: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
                f" · trace `{entry.get('trace_id')}`"
            )


def render_pending(entry: dict) -> None:
    """A write waiting for approval. Nothing has changed at this point."""
    pending = entry.get("pending_confirmation")
    if not pending:
        return

    with st.container(border=True):
        st.markdown(f"**⏸ Awaiting your confirmation** — {pending.get('summary', '')}")
        preview = pending.get("preview") or {}
        if preview:
            st.table(
                {"field": list(preview.keys()), "value": [str(v) for v in preview.values()]}
            )
        st.caption("Nothing has been changed yet.")

        left, right, _ = st.columns([1, 1, 3])
        if left.button("Confirm", type="primary", key=f"ok-{entry['trace_id']}"):
            st.session_state.pending_input = "Yes, please go ahead with that change."
            st.rerun()
        if right.button("Cancel", key=f"no-{entry['trace_id']}"):
            st.session_state.pending_input = "No, cancel that — do not make the change."
            st.rerun()


def main() -> None:
    if "session_id" not in st.session_state:
        import uuid

        st.session_state.session_id = f"ui-{uuid.uuid4().hex[:8]}"
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending_input", None)

    runtime, error = load_runtime()

    st.title("💻 Asset Management Assistant")
    st.caption(
        "Ask about any asset, person, or IT asset policy. Changes are always previewed "
        "before they are made."
    )

    render_sidebar(runtime)

    if error:
        st.error(error)
        st.info(
            "Add an NVIDIA NIM key to `.env` as `NVIDIA_API_KEY=...` "
            "(https://build.nvidia.com), then restart. "
            "The REST API and test suite work without one."
        )
        return

    # Always render the input, and capture what was typed. It must be called
    # unconditionally: the previous `pending_input or st.chat_input(...)` short-
    # circuited the widget away on any turn a Confirm/Cancel button drove, so the
    # box vanished after confirming and never came back. st.chat_input is pinned
    # to the bottom regardless of call order, so rendering it here is fine.
    typed = st.chat_input("Ask about an asset…")
    prompt = st.session_state.pending_input or typed
    st.session_state.pending_input = None

    # A new turn resolves any outstanding preview — the runtime either commits it
    # (on "yes") or discards it (on anything else). Retire the display flag on
    # prior messages *before* the history renders, so a spent Confirm/Cancel card
    # does not linger and invite a second click that does nothing.
    if prompt:
        for message in st.session_state.messages:
            message["pending_confirmation"] = None

    for entry in st.session_state.messages:
        with st.chat_message(entry["role"]):
            st.markdown(entry["content"])
            if entry["role"] == "assistant":
                render_trace(entry)
                render_pending(entry)

    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.status("Thinking…", expanded=False)

        def on_event(event: dict) -> None:
            if event["type"] == "tool_start":
                status.update(label=f"Calling `{event['tool']}`…")
            elif event["type"] == "tool_end":
                mark = "✅" if event["ok"] else "⚠️"
                status.write(f"{mark} `{event['tool']}` ({event['duration_ms']:.0f} ms)")

        assert runtime is not None
        result = runtime.run_turn(st.session_state.session_id, prompt, on_event=on_event)
        status.update(label=f"Done · {result.duration_ms:.0f} ms", state="complete")

        st.markdown(result.reply)
        entry = {
            "role": "assistant",
            "content": result.reply,
            "tool_calls": result.tool_spans,
            "guardrails": result.guardrails,
            "citations": result.citations,
            "usage": result.usage,
            "trace_id": result.trace_id,
            "pending_confirmation": result.pending_confirmation,
        }
        render_trace(entry)
        render_pending(entry)
        st.session_state.messages.append(entry)


main()
