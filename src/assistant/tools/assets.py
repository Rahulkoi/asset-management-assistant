"""Read tools over the asset table."""

from __future__ import annotations

from typing import Any

from assistant.db import repo
from assistant.tools.registry import ToolContext, ToolOutcome
from assistant.tools.schemas import LookupAssetParams, SearchAssetsParams

# Fields returned to the model. Kept narrow on purpose: fewer tokens, and no
# column reaches the conversation unless a question could actually need it.
_ASSET_FIELDS = (
    "asset_code",
    "asset_name",
    "category",
    "location",
    "status",
    "condition",
    "purchase_date",
    "assigned_to",
)


def _project(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in _ASSET_FIELDS if key in row}


def lookup_asset(params: LookupAssetParams, ctx: ToolContext) -> ToolOutcome:
    row = repo.get_asset(params.asset_code, db_path=ctx.db_path)
    asset = _project(row)

    # The holder and their manager come back with the asset so the multi-step
    # question resolves in one call rather than three chained ones.
    if row.get("assigned_to"):
        asset["holder"] = {
            "name": row["assigned_to"],
            "department": row.get("assigned_to_department"),
            "email": row.get("assigned_to_email"),
            "base_location": row.get("assigned_to_location"),
            "manager": row.get("manager_name"),
            "manager_email": row.get("manager_email"),
        }
    else:
        asset["holder"] = None
        asset["note"] = f"{asset['asset_code']} is not assigned to anyone (status: {asset['status']})."

    return ToolOutcome.success(asset=asset)


def search_assets(params: SearchAssetsParams, ctx: ToolContext) -> ToolOutcome:
    rows = repo.search_assets(
        category=params.category,
        location=params.location,
        status=params.status,
        employee_name=params.employee_name,
        name_contains=params.name_contains,
        purchased_after=params.purchased_after,
        purchased_before=params.purchased_before,
        limit=params.limit,
        db_path=ctx.db_path,
    )
    results = [_project(row) for row in rows]

    if not results:
        # An empty result is a real answer, not a failure — but say so in a way
        # that stops the model inventing rows to fill the gap.
        applied = {
            key: value
            for key, value in params.model_dump(exclude_none=True).items()
            if key != "limit"
        }
        return ToolOutcome.success(
            count=0,
            results=[],
            note=(
                f"No assets match {applied}. Report this to the user plainly; "
                "do not invent assets."
            ),
        )

    return ToolOutcome.success(count=len(results), results=results)
