"""Tool parameter models and provider-safe JSON schema generation.

Pydantic is the single source of truth for tool arguments. It gives us real
validation before dispatch — which matters because Gemini honours only a subset
of OpenAPI schema and ignores `additionalProperties: false`, so we cannot rely
on the provider to reject a malformed call.

`to_provider_schema()` walks Pydantic's JSON schema and emits the conservative
subset every provider accepts: inlined $defs, no anyOf, no titles/defaults.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_LIMIT = 50


class LookupAssetParams(BaseModel):
    asset_code: str = Field(description="Asset code, for example AST1002.")

    @field_validator("asset_code")
    @classmethod
    def strip_code(cls, value: str) -> str:
        return value.strip().upper()


class SearchAssetsParams(BaseModel):
    category: str | None = Field(default=None, description="Asset category, e.g. Laptop.")
    location: str | None = Field(default=None, description="Office location, e.g. Bangalore.")
    status: str | None = Field(
        default=None, description="Assigned, Available, In Repair or Retired."
    )
    employee_name: str | None = Field(
        default=None, description="Full or partial name of the person holding the asset."
    )
    name_contains: str | None = Field(
        default=None, description="Substring of the asset name, e.g. ThinkPad."
    )
    purchased_after: str | None = Field(default=None, description="ISO date, e.g. 2024-01-01.")
    purchased_before: str | None = Field(default=None, description="ISO date, e.g. 2025-12-31.")
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="Max rows to return.")

    @field_validator("purchased_after", "purchased_before")
    @classmethod
    def check_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        date.fromisoformat(value)  # raises on malformed input
        return value


class LookupEmployeeParams(BaseModel):
    name: str | None = Field(default=None, description="Employee's full or partial name.")
    employee_id: str | None = Field(default=None, description="Employee id, e.g. EMP004.")


class RecommendAssetsParams(BaseModel):
    category: str = Field(description="Category of asset needed, e.g. Laptop.")
    location: str | None = Field(default=None, description="Preferred office location.")
    limit: int = Field(default=5, ge=1, le=MAX_LIMIT)


class SearchPolicyParams(BaseModel):
    query: str = Field(description="The policy question, in natural language.")
    k: int = Field(default=4, ge=1, le=8, description="Number of policy passages to retrieve.")


class AddAssetParams(BaseModel):
    """The asset code is deliberately absent — the server assigns it."""

    asset_name: str = Field(description="Model name, e.g. 'Dell Latitude 7450'.")
    category: str = Field(description="Asset category, e.g. Laptop.")
    location: str = Field(description="Office location where the asset sits.")
    assign_to_employee: str | None = Field(
        default=None, description="Who receives it. Omit to add it as unassigned stock."
    )
    purchase_date: str | None = Field(default=None, description="ISO date. Defaults to today.")
    condition: str = Field(default="New", description="New, Good, Fair or Poor.")
    confirm_token: str | None = Field(
        default=None,
        description=(
            "Leave empty on the first call to preview the change. After the user "
            "explicitly approves, call again with the token from the preview."
        ),
    )

    @field_validator("purchase_date")
    @classmethod
    def check_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        date.fromisoformat(value)
        return value


class TransferAssetParams(BaseModel):
    asset_code: str = Field(description="Asset code to transfer, e.g. AST1002.")
    to_employee: str = Field(description="Name or employee id of the new holder.")
    reason: str | None = Field(default=None, description="Why the asset is moving.")
    confirm_token: str | None = Field(
        default=None,
        description=(
            "Leave empty on the first call to preview the change. After the user "
            "explicitly approves, call again with the token from the preview."
        ),
    )

    @field_validator("asset_code")
    @classmethod
    def strip_code(cls, value: str) -> str:
        return value.strip().upper()


# --------------------------------------------------------------------------
# Provider-safe schema emission
# --------------------------------------------------------------------------

_ALLOWED_KEYS = {"type", "description", "properties", "required", "items", "enum", "nullable"}


def to_provider_schema(
    model: type[BaseModel], enums: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Pydantic model -> the OpenAPI subset providers reliably accept.

    `enums` injects live values read from the database (categories, locations,
    statuses) so the model is steered towards filters that actually exist
    instead of inventing 'Gurgaon' or 'Tablet'.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    cleaned = _clean(raw, defs)

    if enums:
        for field_name, values in enums.items():
            prop = cleaned.get("properties", {}).get(field_name)
            if prop is not None and values:
                prop["enum"] = list(values)
    return cleaned


def _clean(node: Any, defs: dict[str, Any]) -> Any:
    if not isinstance(node, dict):
        return node

    node = copy.deepcopy(node)

    # Inline $ref — providers reject or mishandle references.
    if "$ref" in node:
        ref_name = node.pop("$ref").rsplit("/", 1)[-1]
        node = {**_clean(defs.get(ref_name, {}), defs), **node}

    # Optional[T] renders as anyOf[T, null]. Collapse to T + nullable.
    for union_key in ("anyOf", "oneOf"):
        if union_key in node:
            options = node.pop(union_key)
            non_null = [o for o in options if o.get("type") != "null"]
            was_nullable = len(non_null) < len(options)
            if non_null:
                merged = _clean(non_null[0], defs)
                node = {**merged, **node}
                if was_nullable:
                    node["nullable"] = True

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _ALLOWED_KEYS:
            continue  # drops title, default, allOf, additionalProperties, format, ge/le...
        if key == "properties":
            result[key] = {name: _clean(sub, defs) for name, sub in value.items()}
        elif key == "items":
            result[key] = _clean(value, defs)
        else:
            result[key] = value

    result.setdefault("type", "object" if "properties" in result else "string")
    return result
