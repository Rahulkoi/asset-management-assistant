"""Write tools — add an asset, transfer an asset.

Both are two-phase. Called without a `confirm_token` they change nothing: they
validate, build a human-readable preview of exactly what would change, and hand
back a token. Only a second call carrying that token commits.

That shape is what makes an agent safe to point at a real database. The model
can be wrong, or manipulated by text it read, and still cannot mutate anything
the user has not seen and approved.
"""

from __future__ import annotations

from typing import Any

from assistant.db import repo
from assistant.tools.registry import ToolContext, ToolOutcome
from assistant.tools.schemas import AddAssetParams, TransferAssetParams

_VALID_CONDITIONS = {"New", "Good", "Fair", "Poor"}


def _needs_confirmation(
    ctx: ToolContext, tool_name: str, params: Any, preview: dict[str, Any], summary: str
) -> ToolOutcome:
    record = ctx.confirmations.issue(
        session_id=ctx.session_id,
        tool_name=tool_name,
        payload=params.model_dump(),
        preview=preview,
    )
    return ToolOutcome.success(
        status="needs_confirmation",
        summary=summary,
        preview=preview,
        confirm_token=record.token,
        next_step=(
            "Nothing has changed yet. Show the user this exact change and ask them to "
            "confirm. If they agree, call this tool again with identical arguments plus "
            "confirm_token. If they decline or change any detail, do not reuse the token."
        ),
    )


# --------------------------------------------------------------------------


def add_asset(params: AddAssetParams, ctx: ToolContext) -> ToolOutcome:
    if params.condition not in _VALID_CONDITIONS:
        return ToolOutcome.failure(
            f"condition must be one of {sorted(_VALID_CONDITIONS)}, got {params.condition!r}.",
            code="invalid_arguments",
        )

    # Validate the holder before previewing, so the user is never asked to
    # approve a change that would then fail.
    holder = None
    if params.assign_to_employee:
        profile = repo.get_employee(name=params.assign_to_employee, db_path=ctx.db_path)
        holder = profile["full_name"]

    if params.confirm_token is None:
        preview = {
            "action": "add_asset",
            "asset_code": f"{repo.next_asset_code(ctx.db_path)} (assigned by the system)",
            "asset_name": params.asset_name,
            "category": params.category,
            "location": params.location,
            "condition": params.condition,
            "purchase_date": params.purchase_date or "today",
            "assign_to": holder or "nobody — added as available stock",
        }
        summary = (
            f"Add a {params.category} '{params.asset_name}' at {params.location}"
            + (f", assigned to {holder}" if holder else " as unassigned stock")
        )
        return _needs_confirmation(ctx, "add_asset", params, preview, summary)

    ctx.confirmations.redeem(
        token=params.confirm_token,
        session_id=ctx.session_id,
        tool_name="add_asset",
        payload=params.model_dump(),
    )

    created = repo.create_asset(
        asset_name=params.asset_name,
        category=params.category,
        location=params.location,
        assign_to_employee=params.assign_to_employee,
        purchase_date=params.purchase_date,
        condition=params.condition,
        actor=ctx.actor,
        session_id=ctx.session_id,
        idempotency_key=params.confirm_token,
        db_path=ctx.db_path,
    )
    return ToolOutcome.success(
        status="committed",
        summary=f"Added {created['asset_code']} ({created['asset_name']}) at {created['location']}.",
        asset={
            "asset_code": created["asset_code"],
            "asset_name": created["asset_name"],
            "category": created["category"],
            "location": created["location"],
            "status": created["status"],
            "assigned_to": created["assigned_to"],
        },
    )


# --------------------------------------------------------------------------


def transfer_asset(params: TransferAssetParams, ctx: ToolContext) -> ToolOutcome:
    asset = repo.get_asset(params.asset_code, db_path=ctx.db_path)
    target = repo.get_employee(name=params.to_employee, db_path=ctx.db_path)

    if asset["assigned_to_id"] == target["employee_id"]:
        return ToolOutcome.failure(
            f"{asset['asset_code']} is already assigned to {target['full_name']}. Nothing to do.",
            code="conflict",
        )
    if asset["status"] == "Retired":
        return ToolOutcome.failure(
            f"{asset['asset_code']} is retired and cannot be transferred.", code="conflict"
        )

    if params.confirm_token is None:
        preview = {
            "action": "transfer_asset",
            "asset_code": asset["asset_code"],
            "asset_name": asset["asset_name"],
            "from": asset["assigned_to"] or "unassigned",
            "to": target["full_name"],
            "to_department": target["department"],
            "location": asset["location"],
            "reason": params.reason or "not given",
            # Captured server-side so the commit can detect the row moving in
            # between. Never shown to the model — it is not the model's concern.
            "_expected_updated_at": asset["updated_at"],
        }
        summary = (
            f"Transfer {asset['asset_code']} ({asset['asset_name']}) from "
            f"{asset['assigned_to'] or 'unassigned'} to {target['full_name']}"
        )
        outcome = _needs_confirmation(ctx, "transfer_asset", params, preview, summary)
        outcome.data["preview"] = {
            k: v for k, v in preview.items() if not k.startswith("_")
        }
        return outcome

    record = ctx.confirmations.redeem(
        token=params.confirm_token,
        session_id=ctx.session_id,
        tool_name="transfer_asset",
        payload=params.model_dump(),
    )

    updated = repo.transfer_asset(
        asset_code=params.asset_code,
        to_employee=params.to_employee,
        reason=params.reason,
        expected_updated_at=record.preview.get("_expected_updated_at"),
        actor=ctx.actor,
        session_id=ctx.session_id,
        idempotency_key=params.confirm_token,
        db_path=ctx.db_path,
    )
    return ToolOutcome.success(
        status="committed",
        summary=(
            f"{updated['asset_code']} is now assigned to {updated['assigned_to']}."
        ),
        asset={
            "asset_code": updated["asset_code"],
            "asset_name": updated["asset_name"],
            "location": updated["location"],
            "status": updated["status"],
            "assigned_to": updated["assigned_to"],
        },
    )
