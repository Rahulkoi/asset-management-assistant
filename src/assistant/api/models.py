"""Request and response models for the HTTP API.

These are what render in the OpenAPI docs, so field descriptions are written for
someone reading /docs rather than for the code.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(
        default="default",
        description="Conversation identifier. Reuse it across turns to keep context.",
    )
    message: str = Field(description="The user's message.")
    actor: str = Field(default="user", description="Who is asking; recorded in the audit log.")


class ToolSpanOut(BaseModel):
    tool: str
    arguments: dict[str, Any]
    ok: bool
    error_code: str | None = None
    duration_ms: float


class GuardrailOut(BaseModel):
    stage: str = Field(description="input | tool | loop | output")
    rule: str
    action: str = Field(description="blocked | flagged | rewritten | capped")
    detail: str = ""


class PendingConfirmationOut(BaseModel):
    tool: str
    summary: str | None = None
    preview: dict[str, Any] | None = None
    confirm_token: str | None = Field(
        default=None,
        description=(
            "Opaque, single-use, short-lived. The agent needs it to commit the "
            "change; it is surfaced here so a UI can offer a confirm button."
        ),
    )


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    trace_id: str = Field(description="Correlates with the JSONL trace log.")
    tools_used: list[str] = []
    tool_calls: list[ToolSpanOut] = []
    citations: list[str] = Field(default=[], description="Policy passages cited, if any.")
    pending_confirmation: PendingConfirmationOut | None = Field(
        default=None,
        description="Set when a write is previewed and waiting for the user to approve it.",
    )
    guardrails: list[GuardrailOut] = []
    usage: dict[str, int] = {}
    outcome: str = Field(default="ok", description="ok | blocked | error | budget_exceeded")
    duration_ms: float = 0.0


class AssetOut(BaseModel):
    asset_code: str
    asset_name: str
    category: str
    location: str
    status: str
    condition: str
    purchase_date: str
    assigned_to: str | None = None
    source: str = Field(description="'xlsx' if from the supplied spreadsheet, else 'synthetic'.")


class EmployeeOut(BaseModel):
    employee_id: str
    full_name: str
    email: str
    department: str
    location: str
    manager_name: str | None = None
    direct_reports: list[str] = []
    assets_held: list[dict[str, Any]] = []


class RecommendationOut(BaseModel):
    asset_code: str
    asset_name: str
    category: str
    location: str
    condition: str
    purchase_date: str
    reason: str


class CreateAssetRequest(BaseModel):
    asset_name: str
    category: str
    location: str
    assign_to_employee: str | None = None
    purchase_date: str | None = None
    condition: str = "New"
    actor: str = "api"


class TransferRequest(BaseModel):
    asset_code: str
    to_employee: str
    reason: str | None = None
    actor: str = "api"
    idempotency_key: str | None = Field(
        default=None, description="Optional replay protection for retried requests."
    )


class AuditEntryOut(BaseModel):
    id: int
    asset_code: str
    action: str
    from_employee: str | None = None
    to_employee: str | None = None
    reason: str | None = None
    actor: str
    transferred_at: str


class HealthOut(BaseModel):
    status: str
    database: bool
    policy_index: bool
    policy_chunks: int = 0
    embeddings: bool = False
    llm_provider: str
    llm_configured: bool
    model: str
