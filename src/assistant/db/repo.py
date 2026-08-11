"""Data access layer.

This is the only module in the project that imports sqlite3. Every statement is
parameterised and every column returned is explicitly named — there is no
`SELECT *` and no string-built SQL anywhere, so no tool (and therefore no model
output) can widen a query or reach a column it was not meant to see.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from assistant.config import get_settings

# Cities grouped so recommendation can fall back to "nearby" when the requested
# city has no stock, instead of silently returning something in another region.
REGIONS: dict[str, str] = {
    "Bangalore": "South",
    "Chennai": "South",
    "Hyderabad": "South",
    "Mumbai": "West",
    "Pune": "West",
    "Delhi": "North",
    "Kolkata": "East",
}

ASSET_COLUMNS = (
    "a.asset_code, a.asset_name, a.category, a.location, a.purchase_date, "
    "a.status, a.condition, a.source, a.updated_at, "
    "e.full_name AS assigned_to, e.employee_id AS assigned_to_id"
)


class RepoError(Exception):
    """Base class for expected, user-explainable data errors."""


class NotFoundError(RepoError):
    pass


class ConflictError(RepoError):
    """The row changed since it was previewed, or the write violates an invariant."""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or get_settings().db_path
    if not path.exists():
        raise RepoError(
            f"Database not found at {path}. Run `make seed` (python -m assistant.db.seed) first."
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_asset(asset_code: str, db_path: Path | None = None) -> dict[str, Any]:
    """One asset plus its holder and the holder's manager.

    Returns the manager in the same call so the multi-step question
    ("who is using AST1002, and who is that employee's manager?") can be
    answered in a single hop — smaller free-tier models chain unreliably.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {ASSET_COLUMNS}, e.department AS assigned_to_department,"
            "        e.email AS assigned_to_email, e.location AS assigned_to_location,"
            "        m.full_name AS manager_name, m.email AS manager_email,"
            "        m.department AS manager_department"
            "   FROM assets a"
            "   LEFT JOIN employees e ON e.employee_id = a.employee_id"
            "   LEFT JOIN employees m ON m.employee_id = e.manager_id"
            "  WHERE a.asset_code = ? COLLATE NOCASE",
            (asset_code,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"No asset with code {asset_code!r} exists.")
    return dict(row)


def search_assets(
    *,
    category: str | None = None,
    location: str | None = None,
    status: str | None = None,
    employee_name: str | None = None,
    name_contains: str | None = None,
    purchased_after: str | None = None,
    purchased_before: str | None = None,
    limit: int = 20,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Filtered search. Every filter is optional; unset filters are not applied."""
    settings = get_settings()
    limit = max(1, min(limit, settings.max_rows_per_query))

    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("a.category = ? COLLATE NOCASE")
        params.append(category)
    if location:
        clauses.append("a.location = ? COLLATE NOCASE")
        params.append(location)
    if status:
        clauses.append("a.status = ? COLLATE NOCASE")
        params.append(status)
    if employee_name:
        clauses.append("e.full_name LIKE ? COLLATE NOCASE")
        params.append(f"%{employee_name}%")
    if name_contains:
        clauses.append("a.asset_name LIKE ? COLLATE NOCASE")
        params.append(f"%{name_contains}%")
    if purchased_after:
        clauses.append("a.purchase_date >= ?")
        params.append(purchased_after)
    if purchased_before:
        clauses.append("a.purchase_date <= ?")
        params.append(purchased_before)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {ASSET_COLUMNS} FROM assets a"
            "  LEFT JOIN employees e ON e.employee_id = a.employee_id"
            f"{where}"
            " ORDER BY a.asset_code LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def count_assets(
    *, category: str | None = None, location: str | None = None,
    status: str | None = None, db_path: Path | None = None,
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("category", category), ("location", location), ("status", status)):
        if value:
            clauses.append(f"{column} = ? COLLATE NOCASE")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM assets{where}", params).fetchone()[0]


def get_employee(
    *, name: str | None = None, employee_id: str | None = None, db_path: Path | None = None
) -> dict[str, Any]:
    """Employee profile: department, manager, direct reports, assets held."""
    if not name and not employee_id:
        raise RepoError("Provide either an employee name or an employee_id.")

    with connect(db_path) as conn:
        if employee_id:
            row = conn.execute(
                "SELECT e.employee_id, e.full_name, e.email, e.department, e.location,"
                "       m.employee_id AS manager_id, m.full_name AS manager_name,"
                "       m.email AS manager_email, m.department AS manager_department"
                "  FROM employees e LEFT JOIN employees m ON m.employee_id = e.manager_id"
                " WHERE e.employee_id = ? COLLATE NOCASE",
                (employee_id,),
            ).fetchone()
        else:
            candidates = conn.execute(
                "SELECT e.employee_id, e.full_name, e.email, e.department, e.location,"
                "       m.employee_id AS manager_id, m.full_name AS manager_name,"
                "       m.email AS manager_email, m.department AS manager_department"
                "  FROM employees e LEFT JOIN employees m ON m.employee_id = e.manager_id"
                " WHERE e.full_name LIKE ? COLLATE NOCASE"
                " ORDER BY LENGTH(e.full_name)",
                (f"%{name}%",),
            ).fetchall()
            if len(candidates) > 1:
                exact = [c for c in candidates if c["full_name"].lower() == (name or "").lower()]
                if len(exact) == 1:
                    candidates = exact
                else:
                    names = ", ".join(c["full_name"] for c in candidates)
                    raise ConflictError(
                        f"{name!r} matches more than one employee: {names}. Ask which one."
                    )
            row = candidates[0] if candidates else None

        if row is None:
            raise NotFoundError(f"No employee found matching {name or employee_id!r}.")

        profile = dict(row)
        profile["direct_reports"] = [
            r["full_name"]
            for r in conn.execute(
                "SELECT full_name FROM employees WHERE manager_id = ? ORDER BY full_name",
                (profile["employee_id"],),
            ).fetchall()
        ]
        profile["assets_held"] = [
            dict(r)
            for r in conn.execute(
                "SELECT asset_code, asset_name, category, location, status"
                "  FROM assets WHERE employee_id = ? ORDER BY asset_code",
                (profile["employee_id"],),
            ).fetchall()
        ]
    return profile


def recommend_assets(
    *, category: str, location: str | None = None, limit: int = 5, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Rank genuinely available stock: exact city first, then same region.

    Only status='Available' is ever returned — an in-repair or assigned unit is
    not a recommendation, and excluding them is asserted in the eval suite.
    """
    settings = get_settings()
    limit = max(1, min(limit, settings.max_rows_per_query))
    region = REGIONS.get(location.title()) if location else None

    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {ASSET_COLUMNS} FROM assets a"
            "  LEFT JOIN employees e ON e.employee_id = a.employee_id"
            " WHERE a.status = 'Available' AND a.category = ? COLLATE NOCASE"
            " ORDER BY a.purchase_date DESC",
            (category,),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item_region = REGIONS.get(item["location"])
        if location and item["location"].lower() == location.lower():
            rank, reason = 0, f"Available in {item['location']}"
        elif region and item_region == region:
            rank, reason = 1, f"Available in {item['location']} (same region: {region})"
        elif location:
            rank, reason = 2, f"Available in {item['location']} (different region)"
        else:
            rank, reason = 0, f"Available in {item['location']}"
        item["match_rank"] = rank
        item["reason"] = f"{reason}; condition {item['condition']}, purchased {item['purchase_date']}"
        results.append(item)

    # Closest location wins; within a tier, newest stock first.
    results.sort(key=lambda r: (r["match_rank"], -_date_key(r["purchase_date"])))
    return results[:limit]


def _date_key(iso_date: str) -> int:
    return int(iso_date.replace("-", ""))


def distinct_values(column: str, db_path: Path | None = None) -> list[str]:
    """Enum values for tool schemas, read from the data itself.

    Constraining the model to real categories/locations removes a whole class of
    hallucinated filters. `column` is checked against an allowlist because it is
    interpolated into SQL.
    """
    allowed = {"category", "location", "status", "condition"}
    if column not in allowed:
        raise RepoError(f"distinct_values only supports {sorted(allowed)}, got {column!r}")
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM assets ORDER BY {column}"  # noqa: S608 - allowlisted
        ).fetchall()
    return [row[0] for row in rows]


def employee_names(db_path: Path | None = None) -> list[str]:
    with connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT full_name FROM employees ORDER BY full_name")]


def next_asset_code(db_path: Path | None = None) -> str:
    """Server-assigned codes. The model never picks one, so it cannot collide."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT asset_code FROM assets WHERE asset_code LIKE 'AST%'"
            " ORDER BY CAST(SUBSTR(asset_code, 4) AS INTEGER) DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return "AST1000"
    return f"AST{int(row[0][3:]) + 1}"


def get_audit_log(
    asset_code: str | None = None, limit: int = 20, db_path: Path | None = None
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, get_settings().max_rows_per_query))
    with connect(db_path) as conn:
        if asset_code:
            rows = conn.execute(
                "SELECT t.id, t.asset_code, t.action, t.reason, t.actor, t.transferred_at,"
                "       f.full_name AS from_employee, g.full_name AS to_employee"
                "  FROM asset_transfers t"
                "  LEFT JOIN employees f ON f.employee_id = t.from_employee_id"
                "  LEFT JOIN employees g ON g.employee_id = t.to_employee_id"
                " WHERE t.asset_code = ? COLLATE NOCASE"
                " ORDER BY t.id DESC LIMIT ?",
                (asset_code, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.id, t.asset_code, t.action, t.reason, t.actor, t.transferred_at,"
                "       f.full_name AS from_employee, g.full_name AS to_employee"
                "  FROM asset_transfers t"
                "  LEFT JOIN employees f ON f.employee_id = t.from_employee_id"
                "  LEFT JOIN employees g ON g.employee_id = t.to_employee_id"
                " ORDER BY t.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Writes — each runs in a single transaction and appends to the audit log.
# --------------------------------------------------------------------------


def create_asset(
    *,
    asset_name: str,
    category: str,
    location: str,
    assign_to_employee: str | None = None,
    purchase_date: str | None = None,
    condition: str = "New",
    actor: str = "unknown",
    session_id: str | None = None,
    idempotency_key: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    purchase_date = purchase_date or datetime.now().date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    with connect(db_path) as conn:
        if idempotency_key and _already_applied(conn, idempotency_key):
            raise ConflictError("This write was already applied (duplicate idempotency key).")

        employee_id = None
        if assign_to_employee:
            employee_id = _resolve_employee_id(conn, assign_to_employee)
        status = "Assigned" if employee_id else "Available"
        code = next_asset_code(db_path)

        try:
            with conn:  # transaction
                conn.execute(
                    "INSERT INTO assets (asset_code, asset_name, category, employee_id, location,"
                    " purchase_date, status, condition, source, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'synthetic', ?)",
                    (
                        code, asset_name, category, employee_id, location,
                        purchase_date, status, condition, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO asset_transfers (asset_code, action, from_employee_id,"
                    " to_employee_id, reason, actor, session_id, idempotency_key, transferred_at)"
                    " VALUES (?, 'create', NULL, ?, ?, ?, ?, ?, ?)",
                    (code, employee_id, "asset created", actor, session_id, idempotency_key, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Could not create asset: {exc}") from exc

    return get_asset(code, db_path)


def transfer_asset(
    *,
    asset_code: str,
    to_employee: str,
    reason: str | None = None,
    expected_updated_at: str | None = None,
    actor: str = "unknown",
    session_id: str | None = None,
    idempotency_key: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Reassign an asset. Optimistic concurrency via `expected_updated_at`.

    If the row changed between the confirmation preview and the commit, the
    write is rejected rather than silently overwriting someone else's change.
    """
    now = datetime.now().isoformat(timespec="seconds")

    with connect(db_path) as conn:
        if idempotency_key and _already_applied(conn, idempotency_key):
            raise ConflictError("This transfer was already applied (duplicate idempotency key).")

        current = conn.execute(
            "SELECT asset_code, employee_id, status, updated_at FROM assets"
            " WHERE asset_code = ? COLLATE NOCASE",
            (asset_code,),
        ).fetchone()
        if current is None:
            raise NotFoundError(f"No asset with code {asset_code!r} exists.")

        if expected_updated_at and current["updated_at"] != expected_updated_at:
            raise ConflictError(
                f"{asset_code} changed since it was previewed. Re-check the asset and confirm again."
            )
        if current["status"] == "Retired":
            raise ConflictError(f"{asset_code} is retired and cannot be transferred.")

        target_id = _resolve_employee_id(conn, to_employee)
        if current["employee_id"] == target_id:
            raise ConflictError(
                f"{asset_code} is already assigned to {to_employee}. Nothing to do."
            )

        canonical_code = current["asset_code"]
        try:
            with conn:  # transaction
                conn.execute(
                    "UPDATE assets SET employee_id = ?, status = 'Assigned', updated_at = ?"
                    " WHERE asset_code = ?",
                    (target_id, now, canonical_code),
                )
                conn.execute(
                    "INSERT INTO asset_transfers (asset_code, action, from_employee_id,"
                    " to_employee_id, reason, actor, session_id, idempotency_key, transferred_at)"
                    " VALUES (?, 'transfer', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        canonical_code, current["employee_id"], target_id, reason,
                        actor, session_id, idempotency_key, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Could not transfer asset: {exc}") from exc

    return get_asset(canonical_code, db_path)


def _already_applied(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM asset_transfers WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return row is not None


def _resolve_employee_id(conn: sqlite3.Connection, name_or_id: str) -> str:
    row = conn.execute(
        "SELECT employee_id FROM employees WHERE employee_id = ? COLLATE NOCASE",
        (name_or_id,),
    ).fetchone()
    if row:
        return row[0]

    candidates = conn.execute(
        "SELECT employee_id, full_name FROM employees WHERE full_name LIKE ? COLLATE NOCASE",
        (f"%{name_or_id}%",),
    ).fetchall()
    if not candidates:
        raise NotFoundError(f"No employee found matching {name_or_id!r}.")
    if len(candidates) > 1:
        exact = [c for c in candidates if c["full_name"].lower() == name_or_id.lower()]
        if len(exact) != 1:
            names = ", ".join(c["full_name"] for c in candidates)
            raise ConflictError(f"{name_or_id!r} matches more than one employee: {names}.")
        candidates = exact
    return candidates[0][0]
