"""Employee lookup, including the manager hierarchy."""

from __future__ import annotations

from assistant.db import repo
from assistant.tools.registry import ToolContext, ToolOutcome
from assistant.tools.schemas import LookupEmployeeParams


def lookup_employee(params: LookupEmployeeParams, ctx: ToolContext) -> ToolOutcome:
    if not params.name and not params.employee_id:
        return ToolOutcome.failure(
            "Provide either a name or an employee_id.", code="invalid_arguments"
        )

    profile = repo.get_employee(
        name=params.name, employee_id=params.employee_id, db_path=ctx.db_path
    )

    return ToolOutcome.success(
        employee={
            "employee_id": profile["employee_id"],
            "name": profile["full_name"],
            "email": profile["email"],
            "department": profile["department"],
            "base_location": profile["location"],
            "manager": profile["manager_name"],
            "manager_email": profile["manager_email"],
            "direct_reports": profile["direct_reports"],
            "assets_held": profile["assets_held"],
        }
    )
