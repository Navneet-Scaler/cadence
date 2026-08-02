"""Tests for the survival analysis.

Same discipline as the streak-builder tests: small hand-checked fixtures whose
expected answers are worked out by hand, not produced by the code under test. No
database — every function under test takes a frame.

The tests that matter most here are the ones guarding *interpretation* rather
than arithmetic. A survival module fails dangerously, not loudly: it returns a
plausible number computed the wrong way. So there are explicit tests that
censoring is honoured, that clustered standard errors are actually wider than
naive ones, and that the word "never" never reappears in a column name.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from lifelines import KaplanMeierFitter

from src.analysis import survival_analysis as sa
from src.analysis import viz


def streaks(rows: list[tuple]) -> pd.DataFrame:
    """Build a streak frame from tuples.

    Columns: user_id, streak_index, streak_length, is_censored,
    days_to_next_streak, streak_end.
    """
    return pd.DataFrame(
        rows,
        columns=[
            "user_id",
            "streak_index",
            "streak_length",
            "is_censored",
            "days_to_next_streak",
            "streak_end",
        ],
    )


# --------------------------------------------------------------------------- #
# Censoring
# --------------------------------------------------------------------------- #


def test_censored_streaks_are_not_counted_as_deaths() -> None:
    """The whole point of using Kaplan-Meier instead of a histogram.

    Durations 2, 2, 5 with the middle one censored. At t=2 three streaks are at
    risk and exactly one dies, so S(2) = 2/3 — not 1/3, which is what counting
    the censored row as a death would give.
    """
    df = streaks(
        [
            (1, 1, 2, False, 3.0, "2025-03-01"),
            (2, 1, 2, True, None, "2025-03-01"),
            (3, 1, 5, False, 4.0, "2025-03-04"),
        ]
    )
    kmf = sa.fit_streak_survival(df)

    assert kmf.predict(2) == pytest.approx(2 / 3)
    # Nothing happens between the two event times, so the curve is flat there.
    assert kmf.predict(4) == pytest.approx(2 / 3)


def test_ignoring_censoring_would_understate_survival() -> None:
    """Guards the direction of the bias, so a regression cannot pass silently."""
    df = streaks(
        [
            (1, 1, 3, False, 2.0, "2025-03-01"),
            (2, 1, 3, True, None, "2025-03-01"),
            (3, 1, 3, True, None, "2025-03-01"),
            (4, 1, 9, False, 1.0, "2025-03-07"),
        ]
    )
    correct = sa.fit_streak_survival(df).predict(3)

    naive = KaplanMeierFitter().fit(df["streak_length"], event_observed=np.ones(len(df)))
    assert correct > naive.predict(3)


# --------------------------------------------------------------------------- #
# Gap frame — "hasn't returned yet" is not "never returned"
# --------------------------------------------------------------------------- #


def test_gap_frame_censors_users_who_have_not_returned_yet() -> None:
    """A user who lapsed near the window's end is censored, not counted as churn.

    User 2 broke on 2025-03-28 with the window closing 2025-03-31: three days of
    exposure and no return. That is not evidence of churn, and recording it as
    ``returned = 0`` with a duration of 3 is what keeps it out of the churn count.
    """
    df = streaks(
        [
            (1, 1, 5, False, 4.0, "2025-03-10"),
            (2, 1, 5, False, None, "2025-03-28"),
        ]
    )
    gaps = sa.build_gap_frame(df, pd.Timestamp("2025-03-31"))

    returned = gaps.set_index("user_id")["returned"]
    duration = gaps.set_index("user_id")["gap_duration"]

    assert returned[1] == 1
    assert duration[1] == 4.0
    assert returned[2] == 0
    assert duration[2] == 3.0  # days remaining in the window, not a return time


def test_gap_frame_excludes_censored_streaks() -> None:
    """A streak that never broke contributes no gap at all."""
    df = streaks(
        [
            (1, 1, 5, True, None, "2025-03-31"),
            (2, 1, 5, False, 2.0, "2025-03-10"),
        ]
    )
    gaps = sa.build_gap_frame(df, pd.Timestamp("2025-03-31"))

    assert list(gaps["user_id"]) == [2]


def test_gap_frame_drops_zero_exposure_breaks() -> None:
    """A break on the final day had no chance to be followed by a return."""
    df = streaks([(1, 1, 5, False, None, "2025-03-31")])
    gaps = sa.build_gap_frame(df, pd.Timestamp("2025-03-31"))

    assert gaps.empty


# --------------------------------------------------------------------------- #
# Conditional recovery
# --------------------------------------------------------------------------- #


def _recovery_fitter() -> KaplanMeierFitter:
    """A fitted time-to-return curve with a mix of returns and censored rows."""
    durations = [1, 1, 2, 3, 3, 5, 8, 12, 20, 30, 30, 30]
    returned = [1, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 0]
    return sa.fit_recovery(pd.DataFrame({"gap_duration": durations, "returned": returned}))


def test_conditional_recovery_matches_the_survival_ratio() -> None:
    """P(return by h | absent at k) = 1 - S(h)/S(k), read off the fitted curve."""
    kmf = _recovery_fitter()
    table = sa.recovery_by_gap_length(kmf, milestones=[1, 3, 7], horizon=30)

    for _, row in table.iterrows():
        k = row["days_missed"]
        expected = 1 - (kmf.predict(30) / kmf.predict(k))
        assert row["p_return_within_horizon"] == pytest.approx(expected, abs=1e-4)


def test_recovery_odds_fall_as_the_gap_lengthens() -> None:
    """The behavioural claim the intervention day is chosen from."""
    table = sa.recovery_by_gap_length(_recovery_fitter(), milestones=[1, 3, 7, 14], horizon=30)
    odds = table["p_return_within_horizon"].tolist()

    assert odds == sorted(odds, reverse=True)


def test_recovery_table_never_claims_permanence() -> None:
    """A finite window cannot support the word "never" — guard the column name.

    This is a naming test on purpose. The number itself is right either way; what
    makes it dangerous is a column called ``p_never_returns`` getting quoted in a
    memo as permanent churn when it only ever meant "still away at 30 days".
    """
    table = sa.recovery_by_gap_length(_recovery_fitter())

    assert "p_no_return_within_horizon" in table.columns
    assert not [c for c in table.columns if "never" in c.lower()]
    assert (table["horizon_days"] == sa.RECOVERY_HORIZON_DAYS).all()


# --------------------------------------------------------------------------- #
# Segmentation — curves and test must describe the same population
# --------------------------------------------------------------------------- #


def _segmented_frame() -> pd.DataFrame:
    """Two healthy segments plus a tiny third that must be dropped."""
    rows: list[tuple] = []
    uid = 0
    for segment, length in [("big_a", 3), ("big_b", 9)]:
        for _ in range(sa.MIN_SEGMENT_SIZE + 10):
            uid += 1
            rows.append((uid, 1, length, False, 2.0, "2025-03-10", segment))
    for _ in range(3):  # below MIN_SEGMENT_SIZE
        uid += 1
        rows.append((uid, 1, 40, False, 2.0, "2025-03-10", "tiny"))

    return pd.DataFrame(
        rows,
        columns=[
            "user_id",
            "streak_index",
            "streak_length",
            "is_censored",
            "days_to_next_streak",
            "streak_end",
            "segment",
        ],
    )


def test_undersized_segments_are_dropped_from_the_curves() -> None:
    fitters, _ = sa.streak_survival_by_segment(_segmented_frame(), "segment")

    assert set(fitters) == {"big_a", "big_b"}


def test_logrank_runs_on_the_same_population_it_plots() -> None:
    """The tiny segment is extreme; if the test saw it, the p-value would shift.

    Running the test on the full frame while plotting a filtered subset reports a
    p-value for a comparison the reader is not looking at.
    """
    frame = _segmented_frame()
    _, p_filtered = sa.streak_survival_by_segment(frame, "segment")
    _, p_without_tiny = sa.streak_survival_by_segment(
        frame[frame["segment"] != "tiny"].copy(), "segment"
    )

    assert p_filtered == pytest.approx(p_without_tiny)


def test_segment_with_one_usable_level_raises() -> None:
    frame = _segmented_frame()
    frame = frame[frame["segment"].isin(["big_a", "tiny"])]

    with pytest.raises(ValueError, match="fewer than two levels"):
        sa.streak_survival_by_segment(frame, "segment")


# --------------------------------------------------------------------------- #
# Cox model
# --------------------------------------------------------------------------- #


def _recurrent_cox_frame(
    n_users: int = 200,
    streaks_per_user: int = 6,
    beta: float = 0.5,
    frailty_sd: float = 1.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic recurrent-event data with a per-user shared frailty.

    ``frailty_sd`` is the knob that matters. Each user draws one lognormal
    frailty ``z`` that scales the hazard of *all* their streaks, which is what
    actually creates within-user correlation — some users are simply stickier
    than their covariates explain. At ``frailty_sd = 0`` the rows are genuinely
    independent and clustering has nothing to correct.

    The covariate is fixed within a user, the realistic case here: city tier and
    acquisition channel do not change between one streak and the next.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for user_id in range(n_users):
        x = float(user_id % 2)
        z = rng.lognormal(0.0, frailty_sd) if frailty_sd else 1.0
        rate = 0.15 * np.exp(beta * x) * z
        for _ in range(streaks_per_user):
            rows.append(
                {
                    "user_id": user_id,
                    "streak_length": max(float(rng.exponential(1 / rate)), 0.5),
                    "event": 1,
                    "x": x,
                }
            )
    return pd.DataFrame(rows)


def _ci_width(cph, covariate: str = "x") -> float:
    row = cph.summary.loc[covariate]
    return float(row["coef upper 95%"] - row["coef lower 95%"])


def test_clustering_widens_the_interval_when_streaks_are_correlated() -> None:
    """The central statistical claim of the module.

    Streaks are recurrent: 50k of them come from 5k users, and a user's streaks
    resemble each other. Treating them as independent shrinks standard errors
    toward a precision the data does not have. With real within-user correlation
    present, the clustered interval must come out clearly wider — if this test
    fails, hazard ratios are being published with false confidence.
    """
    frame = _recurrent_cox_frame(frailty_sd=1.0)

    clustered = _ci_width(sa.fit_cox(frame, cluster_col="user_id"))
    naive = _ci_width(sa.fit_cox(frame, cluster_col=None))

    assert clustered > naive * 1.25


def test_clustering_is_near_neutral_when_rows_are_independent() -> None:
    """The other half of the claim, and the one that keeps it honest.

    Clustering is a correction, not a penalty. Given genuinely independent rows
    it should land close to the naive fit. Asserting only that "clustered is
    wider" would also pass for an estimator that simply inflates everything, so
    this pins the behaviour from the opposite side.
    """
    frame = _recurrent_cox_frame(frailty_sd=0.0)

    clustered = _ci_width(sa.fit_cox(frame, cluster_col="user_id"))
    naive = _ci_width(sa.fit_cox(frame, cluster_col=None))

    assert clustered == pytest.approx(naive, rel=0.15)


def test_naive_fit_drops_the_clustering_key_from_the_covariates() -> None:
    """user_id is an identifier, never a predictor — it must not become one."""
    naive = sa.fit_cox(_recurrent_cox_frame(), cluster_col=None)

    assert "user_id" not in naive.summary.index


def test_cox_frame_carries_user_id_and_no_outcome_derived_covariate() -> None:
    """Leakage guard: nothing known only *after* the streak ends may be a feature."""
    frame = pd.DataFrame(
        {
            "user_id": [1, 1, 2],
            "streak_index": [1, 2, 1],
            "streak_length": [5, 3, 8],
            "is_censored": [False, False, True],
            "signup_date": pd.to_datetime(["2025-01-01"] * 3),
            "kyc_completed_at": pd.to_datetime(["2025-01-02", "2025-01-02", None]),
            "acquisition_channel": ["organic", "organic", "referral"],
            "city_tier": ["tier_1", "tier_1", "tier_2"],
        }
    )
    model_frame = sa.build_cox_frame(frame)

    assert "user_id" in model_frame.columns
    for leaked in ("days_to_next_streak", "recovered", "is_censored", "streak_end"):
        assert leaked not in model_frame.columns

    # "Never verified" is imputed to the slow extreme, and flagged so the
    # imputation cannot pass itself off as a measurement.
    assert model_frame.loc[2, "kyc_incomplete"] == 1
    assert model_frame.loc[2, "kyc_weeks"] == model_frame["kyc_weeks"].max()


def test_first_streak_flag_is_derived_from_streak_index() -> None:
    frame = pd.DataFrame(
        {
            "user_id": [1, 1],
            "streak_index": [1, 2],
            "streak_length": [5, 3],
            "is_censored": [False, False],
            "signup_date": pd.to_datetime(["2025-01-01"] * 2),
            "kyc_completed_at": pd.to_datetime(["2025-01-02"] * 2),
            "acquisition_channel": ["organic", "organic"],
            "city_tier": ["tier_1", "tier_1"],
        }
    )
    model_frame = sa.build_cox_frame(frame)

    assert list(model_frame["is_first_streak"]) == [1, 0]


# --------------------------------------------------------------------------- #
# Chart tokens
# --------------------------------------------------------------------------- #


def test_archetype_colour_is_stable_regardless_of_iteration_order() -> None:
    """Colour follows the entity, not its row number.

    A filter that drops one archetype must not repaint the survivors — a reader
    who learned "early_dropper is orange" keeps that across every figure.
    """
    for theme in viz.THEMES.values():
        first = {name: viz.archetype_color(name, theme) for name in viz.ARCHETYPE_SLOT}
        reversed_order = {
            name: viz.archetype_color(name, theme) for name in reversed(list(viz.ARCHETYPE_SLOT))
        }
        assert first == reversed_order
        assert len(set(first.values())) == len(first)


def test_status_colours_never_collide_with_series_colours() -> None:
    """A status colour must never be mistakable for "just another series"."""
    for theme in viz.THEMES.values():
        assert theme.status_good not in theme.series
        assert theme.status_critical not in theme.series


def test_themes_are_selected_not_inverted() -> None:
    """Dark steps are chosen for the dark surface, not flipped from the light set."""
    assert viz.LIGHT.series != viz.DARK.series
    assert viz.LIGHT.surface != viz.DARK.surface
    assert len(viz.LIGHT.series) == len(viz.DARK.series)
