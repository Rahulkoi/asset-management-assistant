"""Data layer tests. No network, no LLM — these must stay fast and deterministic."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.db import repo, seed

# --------------------------------------------------------------------------
# Provenance: the spreadsheet is the source of truth and must not drift.
# --------------------------------------------------------------------------


def test_source_rows_match_spreadsheet(seeded_db: Path, source_xlsx: Path) -> None:
    assert seed.verify(seeded_db, source_xlsx) == []


def test_all_21_source_rows_present(seeded_db: Path) -> None:
    rows = repo.search_assets(limit=50, db_path=seeded_db)
    from_xlsx = [r for r in rows if r["source"] == "xlsx"]
    assert len(from_xlsx) == 21


def test_synthetic_rows_are_flagged(seeded_db: Path) -> None:
    """A reviewer must be able to tell generated data from supplied data."""
    rows = repo.search_assets(limit=50, db_path=seeded_db)
    assert {r["source"] for r in rows} == {"xlsx", "synthetic"}
    assert all(r["asset_code"] >= "AST1021" for r in rows if r["source"] == "synthetic")


# --------------------------------------------------------------------------
# Requirement 1 & 2: lookup by code, and the fields NL questions ask for.
# --------------------------------------------------------------------------


def test_lookup_asset_returns_holder_and_location(seeded_db: Path) -> None:
    asset = repo.get_asset("AST1002", db_path=seeded_db)
    assert asset["asset_name"] == "Lenovo ThinkPad X1"
    assert asset["assigned_to"] == "Amit Kumar"
    assert asset["location"] == "Bangalore"


def test_lookup_asset_is_case_insensitive(seeded_db: Path) -> None:
    assert repo.get_asset("ast1002", db_path=seeded_db)["asset_code"] == "AST1002"


def test_lookup_unknown_asset_raises_not_found(seeded_db: Path) -> None:
    with pytest.raises(repo.NotFoundError):
        repo.get_asset("AST9999", db_path=seeded_db)


# --------------------------------------------------------------------------
# Requirement 3: multi-step — holder's manager, resolvable in one hop.
# --------------------------------------------------------------------------


def test_asset_lookup_includes_holders_manager(seeded_db: Path) -> None:
    asset = repo.get_asset("AST1002", db_path=seeded_db)
    assert asset["assigned_to"] == "Amit Kumar"
    assert asset["manager_name"] == "Priya Singh"


def test_employee_profile_has_manager_and_reports(seeded_db: Path) -> None:
    profile = repo.get_employee(name="Sakshi Jain", db_path=seeded_db)
    assert profile["manager_name"] == "Vikram Shah"
    assert "Neha Joshi" in profile["direct_reports"]
    assert len(profile["assets_held"]) == 3


def test_head_of_it_has_no_manager(seeded_db: Path) -> None:
    profile = repo.get_employee(name="Vikram Shah", db_path=seeded_db)
    assert profile["manager_name"] is None


def test_ambiguous_employee_name_raises_rather_than_guessing(seeded_db: Path) -> None:
    """Two employees share a surname; guessing one would be a silent wrong answer."""
    with pytest.raises(repo.ConflictError, match="more than one"):
        repo.get_employee(name="a", db_path=seeded_db)


# --------------------------------------------------------------------------
# Requirement 5: recommendation must only offer genuinely available stock.
# --------------------------------------------------------------------------


def test_recommend_available_laptop_in_bangalore(seeded_db: Path) -> None:
    results = repo.recommend_assets(category="Laptop", location="Bangalore", limit=5, db_path=seeded_db)
    assert results, "expected available laptops in Bangalore"
    top_three = results[:3]
    assert all(r["location"] == "Bangalore" for r in top_three)
    assert all(r["status"] == "Available" for r in results)


def test_recommend_never_offers_in_repair_or_assigned(seeded_db: Path) -> None:
    """AST1033 is an in-repair laptop in Bangalore — it must never be recommended."""
    results = repo.recommend_assets(category="Laptop", location="Bangalore", limit=20, db_path=seeded_db)
    codes = {r["asset_code"] for r in results}
    assert "AST1033" not in codes
    assert "AST1002" not in codes  # assigned to Amit Kumar


def test_recommend_prefers_exact_city_then_region(seeded_db: Path) -> None:
    results = repo.recommend_assets(category="Laptop", location="Bangalore", limit=10, db_path=seeded_db)
    ranks = [r["match_rank"] for r in results]
    assert ranks == sorted(ranks), "results must be ordered closest-location-first"


def test_recommend_returns_empty_rather_than_wrong_category(seeded_db: Path) -> None:
    assert repo.recommend_assets(category="Projector", location="Pune", db_path=seeded_db) == []


# --------------------------------------------------------------------------
# Search filters and guardrail-relevant limits.
# --------------------------------------------------------------------------


def test_search_by_employee_name(seeded_db: Path) -> None:
    rows = repo.search_assets(employee_name="Rahul Sharma", db_path=seeded_db)
    assert {r["asset_code"] for r in rows} == {"AST1000", "AST1001", "AST1007", "AST1013"}


def test_search_by_category_and_location(seeded_db: Path) -> None:
    rows = repo.search_assets(category="Laptop", location="Bangalore", db_path=seeded_db)
    assert rows
    assert all(r["category"] == "Laptop" and r["location"] == "Bangalore" for r in rows)


def test_search_limit_is_capped(seeded_db: Path) -> None:
    """A model asking for 10_000 rows must not be able to dump the table."""
    rows = repo.search_assets(limit=10_000, db_path=seeded_db)
    assert len(rows) <= 50


def test_distinct_values_rejects_unlisted_column(seeded_db: Path) -> None:
    """The column name reaches SQL, so it is allowlisted rather than escaped."""
    with pytest.raises(repo.RepoError):
        repo.distinct_values("asset_code; DROP TABLE assets", db_path=seeded_db)


def test_search_filters_do_not_interpolate_user_input(seeded_db: Path) -> None:
    """Classic injection string must be treated as a literal value, not SQL."""
    rows = repo.search_assets(name_contains="'; DROP TABLE assets; --", db_path=seeded_db)
    assert rows == []
    assert repo.count_assets(db_path=seeded_db) == 35  # table still intact


# --------------------------------------------------------------------------
# Writes: invariants, audit trail, concurrency, idempotency.
# --------------------------------------------------------------------------


def test_transfer_reassigns_and_audits(scratch_db: Path) -> None:
    before = repo.get_asset("AST1002", db_path=scratch_db)
    assert before["assigned_to"] == "Amit Kumar"

    after = repo.transfer_asset(
        asset_code="AST1002",
        to_employee="Priya Singh",
        reason="team change",
        actor="tester",
        db_path=scratch_db,
    )
    assert after["assigned_to"] == "Priya Singh"
    assert after["status"] == "Assigned"

    log = repo.get_audit_log("AST1002", db_path=scratch_db)
    assert log[0]["action"] == "transfer"
    assert log[0]["from_employee"] == "Amit Kumar"
    assert log[0]["to_employee"] == "Priya Singh"
    assert log[0]["actor"] == "tester"


def test_transfer_rejects_stale_write(scratch_db: Path) -> None:
    """Optimistic concurrency: the row moved after the preview was shown."""
    with pytest.raises(repo.ConflictError, match="changed since"):
        repo.transfer_asset(
            asset_code="AST1002",
            to_employee="Priya Singh",
            expected_updated_at="2000-01-01T00:00:00",
            db_path=scratch_db,
        )


def test_transfer_to_current_holder_is_rejected(scratch_db: Path) -> None:
    with pytest.raises(repo.ConflictError, match="already assigned"):
        repo.transfer_asset(asset_code="AST1002", to_employee="Amit Kumar", db_path=scratch_db)


def test_transfer_to_unknown_employee_is_rejected(scratch_db: Path) -> None:
    with pytest.raises(repo.NotFoundError):
        repo.transfer_asset(asset_code="AST1002", to_employee="Nobody McGhost", db_path=scratch_db)


def test_transfer_is_idempotent_under_replay(scratch_db: Path) -> None:
    """A retried request must not move the asset twice."""
    repo.transfer_asset(
        asset_code="AST1002", to_employee="Priya Singh",
        idempotency_key="key-123", db_path=scratch_db,
    )
    with pytest.raises(repo.ConflictError, match="already applied"):
        repo.transfer_asset(
            asset_code="AST1002", to_employee="Arjun Rao",
            idempotency_key="key-123", db_path=scratch_db,
        )
    assert repo.get_asset("AST1002", db_path=scratch_db)["assigned_to"] == "Priya Singh"


def test_create_asset_assigns_server_side_code(scratch_db: Path) -> None:
    """The model never picks the code, so it cannot collide with an existing one."""
    created = repo.create_asset(
        asset_name="Dell Latitude 7450", category="Laptop", location="Pune",
        actor="tester", db_path=scratch_db,
    )
    assert created["asset_code"] == "AST1035"
    assert created["status"] == "Available"
    assert created["assigned_to"] is None


def test_create_asset_with_holder_is_assigned(scratch_db: Path) -> None:
    created = repo.create_asset(
        asset_name="iPhone 16", category="Mobile", location="Mumbai",
        assign_to_employee="Deepak Sinha", actor="tester", db_path=scratch_db,
    )
    assert created["status"] == "Assigned"
    assert created["assigned_to"] == "Deepak Sinha"
    assert repo.get_audit_log(created["asset_code"], db_path=scratch_db)[0]["action"] == "create"


def test_created_codes_do_not_collide(scratch_db: Path) -> None:
    first = repo.create_asset(
        asset_name="A", category="Laptop", location="Pune", db_path=scratch_db
    )
    second = repo.create_asset(
        asset_name="B", category="Laptop", location="Pune", db_path=scratch_db
    )
    assert first["asset_code"] != second["asset_code"]
