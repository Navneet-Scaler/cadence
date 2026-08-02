"""Tests for the nudge experiment analysis.

Focused on the statistical machinery, which is where a quiet error would be most
expensive: a wrong standard error or a missing multiple-comparison correction
produces a confident number that is simply false, and nothing downstream would
catch it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from src.analysis import nudge_simulation as ns


def test_z_test_matches_a_hand_computed_statistic() -> None:
    """500/1000 vs 400/1000: pooled p = 0.45, z = 4.4907."""
    result = ns.two_proportion_z_test(500, 1000, 400, 1000, "fixture")

    pooled = 0.45
    expected_se = np.sqrt(pooled * (1 - pooled) * (1 / 1000 + 1 / 1000))
    expected_z = 0.10 / expected_se

    assert result.z_statistic == pytest.approx(expected_z, rel=1e-9)
    assert result.absolute_lift == pytest.approx(0.10)
    assert result.p_value < 1e-5


def test_confidence_interval_uses_the_unpooled_standard_error() -> None:
    """Pooling for the interval is a common shortcut and understates the spread."""
    result = ns.two_proportion_z_test(500, 1000, 400, 1000, "fixture")

    se_unpooled = np.sqrt(0.5 * 0.5 / 1000 + 0.4 * 0.6 / 1000)
    margin = stats.norm.ppf(0.975) * se_unpooled

    assert result.ci_low == pytest.approx(0.10 - margin)
    assert result.ci_high == pytest.approx(0.10 + margin)


def test_identical_rates_give_zero_lift_and_a_null_result() -> None:
    result = ns.two_proportion_z_test(500, 1000, 500, 1000, "fixture")

    assert result.absolute_lift == pytest.approx(0.0)
    assert result.z_statistic == pytest.approx(0.0)
    assert result.p_value == pytest.approx(1.0)
    assert result.ci_low < 0 < result.ci_high


def test_confidence_interval_brackets_the_point_estimate() -> None:
    result = ns.two_proportion_z_test(300, 1000, 250, 1000, "fixture")
    assert result.ci_low < result.absolute_lift < result.ci_high


def test_number_needed_to_treat_is_the_reciprocal_of_the_lift() -> None:
    result = ns.two_proportion_z_test(500, 1000, 400, 1000, "fixture")
    assert result.number_needed_to_treat == pytest.approx(10.0)


def test_number_needed_to_treat_is_infinite_when_the_nudge_does_not_help() -> None:
    result = ns.two_proportion_z_test(400, 1000, 500, 1000, "fixture")
    assert not np.isfinite(result.number_needed_to_treat)


def make_test(p_value: float, label: str = "x") -> ns.ProportionTest:
    return ns.ProportionTest(
        label=label,
        treated_n=1000,
        treated_recovered=500,
        control_n=1000,
        control_recovered=400,
        treated_rate=0.5,
        control_rate=0.4,
        absolute_lift=0.1,
        relative_lift=0.25,
        ci_low=0.05,
        ci_high=0.15,
        z_statistic=4.5,
        p_value=p_value,
    )


def test_holm_adjustment_matches_the_hand_computed_sequence() -> None:
    """Sorted p-values are scaled by (m - i), then made monotonic."""
    tests = [make_test(p) for p in [0.01, 0.04, 0.03]]
    ns.holm_bonferroni(tests)

    by_raw = {t.p_value: t.p_adjusted for t in tests}
    # sorted: 0.01*3 = 0.03; 0.03*2 = 0.06; 0.04*1 = 0.04 -> raised to 0.06
    assert by_raw[0.01] == pytest.approx(0.03)
    assert by_raw[0.03] == pytest.approx(0.06)
    assert by_raw[0.04] == pytest.approx(0.06)


def test_holm_adjusted_p_values_are_never_below_the_raw_ones() -> None:
    tests = [make_test(p) for p in [0.001, 0.02, 0.3, 0.45, 0.5]]
    ns.holm_bonferroni(tests)
    assert all(t.p_adjusted >= t.p_value for t in tests)


def test_holm_never_exceeds_one() -> None:
    tests = [make_test(p) for p in [0.4, 0.5, 0.6, 0.7, 0.8]]
    ns.holm_bonferroni(tests)
    assert all(t.p_adjusted <= 1.0 for t in tests)


def test_a_borderline_result_can_lose_significance_after_correction() -> None:
    """The whole point of correcting: 0.04 raw is not significant across 5 tests."""
    tests = [make_test(p) for p in [0.04, 0.30, 0.40, 0.50, 0.60]]
    ns.holm_bonferroni(tests)

    borderline = next(t for t in tests if t.p_value == 0.04)
    assert borderline.p_value < ns.ALPHA
    assert not borderline.significant


def test_untested_cells_are_excluded_from_the_correction() -> None:
    """An untested cell must not consume a comparison and weaken real findings."""
    tested = [make_test(p) for p in [0.01, 0.02]]
    skipped = make_test(float("nan"), "small")
    skipped.tested = False

    ns.holm_bonferroni([*tested, skipped])

    # Two comparisons, not three: smallest p is scaled by 2.
    assert tested[0].p_adjusted == pytest.approx(0.02)
    assert not skipped.significant


def make_breaks(rows: list[tuple[str, int, int, bool]]) -> pd.DataFrame:
    """Build a breaks frame from (arm, gap_reached, days_observable, recovered)."""
    frame = pd.DataFrame(
        rows, columns=["arm", "gap_reached", "days_observable", "recovered_in_window"]
    )
    frame["streak_id"] = range(len(frame))
    frame["user_id"] = range(len(frame))
    return frame


def test_risk_set_excludes_breaks_that_recovered_before_the_threshold() -> None:
    """Someone who returned on day 1 was never eligible for a day-3 nudge."""
    breaks = make_breaks(
        [
            ("control", 1, 60, True),  # came back before day 3
            ("control", 5, 60, True),  # still away at day 3
        ]
    )
    risk_set = ns.eligible_at_threshold(breaks, threshold=3)

    assert len(risk_set) == 1
    assert risk_set["gap_reached"].iat[0] == 5


def test_risk_set_excludes_breaks_without_a_full_recovery_window() -> None:
    """Otherwise the comparison scores data truncation as churn."""
    breaks = make_breaks(
        [
            ("control", 5, ns.RECOVERY_WINDOW_DAYS - 1, False),
            ("control", 5, ns.RECOVERY_WINDOW_DAYS, False),
        ]
    )
    risk_set = ns.eligible_at_threshold(breaks, threshold=3)

    assert len(risk_set) == 1
    assert risk_set["days_observable"].iat[0] == ns.RECOVERY_WINDOW_DAYS
