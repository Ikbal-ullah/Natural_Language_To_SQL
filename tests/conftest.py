"""Shared fixtures."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

# The default 3 s statement timeout is sized for the 50k-row e-commerce DB. Reference queries
# in the legacy 5M-row learners DB legitimately need longer (jn09 aggregates 5M rows).
os.environ.setdefault("NL2SQL_STATEMENT_TIMEOUT_MS", "10000")

from nl2sql.schema import Catalog, get_catalog  # noqa: E402


def _database_available() -> bool:
    try:
        get_catalog()
    except Exception:
        return False
    return True


HAVE_DB = _database_available()
needs_db = pytest.mark.skipif(not HAVE_DB, reason="no seeded database; run `make db`")


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return get_catalog()
