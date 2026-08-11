"""Asset recommendation.

The ranking is deliberately in code rather than left to the model: availability
is a factual constraint, and a model that "recommends" an in-repair or already
assigned machine is worse than useless in an asset system.
"""

from __future__ import annotations

from assistant.db import repo
from assistant.tools.registry import ToolContext, ToolOutcome
from assistant.tools.schemas import RecommendAssetsParams


def recommend_assets(params: RecommendAssetsParams, ctx: ToolContext) -> ToolOutcome:
    results = repo.recommend_assets(
        category=params.category,
        location=params.location,
        limit=params.limit,
        db_path=ctx.db_path,
    )

    if not results:
        alternatives = repo.search_assets(
            category=params.category, status="Available", limit=5, db_path=ctx.db_path
        )
        if alternatives:
            where = sorted({row["location"] for row in alternatives})
            note = (
                f"Nothing available in {params.location}. "
                f"There is available {params.category} stock in: {', '.join(where)}."
            )
        else:
            note = (
                f"There is no available {params.category} stock in any location right now. "
                "Say so plainly rather than suggesting an assigned or in-repair asset."
            )
        return ToolOutcome.success(count=0, recommendations=[], note=note)

    return ToolOutcome.success(
        count=len(results),
        recommendations=[
            {
                "asset_code": row["asset_code"],
                "asset_name": row["asset_name"],
                "category": row["category"],
                "location": row["location"],
                "condition": row["condition"],
                "purchase_date": row["purchase_date"],
                "why": row["reason"],
            }
            for row in results
        ],
        note="All items listed are status='Available'. Assigned and in-repair assets are excluded.",
    )
