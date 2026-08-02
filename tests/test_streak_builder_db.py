"""Integration test: SQL and pandas streak construction agree on live data.

Separated from ``test_streak_builder.py`` because this one needs a running
Postgres with the schema and simulated data loaded — the other 100+ tests do
not. Skipped automatically if the database is unreachable, so `pytest` still
passes with no environment; run explicitly (or in the `db-tests` CI job, which
brings up Postgres, applies the schema, and seeds data first) to actually
exercise it.
"""

from __future__ import annotations

import pytest

from src import db
from src.analysis import streak_builder


def _db_available() -> bool:
    try:
        db.read_sql("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="requires a live Postgres instance with the schema applied"
)


def test_sql_and_pandas_streak_construction_agree_on_live_data() -> None:
    """Guards against the two implementations drifting apart in production.

    This is the check the README and MEMO cite as validating 50,576 streaks —
    it must actually run in CI, not just be runnable by hand, or that claim is
    unverified the moment either implementation changes.
    """
    streak_builder.rebuild_streaks_in_db()
    assert streak_builder.validate_against_sql()
