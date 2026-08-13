"""FastAPI application.

Exposes two layers deliberately:

* `/chat` — the agent. Natural language in, grounded answer plus the full tool
  trace out.
* `/assets`, `/employees`, `/transfers` — the same operations as plain REST,
  with no model involved.

The second layer is not filler. It is the tool layer's own contract, which means
the deterministic half of the system can be tested, scripted and integrated
without paying for or depending on an LLM — and it makes clear that the model
adds a natural-language interface over a real service, rather than being the
service.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from assistant.agent.runtime import AgentRuntime
from assistant.api.models import (
    AssetOut,
    AuditEntryOut,
    ChatRequest,
    ChatResponse,
    CreateAssetRequest,
    EmployeeOut,
    HealthOut,
    RecommendationOut,
    TransferRequest,
)
from assistant.config import get_settings
from assistant.db import repo
from assistant.llm import LLMError, get_client

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _get_retriever()  # warm the policy index so the first question is not slow
    yield


app = FastAPI(
    title="XYZ Technologies — Asset Management Assistant",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Agentic assistant over the IT asset register.\n\n"
        "**`/chat`** is the agent: it decides which tools to call and returns a grounded "
        "answer along with the tools it used and any guardrails that fired.\n\n"
        "**`/assets`, `/employees`, `/transfers`** are the same capabilities as plain REST, "
        "with no model in the path.\n\n"
        "Writes through `/chat` are two-phase: the agent previews the change and the user "
        "must confirm before anything is committed."
    ),
)

_runtime: AgentRuntime | None = None
_retriever: Any = None
_runtime_error: str | None = None


def _get_retriever() -> Any:
    global _retriever
    if _retriever is None:
        try:
            from assistant.rag.retriever import build_retriever

            _retriever = build_retriever()
        except Exception:  # noqa: BLE001 - optional capability
            logger.warning("Policy retrieval unavailable", exc_info=True)
            _retriever = None
    return _retriever


def get_runtime() -> AgentRuntime:
    """Built lazily so the deterministic REST endpoints work without an API key."""
    global _runtime, _runtime_error
    if _runtime is None:
        try:
            _runtime = AgentRuntime(get_client(), retriever=_get_retriever())
            _runtime_error = None
        except LLMError as exc:
            _runtime_error = str(exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _runtime


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse, tags=["agent"])
def chat(request: ChatRequest) -> ChatResponse:
    """Ask the assistant a question, or request a change.

    A change never commits on the first call: the response carries
    `pending_confirmation`, and the user must reply approving it.
    """
    runtime = get_runtime()
    result = runtime.run_turn(request.session_id, request.message, actor=request.actor)
    return ChatResponse(
        reply=result.reply,
        session_id=request.session_id,
        trace_id=result.trace_id,
        tools_used=result.tools_used,
        tool_calls=result.tool_spans,
        citations=result.citations,
        pending_confirmation=result.pending_confirmation,
        guardrails=result.guardrails,
        usage=result.usage,
        outcome=result.outcome,
        duration_ms=result.duration_ms,
    )


@app.post("/chat/stream", tags=["agent"])
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Same as `/chat`, as Server-Sent Events.

    Emits `tool_start` / `tool_end` as the agent works, then a final `result`
    event. Useful for showing progress on multi-step questions instead of a
    silent pause.
    """
    runtime = get_runtime()
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def worker() -> None:
        try:
            result = runtime.run_turn(
                request.session_id, request.message, actor=request.actor, on_event=events.put
            )
            events.put(
                {
                    "type": "result",
                    "reply": result.reply,
                    "trace_id": result.trace_id,
                    "tools_used": result.tools_used,
                    "citations": result.citations,
                    "pending_confirmation": result.pending_confirmation,
                    "guardrails": result.guardrails,
                    "usage": result.usage,
                    "outcome": result.outcome,
                }
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            logger.exception("Streaming turn failed")
            events.put({"type": "error", "detail": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream() -> Iterator[str]:
        while True:
            event = events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/sessions/{session_id}/reset", tags=["agent"])
def reset_session(session_id: str) -> dict[str, str]:
    """Forget a conversation's history and its pending confirmations."""
    runtime = get_runtime()
    runtime.sessions.reset(session_id)
    runtime.rate_limiter.reset(session_id)
    return {"status": "reset", "session_id": session_id}


# --------------------------------------------------------------------------
# Deterministic REST — no model involved
# --------------------------------------------------------------------------


@app.get("/assets/{asset_code}", response_model=AssetOut, tags=["assets"])
def get_asset(asset_code: str) -> AssetOut:
    try:
        row = repo.get_asset(asset_code)
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AssetOut(**{k: row[k] for k in AssetOut.model_fields if k in row})


@app.get("/assets", response_model=list[AssetOut], tags=["assets"])
def list_assets(
    category: str | None = None,
    location: str | None = None,
    status: str | None = None,
    employee_name: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
) -> list[AssetOut]:
    rows = repo.search_assets(
        category=category,
        location=location,
        status=status,
        employee_name=employee_name,
        limit=limit,
    )
    return [AssetOut(**{k: row[k] for k in AssetOut.model_fields if k in row}) for row in rows]


@app.post("/assets", response_model=AssetOut, status_code=201, tags=["assets"])
def create_asset(request: CreateAssetRequest) -> AssetOut:
    """Register an asset. The code is assigned by the server."""
    try:
        row = repo.create_asset(**request.model_dump())
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except repo.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AssetOut(**{k: row[k] for k in AssetOut.model_fields if k in row})


@app.get("/recommendations", response_model=list[RecommendationOut], tags=["assets"])
def recommendations(
    category: str = Query(description="e.g. Laptop"),
    location: str | None = Query(default=None, description="e.g. Bangalore"),
    limit: int = Query(default=5, ge=1, le=50),
) -> list[RecommendationOut]:
    """Available stock only — never assigned or in-repair assets."""
    rows = repo.recommend_assets(category=category, location=location, limit=limit)
    return [
        RecommendationOut(
            asset_code=row["asset_code"],
            asset_name=row["asset_name"],
            category=row["category"],
            location=row["location"],
            condition=row["condition"],
            purchase_date=row["purchase_date"],
            reason=row["reason"],
        )
        for row in rows
    ]


@app.get("/employees/{name}", response_model=EmployeeOut, tags=["employees"])
def get_employee(name: str) -> EmployeeOut:
    try:
        profile = repo.get_employee(name=name)
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except repo.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EmployeeOut(**{k: profile[k] for k in EmployeeOut.model_fields if k in profile})


@app.post("/transfers", response_model=AssetOut, tags=["transfers"])
def transfer(request: TransferRequest) -> AssetOut:
    """Reassign an asset.

    Note the asymmetry with `/chat`: this endpoint commits immediately, because
    an explicit API call *is* the confirmation. The two-phase gate exists for the
    agent path, where the intent was inferred from natural language.
    """
    try:
        row = repo.transfer_asset(**request.model_dump())
    except repo.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except repo.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AssetOut(**{k: row[k] for k in AssetOut.model_fields if k in row})


@app.get("/audit", response_model=list[AuditEntryOut], tags=["transfers"])
def audit_log(
    asset_code: str | None = None, limit: int = Query(default=20, ge=1, le=50)
) -> list[AuditEntryOut]:
    """Every mutation, newest first."""
    return [AuditEntryOut(**row) for row in repo.get_audit_log(asset_code, limit)]


# --------------------------------------------------------------------------


@app.get("/healthz", response_model=HealthOut, tags=["ops"])
def healthz() -> HealthOut:
    settings = get_settings()
    try:
        repo.count_assets()
        database_ok = True
    except repo.RepoError:
        database_ok = False

    retriever = _get_retriever()
    return HealthOut(
        status="ok" if database_ok else "degraded",
        database=database_ok,
        policy_index=retriever is not None,
        policy_chunks=len(retriever.chunks) if retriever else 0,
        embeddings=bool(retriever and retriever.uses_embeddings),
        llm_provider=settings.llm_provider,
        llm_configured=settings.llm_configured,
        model=settings.active_model,
    )
