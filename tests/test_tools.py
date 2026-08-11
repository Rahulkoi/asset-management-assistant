"""Tool-layer tests: dispatch, validation, and the write confirmation gate.

No LLM involved — these assert the behaviour the agent depends on, so a failure
here is unambiguous rather than being blamed on model non-determinism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.agent.confirm import ConfirmationStore
from assistant.db import repo
from assistant.tools.catalog import build_registry
from assistant.tools.registry import ToolContext
from assistant.tools.schemas import SearchAssetsParams, to_provider_schema


@pytest.fixture()
def registry():
    return build_registry()


@pytest.fixture()
def ctx(scratch_db: Path) -> ToolContext:
    return ToolContext(
        session_id="test-session",
        actor="tester",
        db_path=scratch_db,
        confirmations=ConfirmationStore(),
    )


# --------------------------------------------------------------------------
# Dispatch and validation
# --------------------------------------------------------------------------


def test_all_seven_tools_registered(registry) -> None:
    assert registry.names() == [
        "add_asset",
        "lookup_asset",
        "lookup_employee",
        "recommend_assets",
        "search_assets",
        "search_policy",
        "transfer_asset",
    ]


def test_unknown_tool_returns_error_not_exception(registry, ctx) -> None:
    """A hallucinated tool name must be recoverable, not fatal."""
    outcome = registry.dispatch("delete_everything", {}, ctx)
    assert outcome.is_error
    assert outcome.error_code == "unknown_tool"
    assert "lookup_asset" in outcome.data["error"]  # tells the model what does exist


def test_invalid_arguments_are_rejected_before_the_handler_runs(registry, ctx) -> None:
    outcome = registry.dispatch("lookup_asset", {"wrong_field": "AST1002"}, ctx)
    assert outcome.is_error
    assert outcome.error_code == "invalid_arguments"


def test_malformed_date_is_rejected(registry, ctx) -> None:
    outcome = registry.dispatch("search_assets", {"purchased_after": "last Tuesday"}, ctx)
    assert outcome.is_error
    assert outcome.error_code == "invalid_arguments"


def test_row_limit_is_enforced_at_the_schema_boundary(registry, ctx) -> None:
    outcome = registry.dispatch("search_assets", {"limit": 5000}, ctx)
    assert outcome.is_error, "an unbounded limit must be rejected, not silently clamped"


def test_missing_asset_is_a_clean_not_found(registry, ctx) -> None:
    outcome = registry.dispatch("lookup_asset", {"asset_code": "AST9999"}, ctx)
    assert outcome.is_error
    assert outcome.error_code == "not_found"


# --------------------------------------------------------------------------
# Provider schema generation
# --------------------------------------------------------------------------


def test_provider_schema_has_no_unsupported_keywords(registry) -> None:
    """Gemini accepts only a subset of OpenAPI; $defs/anyOf/title must be gone."""
    for spec in registry.specs():
        rendered = repr(spec["parameters"])
        assert "$defs" not in rendered
        assert "anyOf" not in rendered
        assert "$ref" not in rendered
        assert "'title'" not in rendered


def test_optional_fields_become_nullable_not_anyof() -> None:
    schema = to_provider_schema(SearchAssetsParams)
    assert schema["properties"]["category"]["type"] == "string"
    assert schema["properties"]["category"]["nullable"] is True


def test_enums_are_injected_from_live_data(registry, scratch_db: Path) -> None:
    """The model is steered towards categories that actually exist."""
    specs = {spec["name"]: spec for spec in registry.specs(db_path=scratch_db)}
    category = specs["search_assets"]["parameters"]["properties"]["category"]
    assert "Laptop" in category["enum"]
    assert "Tablet" not in category["enum"]


# --------------------------------------------------------------------------
# Read tools answer the brief's five functional requirements
# --------------------------------------------------------------------------


def test_lookup_returns_holder_and_manager_in_one_call(registry, ctx) -> None:
    outcome = registry.dispatch("lookup_asset", {"asset_code": "AST1002"}, ctx)
    assert outcome.ok
    assert outcome.data["asset"]["holder"]["name"] == "Amit Kumar"
    assert outcome.data["asset"]["holder"]["manager"] == "Priya Singh"


def test_recommend_returns_only_available_stock(registry, ctx) -> None:
    outcome = registry.dispatch(
        "recommend_assets", {"category": "Laptop", "location": "Bangalore"}, ctx
    )
    assert outcome.ok
    codes = {r["asset_code"] for r in outcome.data["recommendations"]}
    assert codes
    assert "AST1033" not in codes  # in repair


def test_empty_search_says_so_instead_of_returning_nothing(registry, ctx) -> None:
    outcome = registry.dispatch(
        "search_assets", {"category": "Laptop", "location": "Kolkata"}, ctx
    )
    assert outcome.ok
    assert outcome.data["count"] == 0
    assert "do not invent" in outcome.data["note"]


def test_policy_tool_degrades_gracefully_without_an_index(registry, ctx) -> None:
    outcome = registry.dispatch("search_policy", {"query": "laptop refresh"}, ctx)
    assert outcome.is_error
    assert outcome.error_code == "unavailable"


# --------------------------------------------------------------------------
# The write gate. This is the guardrail that matters most.
# --------------------------------------------------------------------------


def approve(ctx) -> None:
    """Stand in for the user's next turn.

    A preview is inert until a later user turn releases it — the runtime does
    this at the top of `run_turn`. These tests drive the tool layer directly,
    with no runtime, so they have to say explicitly where the user's approval
    falls. A commit test with no `approve()` between preview and commit is
    asserting that the model can approve its own write, which it cannot.
    """
    ctx.confirmations.release_for_session(ctx.session_id)


def test_transfer_without_token_changes_nothing(registry, ctx, scratch_db: Path) -> None:
    outcome = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Priya Singh"}, ctx
    )
    assert outcome.ok
    assert outcome.data["status"] == "needs_confirmation"
    assert outcome.data["confirm_token"]
    # The database is untouched.
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Amit Kumar"


def test_preview_does_not_leak_internal_fields_to_the_model(registry, ctx) -> None:
    outcome = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Priya Singh"}, ctx
    )
    assert not any(key.startswith("_") for key in outcome.data["preview"])


def test_transfer_commits_only_with_a_valid_token(registry, ctx, scratch_db: Path) -> None:
    args = {"asset_code": "AST1002", "to_employee": "Priya Singh"}
    preview = registry.dispatch("transfer_asset", dict(args), ctx)

    approve(ctx)
    committed = registry.dispatch(
        "transfer_asset", {**args, "confirm_token": preview.data["confirm_token"]}, ctx
    )
    assert committed.ok
    assert committed.data["status"] == "committed"
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Priya Singh"


def test_token_is_inert_until_the_user_takes_another_turn(registry, ctx, scratch_db: Path) -> None:
    """The regression that mattered: preview and commit inside one turn.

    The token is real and the arguments match — the only thing missing is the
    user, which is exactly the thing that must not be optional.
    """
    args = {"asset_code": "AST1002", "to_employee": "Priya Singh"}
    preview = registry.dispatch("transfer_asset", dict(args), ctx)

    replayed = registry.dispatch(
        "transfer_asset", {**args, "confirm_token": preview.data["confirm_token"]}, ctx
    )
    assert replayed.is_error
    assert replayed.error_code == "confirmation_required"
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Amit Kumar"

    # ...and the same token works once the user has actually been asked.
    approve(ctx)
    committed = registry.dispatch(
        "transfer_asset", {**args, "confirm_token": preview.data["confirm_token"]}, ctx
    )
    assert committed.ok
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Priya Singh"


def test_invented_token_is_refused(registry, ctx, scratch_db: Path) -> None:
    """The model must not be able to talk its way past the gate."""
    outcome = registry.dispatch(
        "transfer_asset",
        {"asset_code": "AST1002", "to_employee": "Priya Singh", "confirm_token": "not-a-real-token"},
        ctx,
    )
    assert outcome.is_error
    assert outcome.error_code == "confirmation_required"
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Amit Kumar"


def test_token_cannot_be_reused(registry, ctx) -> None:
    args = {"asset_code": "AST1002", "to_employee": "Priya Singh"}
    preview = registry.dispatch("transfer_asset", dict(args), ctx)
    token = preview.data["confirm_token"]

    approve(ctx)
    registry.dispatch("transfer_asset", {**args, "confirm_token": token}, ctx)
    replay = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Arjun Rao", "confirm_token": token}, ctx
    )
    assert replay.is_error


def test_token_cannot_be_used_for_different_arguments(registry, ctx, scratch_db: Path) -> None:
    """Approve one transfer, then swap the target — must be caught."""
    preview = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Priya Singh"}, ctx
    )
    approve(ctx)
    swapped = registry.dispatch(
        "transfer_asset",
        {
            "asset_code": "AST1002",
            "to_employee": "Vikram Shah",  # not what the user approved
            "confirm_token": preview.data["confirm_token"],
        },
        ctx,
    )
    assert swapped.is_error
    assert "changed since" in swapped.data["error"]
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Amit Kumar"


def test_token_from_another_session_is_refused(registry, ctx, scratch_db: Path) -> None:
    preview = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Priya Singh"}, ctx
    )
    approve(ctx)
    other = ToolContext(
        session_id="someone-elses-session",
        db_path=scratch_db,
        confirmations=ctx.confirmations,
    )
    outcome = registry.dispatch(
        "transfer_asset",
        {
            "asset_code": "AST1002",
            "to_employee": "Priya Singh",
            "confirm_token": preview.data["confirm_token"],
        },
        other,
    )
    assert outcome.is_error
    assert "different conversation" in outcome.data["error"]


def test_expired_token_is_refused(registry, scratch_db: Path) -> None:
    ctx = ToolContext(
        session_id="s", db_path=scratch_db, confirmations=ConfirmationStore(ttl_seconds=-1)
    )
    preview = registry.dispatch(
        "transfer_asset", {"asset_code": "AST1002", "to_employee": "Priya Singh"}, ctx
    )
    outcome = registry.dispatch(
        "transfer_asset",
        {
            "asset_code": "AST1002",
            "to_employee": "Priya Singh",
            "confirm_token": preview.data["confirm_token"],
        },
        ctx,
    )
    assert outcome.is_error


def test_add_asset_preview_then_commit(registry, ctx, scratch_db: Path) -> None:
    args = {"asset_name": "Dell Latitude 7450", "category": "Laptop", "location": "Pune"}
    preview = registry.dispatch("add_asset", dict(args), ctx)
    assert preview.data["status"] == "needs_confirmation"
    before = repo.count_assets(db_path=scratch_db)

    approve(ctx)
    committed = registry.dispatch(
        "add_asset", {**args, "confirm_token": preview.data["confirm_token"]}, ctx
    )
    assert committed.ok
    assert repo.count_assets(db_path=scratch_db) == before + 1


def test_add_asset_validates_holder_before_asking_for_approval(registry, ctx) -> None:
    """Never ask a user to approve something that will then fail."""
    outcome = registry.dispatch(
        "add_asset",
        {
            "asset_name": "X1",
            "category": "Laptop",
            "location": "Pune",
            "assign_to_employee": "Nobody McGhost",
        },
        ctx,
    )
    assert outcome.is_error
    assert outcome.error_code == "not_found"


def test_writes_are_audited(registry, ctx, scratch_db: Path) -> None:
    args = {"asset_code": "AST1002", "to_employee": "Priya Singh", "reason": "role change"}
    preview = registry.dispatch("transfer_asset", dict(args), ctx)
    approve(ctx)
    registry.dispatch("transfer_asset", {**args, "confirm_token": preview.data["confirm_token"]}, ctx)

    entry = repo.get_audit_log("AST1002", db_path=scratch_db)[0]
    assert entry["action"] == "transfer"
    assert entry["actor"] == "tester"
    assert entry["reason"] == "role change"
