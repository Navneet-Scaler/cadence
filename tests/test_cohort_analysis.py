"""Tests for cohort retention.

The properties asserted here are the ones that make a retention chart honest:
the denominator includes users who never transacted, cohorts without full
exposure to a horizon are excluded rather than computed from a partial
denominator, and pooling across cohorts is size-weighted.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis import cohort_analysis as ca


def make_users(rows: list[tuple[int, str]]) -> pd.DataFrame:
    """Build a user frame from (user_id, signup_date) tuples."""
    users = pd.DataFrame(rows, columns=["user_id", "signup_date"])
    users["signup_date"] = pd.to_datetime(users["signup_date"])
    users["cohort_week"] = users["signup_date"].dt.to_period("W").dt.start_time
    return users


def make_activity(users: pd.DataFrame, rows: list[tuple[int, str]]) -> pd.DataFrame:
    """Build an activity frame from (user_id, txn_date) tuples."""
    activity = pd.DataFrame(rows, columns=["user_id", "txn_date"])
    activity["txn_date"] = pd.to_datetime(activity["txn_date"])
    activity = activity.merge(users[["user_id", "signup_date"]], on="user_id")
    activity["day_index"] = (activity["txn_date"] - activity["signup_date"]).dt.days
    return activity


@pytest.fixture(autouse=True)
def small_cohorts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixtures are tiny; the production floor would discard all of them."""
    monkeypatch.setattr(ca, "MIN_COHORT_SIZE", 1)


def test_denominator_includes_users_who_never_transacted() -> None:
    """Dropping silent users would inflate retention by excluding the worst churn."""
    users = make_users([(1, "2025-01-01"), (2, "2025-01-01")])
    activity = make_activity(users, [(1, "2025-01-08")])  # user 2 never invests

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=7)

    assert result["cohort_size"].iat[0] == 2
    assert result["active_on_day"].iat[0] == pytest.approx(0.5)


def test_cohorts_without_full_exposure_are_excluded() -> None:
    """A cohort 10 days old has no D30 number; it must not be counted as churned."""
    users = make_users([(1, "2025-01-01"), (2, "2025-05-25")])
    activity = make_activity(users, [(1, "2025-01-31")])
    observation_end = pd.Timestamp("2025-06-01")

    result = ca.retention_at_horizon(activity, users, observation_end, horizon=30)

    # Only the January cohort has 30 full days of exposure.
    assert result["cohort_size"].sum() == 1
    assert 2 not in set(result.index)


def test_no_eligible_cohorts_returns_empty_rather_than_zero() -> None:
    users = make_users([(1, "2025-05-25")])
    activity = make_activity(users, [(1, "2025-05-26")])

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=90)

    assert result.empty


def test_active_on_day_requires_that_exact_day() -> None:
    users = make_users([(1, "2025-01-01"), (2, "2025-01-01")])
    # User 1 invests on day 7 exactly; user 2 on day 6 only.
    activity = make_activity(users, [(1, "2025-01-08"), (2, "2025-01-07")])

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=7)

    assert result["active_on_day"].iat[0] == pytest.approx(0.5)
    # Both fall inside the trailing 7-day window, so the looser measure sees both.
    assert result["active_in_window"].iat[0] == pytest.approx(1.0)


def test_active_in_window_is_a_superset_of_active_on_day() -> None:
    """These two are nested by construction; the forward-looking one is not."""
    users = make_users([(i, "2025-01-01") for i in range(1, 6)])
    activity = make_activity(users, [(1, "2025-01-08"), (2, "2025-01-05"), (3, "2025-01-02")])

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=7)

    assert result["active_in_window"].iat[0] >= result["active_on_day"].iat[0]


def test_ever_active_after_counts_activity_at_or_beyond_the_horizon() -> None:
    users = make_users([(1, "2025-01-01"), (2, "2025-01-01")])
    # User 1 is active well after day 7; user 2 stopped before it.
    activity = make_activity(users, [(1, "2025-03-01"), (2, "2025-01-03")])

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=7)

    assert result["ever_active_after"].iat[0] == pytest.approx(0.5)


def test_pooled_retention_is_weighted_by_cohort_size() -> None:
    """An unweighted mean would let a tiny cohort move the headline as much as a big one."""
    table = pd.DataFrame(
        {
            "cohort_week": pd.to_datetime(["2025-01-06", "2025-01-13"]),
            "cohort_size": [900, 100],
            "horizon": [30, 30],
            "active_on_day": [0.50, 0.10],
            "active_in_window": [0.60, 0.20],
            "ever_active_after": [0.70, 0.30],
        }
    )

    pooled = ca.overall_retention(table)

    # Weighted: 0.5*0.9 + 0.1*0.1 = 0.46, not the unweighted 0.30.
    assert pooled["active_on_day"].iat[0] == pytest.approx(0.46)
    assert pooled["users"].iat[0] == 1000


def test_small_cohorts_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cohort of a handful of users swings percentage points per person."""
    monkeypatch.setattr(ca, "MIN_COHORT_SIZE", 50)
    users = make_users([(1, "2025-01-01"), (2, "2025-01-02")])
    activity = make_activity(users, [(1, "2025-01-08")])

    result = ca.retention_at_horizon(activity, users, pd.Timestamp("2025-06-01"), horizon=7)

    assert result.empty
