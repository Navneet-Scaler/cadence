"""Tests for streak construction.

Every case here is a small, hand-checked fixture — the expected streaks are
worked out by eye, not produced by the code under test. The full simulated
dataset is deliberately not used: a test that only says "the same as last time"
would not catch a wrong definition, only a changed one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis import streak_builder as sb


def txns(rows: list[tuple[int, str, str]]) -> pd.DataFrame:
    """Build a transaction frame from (user_id, 'YYYY-MM-DD', status) tuples."""
    return pd.DataFrame(rows, columns=["user_id", "txn_date", "status"])


def test_single_unbroken_run_is_one_streak() -> None:
    df = txns(
        [(1, "2025-01-01", "success"), (1, "2025-01-02", "success"), (1, "2025-01-03", "success")]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-10")

    assert len(streaks) == 1
    assert streaks.loc[0, "streak_length"] == 3
    assert streaks.loc[0, "streak_start"] == pd.Timestamp("2025-01-01")
    assert streaks.loc[0, "streak_end"] == pd.Timestamp("2025-01-03")
    assert not streaks.loc[0, "is_censored"]
    assert not streaks.loc[0, "recovered"]


def test_a_missed_day_splits_the_run_into_two_streaks() -> None:
    """The core gaps-and-islands case: 1,2,3 then a gap then 6,7."""
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (1, "2025-01-02", "success"),
            (1, "2025-01-03", "success"),
            (1, "2025-01-06", "success"),
            (1, "2025-01-07", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-31")

    assert list(streaks["streak_length"]) == [3, 2]
    assert list(streaks["streak_index"]) == [1, 2]
    # 3 Jan -> 6 Jan is a 3-day gap
    assert streaks.loc[0, "days_to_next_streak"] == 3
    assert streaks.loc[0, "recovered"]
    assert not streaks.loc[1, "recovered"]
    assert pd.isna(streaks.loc[1, "days_to_next_streak"])


def test_duplicate_rows_for_one_day_do_not_shatter_a_streak() -> None:
    """A duplicate consumes a row number; unhandled it would split one streak in two."""
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (1, "2025-01-02", "success"),
            (1, "2025-01-02", "success"),  # duplicate
            (1, "2025-01-03", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-10")

    assert len(streaks) == 1
    assert streaks.loc[0, "streak_length"] == 3


@pytest.mark.parametrize("breaking_status", ["failed", "skipped"])
def test_failed_and_skipped_days_break_the_streak(breaking_status: str) -> None:
    """A failed mandate is not evidence the habit held, so it must not continue a streak."""
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (1, "2025-01-02", breaking_status),
            (1, "2025-01-03", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-10")

    assert list(streaks["streak_length"]) == [1, 1]


def test_streak_alive_on_the_last_observed_day_is_censored() -> None:
    """Treating a still-running streak as a death would bias survival downward."""
    df = txns(
        [(1, "2025-01-08", "success"), (1, "2025-01-09", "success"), (1, "2025-01-10", "success")]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-10")

    assert streaks.loc[0, "is_censored"]


def test_users_are_indexed_independently() -> None:
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (1, "2025-01-03", "success"),
            (2, "2025-01-01", "success"),
            (2, "2025-01-02", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-31")

    user1 = streaks[streaks["user_id"] == 1]
    user2 = streaks[streaks["user_id"] == 2]
    assert list(user1["streak_index"]) == [1, 2]
    assert list(user2["streak_index"]) == [1]
    assert list(user2["streak_length"]) == [2]


def test_month_boundary_is_treated_as_consecutive() -> None:
    """Date arithmetic, not day-of-month arithmetic: 31 Jan -> 1 Feb is unbroken."""
    df = txns([(1, "2025-01-31", "success"), (1, "2025-02-01", "success")])
    streaks = sb.build_streaks(df, observation_end="2025-02-28")

    assert len(streaks) == 1
    assert streaks.loc[0, "streak_length"] == 2


def test_empty_and_all_failed_inputs_return_an_empty_frame() -> None:
    assert sb.build_streaks(txns([])).empty
    assert sb.build_streaks(txns([(1, "2025-01-01", "failed")])).empty


def test_expand_to_observations_produces_one_row_per_streak_day() -> None:
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (1, "2025-01-02", "success"),
            (1, "2025-01-03", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-10")
    observations = sb.expand_to_observations(streaks)

    assert len(observations) == 3
    assert list(observations["day_of_streak"]) == [1, 2, 3]
    # Uncensored streak: the last day is the day it broke.
    assert list(observations["survived_to_next_day"]) == [True, True, False]


def test_censored_streak_has_no_break_day() -> None:
    df = txns([(1, "2025-01-09", "success"), (1, "2025-01-10", "success")])
    streaks = sb.build_streaks(df, observation_end="2025-01-10")
    observations = sb.expand_to_observations(streaks)

    assert observations["survived_to_next_day"].all()


def test_discrete_hazard_matches_a_hand_computed_example() -> None:
    """Three streaks of length 1, 2, 3, none censored.

    Day 1: 3 at risk, 1 breaks -> hazard 1/3, survival 2/3
    Day 2: 2 at risk, 1 breaks -> hazard 1/2, survival 1/3
    Day 3: 1 at risk, 1 breaks -> hazard 1,   survival 0
    """
    df = txns(
        [
            (1, "2025-01-01", "success"),
            (2, "2025-01-01", "success"),
            (2, "2025-01-02", "success"),
            (3, "2025-01-01", "success"),
            (3, "2025-01-02", "success"),
            (3, "2025-01-03", "success"),
        ]
    )
    streaks = sb.build_streaks(df, observation_end="2025-01-31")
    hazard = sb.discrete_hazard(sb.expand_to_observations(streaks))

    assert list(hazard["at_risk"]) == [3, 2, 1]
    assert list(hazard["broke"]) == [1, 1, 1]
    assert hazard["hazard_rate"].tolist() == pytest.approx([1 / 3, 1 / 2, 1.0])
    assert hazard["survival"].tolist() == pytest.approx([2 / 3, 1 / 3, 0.0])
