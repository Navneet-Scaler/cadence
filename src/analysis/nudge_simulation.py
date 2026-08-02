"""Did the nudge work, and on which day should it fire?

The experiment
--------------
Users were randomised 50/50 into ``treatment`` and ``control`` at signup, before
any behaviour was observed. That ordering is what makes this a causal contrast
rather than a description: had assignment happened after the first lapse, the
arms would differ in who lapses, and any recovery gap would be selection rather
than effect.

Treatment users receive a nudge the first time a gap reaches their assigned
threshold, drawn from {1, 2, 3, 5, 7} days. Control users never receive one. The
outcome is **recovery**: did the user start a new streak within
``RECOVERY_WINDOW_DAYS`` of the break?

Why a matched comparison, not just treatment vs control
--------------------------------------------------------
A control user has no nudge, so there is no "day the nudge fired" to compare at.
Comparing every treatment break against every control break would conflate the
nudge's effect with the fact that treatment and control breaks happen at
different points in a user's life.

So each treatment break is compared against control breaks **that reached the
same gap length**. A break nudged on day 3 is only compared with control breaks
that also survived unrecovered to day 3. This conditions on the risk set — the
same trick the Kaplan-Meier estimator uses — and it is the difference between
measuring the nudge and measuring who happened to lapse.

The statistics
--------------
Two-proportion z-test per threshold, plus a chi-square test of independence
across the whole table. Both are reported with the absolute lift, its 95%
confidence interval, and the number needed to treat, because a p-value alone
tells a founder that *something* happened but not whether it is worth building.

Multiple comparisons
--------------------
Five thresholds are tested, so at α = 0.05 there is a ~23% chance of at least one
false positive by luck alone. Holm-Bonferroni correction is applied and both raw
and adjusted p-values are reported; a finding that survives only the raw
threshold is labelled as such rather than quietly promoted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from src import db
from src.analysis import viz

logger = logging.getLogger(__name__)

# A user counts as recovered if they start a new streak within this many days of
# the break. Matches the horizon used in the survival module so the two agree.
RECOVERY_WINDOW_DAYS = 30

# Thresholds the experiment assigned nudges at.
THRESHOLDS = [1, 2, 3, 5, 7]

ALPHA = 0.05

# Below this many breaks in either arm, the cell is reported but not tested: a
# z-test on a handful of observations has no power and its p-value is theatre.
MIN_CELL_SIZE = 100


@dataclass
class ProportionTest:
    """Result of one two-proportion comparison."""

    label: str
    treated_n: int
    treated_recovered: int
    control_n: int
    control_recovered: int
    treated_rate: float
    control_rate: float
    absolute_lift: float
    relative_lift: float
    ci_low: float
    ci_high: float
    z_statistic: float
    p_value: float
    p_adjusted: float = float("nan")
    tested: bool = True

    @property
    def significant(self) -> bool:
        return bool(self.tested and self.p_adjusted < ALPHA)

    @property
    def number_needed_to_treat(self) -> float:
        """Nudges to send to gain one extra recovery. Infinite if lift is zero."""
        return float("inf") if self.absolute_lift <= 0 else 1 / self.absolute_lift


def two_proportion_z_test(
    treated_recovered: int,
    treated_n: int,
    control_recovered: int,
    control_n: int,
    label: str,
) -> ProportionTest:
    """Compare two recovery rates.

    The test statistic uses the **pooled** proportion for the standard error,
    which is correct under the null hypothesis that the two rates are equal. The
    confidence interval uses the **unpooled** standard error, because under the
    alternative the rates differ and a pooled estimate would understate the
    spread. Using one for both is a common and quietly wrong shortcut.
    """
    p_treated = treated_recovered / treated_n
    p_control = control_recovered / control_n
    pooled = (treated_recovered + control_recovered) / (treated_n + control_n)

    se_pooled = np.sqrt(pooled * (1 - pooled) * (1 / treated_n + 1 / control_n))
    z = (p_treated - p_control) / se_pooled if se_pooled > 0 else 0.0
    p_value = 2 * (1 - stats.norm.cdf(abs(z)))

    se_unpooled = np.sqrt(
        p_treated * (1 - p_treated) / treated_n + p_control * (1 - p_control) / control_n
    )
    margin = stats.norm.ppf(1 - ALPHA / 2) * se_unpooled
    lift = p_treated - p_control

    return ProportionTest(
        label=label,
        treated_n=treated_n,
        treated_recovered=treated_recovered,
        control_n=control_n,
        control_recovered=control_recovered,
        treated_rate=p_treated,
        control_rate=p_control,
        absolute_lift=lift,
        relative_lift=(lift / p_control) if p_control > 0 else float("nan"),
        ci_low=lift - margin,
        ci_high=lift + margin,
        z_statistic=float(z),
        p_value=float(p_value),
    )


def holm_bonferroni(tests: list[ProportionTest]) -> list[ProportionTest]:
    """Adjust p-values for testing five thresholds at once.

    Holm rather than plain Bonferroni: it controls the same family-wise error
    rate but is uniformly more powerful, so a real effect at one threshold is
    less likely to be discarded for the sin of having been tested alongside four
    others.
    """
    testable = [t for t in tests if t.tested]
    ordered = sorted(testable, key=lambda t: t.p_value)
    m = len(ordered)
    running_max = 0.0
    for index, test in enumerate(ordered):
        adjusted = min(1.0, (m - index) * test.p_value)
        running_max = max(running_max, adjusted)  # enforce monotonicity
        test.p_adjusted = running_max
    return tests


# --------------------------------------------------------------------------- #
# Building the comparison
# --------------------------------------------------------------------------- #


def load_breaks() -> pd.DataFrame:
    """Every streak break, with its arm, gap length, and whether it recovered.

    A break is a streak that actually ended; censored streaks are excluded
    because they never broke. ``gap_reached`` is how many days the user stayed
    away — capped at the days remaining in the observation window, so a break
    near the edge of the data is not scored as a long absence it never had time
    to demonstrate.
    """
    breaks = db.read_sql(
        f"""
        WITH window_end AS (
            SELECT MAX(txn_date) AS last_day FROM v_clean_transactions
        )
        SELECT s.user_id,
               s.streak_id,
               s.streak_end,
               s.streak_length,
               e.arm,
               s.days_to_next_streak,
               -- How long the user actually stayed away, or how long they could
               -- have been observed staying away if they never came back.
               COALESCE(s.days_to_next_streak, (w.last_day - s.streak_end)) AS gap_reached,
               (w.last_day - s.streak_end)                                  AS days_observable,
               (s.days_to_next_streak IS NOT NULL
                AND s.days_to_next_streak <= {RECOVERY_WINDOW_DAYS})        AS recovered_in_window,
               n.days_missed_at_send,
               n.nudge_type
        FROM user_streaks s
        JOIN experiment_assignments e ON e.user_id = s.user_id
        CROSS JOIN window_end w
        LEFT JOIN LATERAL (
            SELECT ns.days_missed_at_send, ns.nudge_type
            FROM nudges_sent ns
            WHERE ns.user_id = s.user_id
              AND ns.sent_date > s.streak_end
              AND ns.sent_date <= s.streak_end + {RECOVERY_WINDOW_DAYS}
            ORDER BY ns.sent_date
            LIMIT 1
        ) n ON TRUE
        WHERE NOT s.is_censored
        """
    )
    logger.info(
        "loaded %s breaks (%s treatment, %s control)",
        f"{len(breaks):,}",
        f"{int((breaks['arm'] == 'treatment').sum()):,}",
        f"{int((breaks['arm'] == 'control').sum()):,}",
    )
    return breaks


def eligible_at_threshold(breaks: pd.DataFrame, threshold: int) -> pd.DataFrame:
    """Breaks that reached ``threshold`` days without recovering.

    This is the risk set. A user who came back on day 1 was never eligible for a
    day-3 nudge and must not sit in the day-3 denominator — including them would
    dilute both arms with people the intervention could never have reached.

    Breaks are also required to have enough remaining observation window to show
    a recovery; otherwise the comparison would score data-truncation as churn.
    """
    return breaks[
        (breaks["gap_reached"] >= threshold) & (breaks["days_observable"] >= RECOVERY_WINDOW_DAYS)
    ]


def test_by_threshold(breaks: pd.DataFrame) -> list[ProportionTest]:
    """Two-proportion test at each nudge threshold, on matched risk sets."""
    tests: list[ProportionTest] = []

    for threshold in THRESHOLDS:
        risk_set = eligible_at_threshold(breaks, threshold)

        # Treated: this break actually received a nudge at this threshold.
        treated = risk_set[
            (risk_set["arm"] == "treatment") & (risk_set["days_missed_at_send"] == threshold)
        ]
        # Control: same risk set, never nudged at all.
        control = risk_set[risk_set["arm"] == "control"]

        if len(treated) < MIN_CELL_SIZE or len(control) < MIN_CELL_SIZE:
            logger.warning(
                "threshold %d: %d treated / %d control — below the %d floor, not tested",
                threshold,
                len(treated),
                len(control),
                MIN_CELL_SIZE,
            )
            tests.append(
                ProportionTest(
                    label=f"day {threshold}",
                    treated_n=len(treated),
                    treated_recovered=int(treated["recovered_in_window"].sum()),
                    control_n=len(control),
                    control_recovered=int(control["recovered_in_window"].sum()),
                    treated_rate=(
                        float(treated["recovered_in_window"].mean())
                        if len(treated)
                        else float("nan")
                    ),
                    control_rate=(
                        float(control["recovered_in_window"].mean())
                        if len(control)
                        else float("nan")
                    ),
                    absolute_lift=float("nan"),
                    relative_lift=float("nan"),
                    ci_low=float("nan"),
                    ci_high=float("nan"),
                    z_statistic=float("nan"),
                    p_value=float("nan"),
                    tested=False,
                )
            )
            continue

        tests.append(
            two_proportion_z_test(
                treated_recovered=int(treated["recovered_in_window"].sum()),
                treated_n=len(treated),
                control_recovered=int(control["recovered_in_window"].sum()),
                control_n=len(control),
                label=f"day {threshold}",
            )
        )

    return holm_bonferroni(tests)


def chi_square_across_thresholds(tests: list[ProportionTest]) -> tuple[float, float, int]:
    """Chi-square test of independence over the whole recovery table.

    Answers a different question from the per-threshold z-tests: not "did the
    nudge work on day 3", but "is recovery related to nudge status at all".
    Reported alongside, because a per-cell result that survives correction while
    the omnibus test is null deserves suspicion.

    Returns:
        ``(chi2_statistic, p_value, degrees_of_freedom)``.
    """
    rows = []
    for test in tests:
        if not test.tested:
            continue
        rows.append([test.treated_recovered, test.treated_n - test.treated_recovered])
        rows.append([test.control_recovered, test.control_n - test.control_recovered])

    table = np.array(rows)
    chi2, p_value, dof, _ = stats.chi2_contingency(table)
    return float(chi2), float(p_value), int(dof)


def randomisation_check() -> pd.DataFrame:
    """Confirm the arms were balanced on characteristics fixed *before* treatment.

    This is the check that earns the right to call the comparison causal, so it
    belongs in the output rather than a footnote: an imbalanced experiment still
    produces a p-value, it just doesn't mean anything.

    Critically, it queries **only pre-treatment covariates** — user counts,
    signup timing, channel mix, city tier, KYC status. Balancing on anything
    downstream of the nudge would be meaningless and actively misleading. Streak
    and break counts in particular are *outcomes*: the treatment arm has ~37%
    more breaks than control precisely because nudges bring users back, and each
    return creates a new streak that can later break. Reading that gap as
    "imbalance" would mistake the effect for a flaw.
    """
    return db.read_sql(
        """
        SELECT e.arm,
               COUNT(*)                                                   AS users,
               MIN(u.signup_date)                                         AS first_signup,
               ROUND(AVG(EXTRACT(EPOCH FROM u.signup_date) / 86400), 1)   AS mean_signup_epoch_day,
               ROUND(AVG((u.city_tier = 'tier_1')::int) * 100, 1)         AS pct_tier_1,
               ROUND(AVG((u.acquisition_channel = 'paid_social')::int) * 100, 1) AS pct_paid_social,
               ROUND(AVG((u.kyc_status = 'verified')::int) * 100, 1)      AS pct_kyc_verified
        FROM experiment_assignments e
        JOIN users u ON u.user_id = e.user_id
        GROUP BY e.arm
        ORDER BY e.arm
        """
    )


def results_frame(tests: list[ProportionTest]) -> pd.DataFrame:
    """Tidy table of every test, for logging and the report."""
    return pd.DataFrame(
        [
            {
                "threshold": test.label,
                "treated_n": test.treated_n,
                "control_n": test.control_n,
                "treated_rate": round(test.treated_rate, 4),
                "control_rate": round(test.control_rate, 4),
                "abs_lift_pp": round(test.absolute_lift * 100, 2),
                "ci_95_pp": (
                    f"[{test.ci_low * 100:.2f}, {test.ci_high * 100:.2f}]" if test.tested else "-"
                ),
                "p_raw": round(test.p_value, 5) if test.tested else None,
                "p_holm": round(test.p_adjusted, 5) if test.tested else None,
                "significant": test.significant,
                "nnt": (
                    round(test.number_needed_to_treat, 1)
                    if np.isfinite(test.number_needed_to_treat)
                    else None
                ),
            }
            for test in tests
        ]
    )


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #


def plot_lift_by_threshold(tests: list[ProportionTest], theme: viz.Theme) -> None:
    """Absolute lift per threshold, with 95% confidence intervals.

    Plots the *difference* with its interval rather than two bars side by side:
    the question is the size of the effect and whether it clears zero, and a
    paired-bar chart makes a reader estimate that difference by eye.
    """
    viz.apply_style(theme)
    testable = [t for t in tests if t.tested]
    fig, ax = plt.subplots(figsize=(8.4, 4.6))

    x = np.arange(len(testable))
    for index, test in enumerate(testable):
        color = theme.status_good if test.significant else theme.deemphasis
        ax.plot(
            [index, index],
            [test.ci_low * 100, test.ci_high * 100],
            color=color,
            linewidth=2,
            alpha=0.55,
        )
        viz.marker(ax, index, test.absolute_lift * 100, color, theme)
        suffix = "" if test.significant else "  n.s."
        viz.label_line_end(
            ax,
            index,
            test.ci_high * 100,
            f"{test.absolute_lift * 100:+.1f}pp{suffix}",
            theme,
            dx=0,
            dy=12,
        )

    ax.axhline(0, color=theme.axis, linewidth=1, zorder=1)
    ax.set_xticks(x)
    ax.set_xticklabels([t.label for t in testable])
    ax.set_xlim(-0.6, len(testable) - 0.4)
    ax.set_xlabel("Days missed when the nudge fired")
    ax.set_ylabel("Lift in 30-day recovery (percentage points)")
    ax.set_title("Where does a nudge actually change the outcome?")
    ax.grid(axis="x", visible=False)
    viz.save(fig, "nudge_lift_by_threshold", theme)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run() -> pd.DataFrame:
    """Run the full nudge analysis, write the figure, return the results table."""
    breaks = load_breaks()

    balance = randomisation_check()
    logger.info("randomisation balance:\n%s", balance.to_string(index=False))

    tests = test_by_threshold(breaks)
    results = results_frame(tests)
    logger.info("nudge effect by threshold:\n%s", results.to_string(index=False))

    chi2, chi_p, dof = chi_square_across_thresholds(tests)
    logger.info("chi-square across thresholds: chi2=%.2f, dof=%d, p=%.3e", chi2, dof, chi_p)

    best = max(
        (t for t in tests if t.significant),
        key=lambda t: t.absolute_lift,
        default=None,
    )
    if best is None:
        logger.warning("no threshold shows a significant effect after Holm correction")
    else:
        logger.info(
            "strongest effect: %s — %+.1f pp (95%% CI [%.1f, %.1f]), NNT %.1f",
            best.label,
            best.absolute_lift * 100,
            best.ci_low * 100,
            best.ci_high * 100,
            best.number_needed_to_treat,
        )

    for theme in viz.THEMES.values():
        plot_lift_by_threshold(tests, theme)

    return results


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    run()


if __name__ == "__main__":
    main()
