"""HTTP API tests.

The deterministic endpoints are covered fully. `/chat` is covered by injecting a
scripted runtime, so the API contract is verified without a provider key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assistant.agent.memory import SessionStore
from assistant.agent.runtime import AgentRuntime
from assistant.api import app as app_module
from assistant.llm import LLMError
from assistant.llm.base import AssistantTurn, ToolCall
from assistant.obs.trace import TraceSink
from tests.test_runtime import ScriptedClient


@pytest.fixture()
def client(scratch_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Point every repo call at a throwaway database."""
    import assistant.db.repo as repo_module

    original = repo_module.connect

    def scoped_connect(db_path: Path | None = None):
        return original(db_path or scratch_db)

    monkeypatch.setattr(repo_module, "connect", scoped_connect)
    monkeypatch.setattr(app_module, "_runtime", None)
    return TestClient(app_module.app)


@pytest.fixture()
def chat_client(client: TestClient, scratch_db: Path, tmp_path: Path, monkeypatch):
    """A client whose agent is a scripted fake."""

    def install(turns: list[AssistantTurn]) -> TestClient:
        runtime = AgentRuntime(
            ScriptedClient(turns),
            sessions=SessionStore(),
            db_path=scratch_db,
            trace_sink=TraceSink(tmp_path / "traces.jsonl"),
        )
        monkeypatch.setattr(app_module, "_runtime", runtime)
        return client

    return install


# --------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------


def test_healthz_reports_component_state(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["database"] is True
    assert body["policy_index"] is True
    assert body["policy_chunks"] > 0
    assert "llm_configured" in body


def test_openapi_schema_is_served(client: TestClient) -> None:
    """The API-documentation deliverable is generated, not hand-maintained."""
    schema = client.get("/openapi.json").json()
    assert "/chat" in schema["paths"]
    assert "/assets/{asset_code}" in schema["paths"]
    assert "/transfers" in schema["paths"]


# --------------------------------------------------------------------------
# Deterministic endpoints
# --------------------------------------------------------------------------


def test_get_asset(client: TestClient) -> None:
    body = client.get("/assets/AST1002").json()
    assert body["asset_name"] == "Lenovo ThinkPad X1"
    assert body["assigned_to"] == "Amit Kumar"
    assert body["source"] == "xlsx"


def test_get_unknown_asset_is_404(client: TestClient) -> None:
    assert client.get("/assets/AST9999").status_code == 404


def test_list_assets_with_filters(client: TestClient) -> None:
    rows = client.get("/assets", params={"category": "Laptop", "location": "Bangalore"}).json()
    assert rows
    assert all(r["category"] == "Laptop" and r["location"] == "Bangalore" for r in rows)


def test_list_assets_rejects_oversized_limit(client: TestClient) -> None:
    assert client.get("/assets", params={"limit": 5000}).status_code == 422


def test_recommendations_exclude_unavailable(client: TestClient) -> None:
    rows = client.get(
        "/recommendations", params={"category": "Laptop", "location": "Bangalore"}
    ).json()
    assert rows
    assert "AST1033" not in {r["asset_code"] for r in rows}  # in repair


def test_get_employee_includes_manager(client: TestClient) -> None:
    body = client.get("/employees/Amit Kumar").json()
    assert body["manager_name"] == "Priya Singh"
    assert body["department"] == "Engineering"


def test_ambiguous_employee_is_409(client: TestClient) -> None:
    assert client.get("/employees/a").status_code == 409


def test_create_and_transfer_asset(client: TestClient) -> None:
    created = client.post(
        "/assets",
        json={"asset_name": "Dell Latitude 7450", "category": "Laptop", "location": "Pune"},
    )
    assert created.status_code == 201
    code = created.json()["asset_code"]

    moved = client.post("/transfers", json={"asset_code": code, "to_employee": "Priya Singh"})
    assert moved.status_code == 200
    assert moved.json()["assigned_to"] == "Priya Singh"

    entries = client.get("/audit", params={"asset_code": code}).json()
    assert [e["action"] for e in entries] == ["transfer", "create"]


def test_transfer_to_unknown_employee_is_404(client: TestClient) -> None:
    response = client.post(
        "/transfers", json={"asset_code": "AST1002", "to_employee": "Nobody McGhost"}
    )
    assert response.status_code == 404


def test_transfer_to_current_holder_is_409(client: TestClient) -> None:
    response = client.post(
        "/transfers", json={"asset_code": "AST1002", "to_employee": "Amit Kumar"}
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------
# Agent endpoint
# --------------------------------------------------------------------------


def test_chat_returns_reply_and_trace(chat_client) -> None:
    client = chat_client(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="lookup_asset", arguments={"asset_code": "AST1002"})]
            ),
            AssistantTurn(text="AST1002 is in Bangalore, held by Amit Kumar."),
        ]
    )
    body = client.post("/chat", json={"session_id": "s1", "message": "where is AST1002?"}).json()

    assert "Bangalore" in body["reply"]
    assert body["tools_used"] == ["lookup_asset"]
    assert body["tool_calls"][0]["ok"] is True
    assert body["trace_id"]


def test_chat_surfaces_pending_confirmation(chat_client) -> None:
    client = chat_client(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="transfer_asset",
                        arguments={"asset_code": "AST1002", "to_employee": "Priya Singh"},
                    )
                ]
            ),
            AssistantTurn(text="Shall I make that change?"),
        ]
    )
    body = client.post("/chat", json={"session_id": "s1", "message": "move AST1002"}).json()

    assert body["pending_confirmation"] is not None
    assert body["pending_confirmation"]["tool"] == "transfer_asset"
    # And the database is untouched.
    assert client.get("/assets/AST1002").json()["assigned_to"] == "Amit Kumar"


def test_chat_without_provider_key_is_503(client: TestClient, monkeypatch) -> None:
    """A missing key must be an honest 503, not a stack trace.

    The keys are cleared explicitly rather than relying on the developer's .env
    being empty — otherwise this passes or fails depending on who runs it.
    """
    monkeypatch.setattr(app_module, "_runtime", None)
    monkeypatch.setattr(
        app_module,
        "get_client",
        lambda *a, **k: (_ for _ in ()).throw(LLMError("No API key set. Add one to .env.")),
    )
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 503
    assert "key" in response.json()["detail"].lower()


def test_chat_stream_emits_events(chat_client) -> None:
    client = chat_client(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="1", name="lookup_asset", arguments={"asset_code": "AST1002"})]
            ),
            AssistantTurn(text="Found it."),
        ]
    )
    with client.stream(
        "POST", "/chat/stream", json={"session_id": "s1", "message": "where is AST1002?"}
    ) as response:
        payload = "".join(response.iter_text())

    assert "tool_start" in payload
    assert "tool_end" in payload
    assert '"type": "result"' in payload


def test_session_reset(chat_client) -> None:
    client = chat_client([AssistantTurn(text="ok")])
    assert client.post("/sessions/s1/reset").json()["status"] == "reset"
