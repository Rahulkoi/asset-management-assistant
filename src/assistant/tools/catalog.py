"""The agent's complete tool surface.

Seven tools, fixed and typed. There is deliberately **no** free-form SQL tool:
a text-to-SQL escape hatch would hand the model unbounded reach over the
database, make injection a live concern, and make tool selection unmeasurable.
A fixed surface costs a little expressiveness and buys auditability.

Descriptions are written for the model, not for a developer — they say when to
reach for the tool, which is what actually drives correct selection.
"""

from __future__ import annotations

from assistant.tools import assets, employees, policy, recommend, writes
from assistant.tools.registry import ToolDefinition, ToolRegistry
from assistant.tools.schemas import (
    AddAssetParams,
    LookupAssetParams,
    LookupEmployeeParams,
    RecommendAssetsParams,
    SearchAssetsParams,
    SearchPolicyParams,
    TransferAssetParams,
)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            name="lookup_asset",
            description=(
                "Get the full record for one asset by its code (e.g. AST1002). Use this "
                "whenever the user names a specific asset code. The result includes who "
                "holds the asset and that person's manager, so you do not need a second "
                "call to answer questions about the holder's manager."
            ),
            params_model=LookupAssetParams,
            handler=assets.lookup_asset,
        )
    )

    registry.register(
        ToolDefinition(
            name="search_assets",
            description=(
                "Find assets matching filters: category, location, status, holder name, "
                "asset name substring, or purchase-date range. Use this for questions "
                "about groups of assets ('how many printers in Mumbai', 'what does Rahul "
                "have'). Do not use it to find something to give someone — use "
                "recommend_assets for that."
            ),
            params_model=SearchAssetsParams,
            handler=assets.search_assets,
            enum_fields=("category", "location", "status"),
        )
    )

    registry.register(
        ToolDefinition(
            name="lookup_employee",
            description=(
                "Get an employee's department, base location, manager, direct reports and "
                "the assets they currently hold. Use this when the user asks about a "
                "person rather than an asset."
            ),
            params_model=LookupEmployeeParams,
            handler=employees.lookup_employee,
        )
    )

    registry.register(
        ToolDefinition(
            name="recommend_assets",
            description=(
                "Find spare, unassigned assets that could be given to someone — 'find an "
                "available laptop in Bangalore'. Returns only genuinely available stock, "
                "ranked by how close it is to the requested location. Never returns "
                "assigned or in-repair assets."
            ),
            params_model=RecommendAssetsParams,
            handler=recommend.recommend_assets,
            enum_fields=("category", "location"),
        )
    )

    registry.register(
        ToolDefinition(
            name="search_policy",
            description=(
                "Search the written IT asset policy: eligibility, the transfer approval "
                "process, warranty and AMC cover, the laptop refresh cycle, loss and "
                "damage reporting, work-from-home equipment, and offboarding. Use this "
                "for questions about rules and process rather than about specific assets. "
                "Always cite the passages you use."
            ),
            params_model=SearchPolicyParams,
            handler=policy.search_policy,
        )
    )

    registry.register(
        ToolDefinition(
            name="add_asset",
            description=(
                "Register a new asset in the system. Call it first WITHOUT confirm_token "
                "to get a preview of exactly what would be created — this changes nothing. "
                "Show that preview to the user, and only if they explicitly approve, call "
                "again with the same arguments plus the confirm_token from the preview. "
                "The asset code is assigned by the system; never invent one."
            ),
            params_model=AddAssetParams,
            handler=writes.add_asset,
            kind="write",
            enum_fields=("category", "location", "condition"),
        )
    )

    registry.register(
        ToolDefinition(
            name="transfer_asset",
            description=(
                "Move an asset from its current holder to another employee. Call it first "
                "WITHOUT confirm_token to get a preview of the change — this changes "
                "nothing. Show the preview to the user, and only if they explicitly "
                "approve, call again with the same arguments plus the confirm_token from "
                "the preview."
            ),
            params_model=TransferAssetParams,
            handler=writes.transfer_asset,
            kind="write",
        )
    )

    return registry
