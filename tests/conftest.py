"""Shared fixtures.

Every test runs against a throwaway database seeded from the real spreadsheet,
so the suite exercises the actual data without touching data/assets.db.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import get_settings
from assistant.db import seed


@pytest.fixture(scope="session")
def source_xlsx() -> Path:
    return get_settings().source_xlsx


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory: pytest.TempPathFactory, source_xlsx: Path) -> Path:
    """A fully seeded database, built once per test session."""
    db_path = tmp_path_factory.mktemp("db") / "test_assets.db"
    seed.build(db_path, source_xlsx)
    return db_path


@pytest.fixture()
def scratch_db(tmp_path: Path, source_xlsx: Path) -> Path:
    """A fresh database per test, for tests that write."""
    db_path = tmp_path / "scratch.db"
    seed.build(db_path, source_xlsx)
    return db_path
