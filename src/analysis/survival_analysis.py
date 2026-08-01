"""Survival analysis of daily SIP streaks.

Two distinct time-to-event questions live here, and keeping them apart is the
whole point of the module:

**1. How long does a streak live?**
    Kaplan-Meier on streak length. The event is "the streak broke"; a streak still
    running on the last day of data is *right-censored*, not dead. Censoring is
    why a plain histogram of streak lengths is wrong: it silently counts every
    still-alive streak as though it had ended on the day the data stopped, which
    biases every survival estimate downward.

**2. Once a streak breaks, does the user come back?**
    Kaplan-Meier on the *gap* — time from a break to the next successful day. The
    event is "returned"; a user still absent at the end of the window is censored,
    because the data cannot yet distinguish "gone for good" from "not back yet".
    This is the estimator that answers the project's central question, and it is
    the one a naive analysis gets wrong: computing "% who never returned" by
    counting rows over-counts churn, because a user who lapsed three days before
    the data ended never had a chance to return.

    From the fitted curve, the decision-relevant quantity is conditional:

        P(returns eventually | already missed k days) = 1 - S(horizon) / S(k)

    which is exactly "if someone has already been gone k days, what are the odds
    we get them back?" — the number that picks the intervention day.

**3. What raises the risk of a break?**
    Cox proportional hazards on streak death, giving hazard ratios per covariate
    (acquisition channel, city tier, KYC speed, weekend signup). A hazard ratio of
    1.4 means that group breaks streaks 40% faster at every point in time, holding
    the others fixed — a statement a founder can act on, unlike a raw chart.

    The model is **clustered on user_id**. Streaks are a recurrent event: 50k
    streaks come from 5k users, and one user's streaks resemble each other far
    more than they resemble a stranger's. Treating them as independent would
    quietly inflate the effective sample size by an order of magnitude and shrink
    every standard error to match, producing confident-looking intervals around
    effects the data cannot actually resolve. Clustering swaps in robust
    sandwich errors, which is the difference between a hazard ratio a founder can
    trust and one that merely looks precise.

A note on ``archetype``: it is simulation ground truth from ``sim_user_profile``,
not an observed production field. It is used here only to *validate* that the
analysis recovers the behaviour that was planted, and is deliberately kept out of
the Cox covariates — a model that predicts churn from the parameter churn was
generated from would be measuring the simulator, not the users.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, KaplanMeierFitter
from lifelines.statistics import logrank_test, multivariate_logrank_test
from matplotlib.ticker import MaxNLocator

from src import db
from src.analysis import viz
from src.analysis.streak_builder import load_streaks

logger = logging.getLogger(__name__)

# Days after a break at which we call a return "recovery". Beyond this the
# conditional estimate is read off the fitted curve, not a raw count.
RECOVERY_HORIZON_DAYS = 30

# Gap lengths the memo quotes. Chosen to bracket plausible nudge trigger days.
GAP_MILESTONES = [1, 2, 3, 5, 7, 14]

# A segment level below this many streaks is dropped from both the curve and the
# log-rank test — a curve fitted on a handful of streaks is noise in the costume
# of a finding.
MIN_SEGMENT_SIZE = 30

# Right edge of the streak-survival plot. Past three weeks every archetype is
# flat against zero, so a wider window spends canvas on nothing and squeezes the
# region where the curves actually separate.
X_LIMIT_STREAK_DAYS = 21


@dataclass
class SurvivalSummary:
    """Headline numbers, so callers don't re-derive them from the fitters."""

    median_streak_length: float
    streak_survival_at: dict[int, float]
    recovery_by_gap: pd.DataFrame
    overall_recovery_rate: float


# --------------------------------------------------------------------------- #
# 1. Streak lifetime
# --------------------------------------------------------------------------- #


def fit_streak_survival(streaks: pd.DataFrame) -> KaplanMeierFitter:
    """Kaplan-Meier fit of streak length.

    Args:
        streaks: one row per streak, with ``streak_length`` and ``is_censored``.

    Returns:
        A fitted :class:`KaplanMeierFitter`. ``S(d)`` is the probability a streak
        is still alive after ``d`` days.
    """
    kmf = KaplanMeierFitter(label="all streaks")
    kmf.fit(
        durations=streaks["streak_length"],
        event_observed=~streaks["is_censored"].astype(bool),
    )
    return kmf


def streak_survival_by_segment(
    streaks: pd.DataFrame, segment: str
) -> tuple[dict[str, KaplanMeierFitter], float]:
    """Fit one survival curve per segment level and test whether they differ.

    The log-rank test is the right companion to the plot: curves that look
    separated can still be within sampling noise, and quoting a difference that
    isn't there is worse than quoting nothing.

    Returns:
        ``(fitters_by_level, logrank_p_value)``.
    """
    # Drop rows with no segment value, then drop levels too small to speak to.
    # The test must run on exactly the population the curves show: testing the
    # full frame while plotting a filtered subset reports a p-value for a
    # comparison the reader is not looking at.
    usable = streaks[streaks[segment].notna()]
    level_sizes = usable[segment].value_counts()
    keep = level_sizes[level_sizes >= MIN_SEGMENT_SIZE].index
    usable = usable[usable[segment].isin(keep)]

    if usable[segment].nunique() < 2:
        raise ValueError(f"segment {segment!r} has fewer than two levels of usable size")

    fitters: dict[str, KaplanMeierFitter] = {}
    for level, group in usable.groupby(segment):
        kmf = KaplanMeierFitter(label=str(level))
        kmf.fit(
            durations=group["streak_length"],
            event_observed=~group["is_censored"].astype(bool),
        )
        fitters[str(level)] = kmf

    test = multivariate_logrank_test(
        usable["streak_length"],
        usable[segment],
        ~usable["is_censored"].astype(bool),
    )
    return fitters, float(test.p_value)


# --------------------------------------------------------------------------- #
# 2. Recovery after a break — the central question
# --------------------------------------------------------------------------- #


def build_gap_frame(streaks: pd.DataFrame, observation_end: pd.Timestamp) -> pd.DataFrame:
    """One row per streak break, framed as a time-to-return problem.

    Censored streaks are dropped: they never broke, so they contribute no gap.
    For a break that was followed by a return, the duration is the observed gap
    and the event is 1. For a break with no return, the duration is the days
    remaining in the observation window and the event is 0 — the user has not
    returned *yet*, which is different from never returning, and only censoring
    keeps that distinction honest.
    """
    breaks = streaks[~streaks["is_censored"].astype(bool)].copy()
    breaks["streak_end"] = pd.to_datetime(breaks["streak_end"])

    observed_gap = breaks["days_to_next_streak"]
    days_remaining = (pd.to_datetime(observation_end) - breaks["streak_end"]).dt.days

    breaks["returned"] = observed_gap.notna().astype(int)
    breaks["gap_duration"] = observed_gap.fillna(days_remaining).astype(float)

    # A break on the final day has zero exposure and tells us nothing.
    breaks = breaks[breaks["gap_duration"] > 0]

    logger.info(
        "gap frame: %s breaks, %.1f%% observed to return, %s censored",
        f"{len(breaks):,}",
        100 * breaks["returned"].mean(),
        f"{int((1 - breaks['returned']).sum()):,}",
    )
    return breaks


def fit_recovery(gaps: pd.DataFrame) -> KaplanMeierFitter:
    """Kaplan-Meier fit of time-to-return after a streak break.

    ``S(k)`` here is the probability a user has **not** come back within ``k``
    days, so ``1 - S(k)`` is the cumulative chance of recovery.
    """
    kmf = KaplanMeierFitter(label="time to return")
    kmf.fit(durations=gaps["gap_duration"], event_observed=gaps["returned"])
    return kmf


def _survival_at(kmf: KaplanMeierFitter, t: float) -> float:
    """Step-function lookup of S(t) that tolerates t between event times."""
    return float(kmf.predict(t))


def recovery_by_gap_length(
    kmf: KaplanMeierFitter,
    milestones: list[int] | None = None,
    horizon: int = RECOVERY_HORIZON_DAYS,
) -> pd.DataFrame:
    """Conditional recovery odds given a gap has already reached k days.

    Computed from the fitted curve rather than by counting rows, because raw
    counts conflate "never came back" with "hasn't come back yet":

        P(return by horizon | still absent at k) = 1 - S(horizon) / S(k)

    The complement is reported as ``p_no_return_within_horizon`` and **not** as
    "never returns". Within a finite observation window the data cannot support
    the word *never*: a user still absent at the 30-day horizon may well return
    on day 40. Overstating a 30-day silence as permanent churn is exactly the
    error this whole censoring-aware module exists to avoid, and it would be
    self-defeating to reintroduce it in the column name a founder ends up
    quoting.

    Returns:
        One row per milestone with the conditional recovery odds, and the horizon
        the estimate is conditional on.
    """
    milestones = milestones or GAP_MILESTONES
    s_horizon = _survival_at(kmf, horizon)

    rows = []
    for k in milestones:
        s_k = _survival_at(kmf, k)
        if s_k <= 0:
            continue
        p_return = 1 - (s_horizon / s_k)
        rows.append(
            {
                "days_missed": k,
                "horizon_days": horizon,
                "p_return_within_horizon": round(p_return, 4),
                "p_no_return_within_horizon": round(1 - p_return, 4),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 3. Cox proportional hazards
# --------------------------------------------------------------------------- #


def build_cox_frame(streaks: pd.DataFrame) -> pd.DataFrame:
    """Assemble the covariate matrix for the Cox model.

    Only covariates fixed at or before the streak starts are used. Anything
    measured after would leak outcome information into the predictor and inflate
    its apparent effect.

    Covariates:
        * acquisition_channel, city_tier — one-hot, reference level dropped
        * kyc_hours — hours from signup to KYC completion; unverified users are
          given the observed maximum, since "never verified" is the slow extreme
        * kyc_incomplete — flag, so the imputation above cannot masquerade as a
          real measurement
        * signed_up_on_weekend — a cheap proxy for casual, low-intent signups
        * is_first_streak — first streak or a later one; habits behave
          differently the second time round

    ``user_id`` is carried through as the clustering key, not as a covariate.
    """
    df = streaks.copy()
    df["signup_date"] = pd.to_datetime(df["signup_date"])
    df["kyc_completed_at"] = pd.to_datetime(df["kyc_completed_at"])

    kyc_hours = (df["kyc_completed_at"] - df["signup_date"]).dt.total_seconds() / 3600
    df["kyc_incomplete"] = kyc_hours.isna().astype(int)
    df["kyc_hours"] = kyc_hours.fillna(kyc_hours.max())
    df["signed_up_on_weekend"] = (df["signup_date"].dt.weekday >= 5).astype(int)
    df["is_first_streak"] = (df["streak_index"] == 1).astype(int)
    df["event"] = (~df["is_censored"].astype(bool)).astype(int)

    design = pd.get_dummies(df[["acquisition_channel", "city_tier"]], drop_first=True, dtype=float)
    design.columns = [c.replace(" ", "_").lower() for c in design.columns]

    model_frame = pd.concat(
        [
            df[
                [
                    "user_id",
                    "streak_length",
                    "event",
                    "kyc_hours",
                    "kyc_incomplete",
                    "signed_up_on_weekend",
                    "is_first_streak",
                ]
            ].reset_index(drop=True),
            design.reset_index(drop=True),
        ],
        axis=1,
    )
    # Scale so the hazard ratio reads "per week of KYC delay", not per hour.
    model_frame["kyc_weeks"] = model_frame.pop("kyc_hours") / (24 * 7)
    return model_frame


def fit_cox(model_frame: pd.DataFrame, cluster_col: str = "user_id") -> CoxPHFitter:
    """Fit a Cox proportional hazards model on streak death.

    Cox is the right tool here because it estimates covariate effects *without*
    assuming a shape for the baseline hazard — we care about which factors move
    the risk, not about fitting the curve itself. Coefficients exponentiate to
    hazard ratios: >1 means the streak breaks faster, <1 means it holds longer.

    Clustered on ``user_id``. Streaks are a *recurrent* event — the same user
    contributes many of them, and their streaks correlate strongly with each
    other. Fitting as though every row were an independent subject would treat
    ~50k correlated streaks as ~50k independent facts, shrinking standard errors
    by roughly the square root of the streaks-per-user ratio and turning noise
    into significance. The robust sandwich estimator that ``cluster_col`` selects
    is what keeps the intervals honest; expect them to widen noticeably against
    an unclustered fit, and treat that widening as the correction working.

    Args:
        model_frame: output of :func:`build_cox_frame`.
        cluster_col: grouping key for robust errors. Pass ``None`` to fit the
            naive independent-rows model (used in tests to demonstrate exactly
            how much the naive intervals overstate precision).
    """
    cph = CoxPHFitter(penalizer=0.01)
    frame = model_frame if cluster_col else model_frame.drop(columns=["user_id"], errors="ignore")
    cph.fit(
        frame,
        duration_col="streak_length",
        event_col="event",
        cluster_col=cluster_col,
    )
    return cph


def hazard_ratios(cph: CoxPHFitter) -> pd.DataFrame:
    """Tidy hazard ratio table with confidence intervals and p-values."""
    summary = cph.summary[["exp(coef)", "exp(coef) lower 95%", "exp(coef) upper 95%", "p"]].copy()
    summary.columns = ["hazard_ratio", "ci_lower", "ci_upper", "p_value"]
    summary["significant"] = summary["p_value"] < 0.05
    return summary.sort_values("hazard_ratio", ascending=False).round(4)


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #


def plot_streak_survival_by_archetype(
    fitters: dict[str, KaplanMeierFitter], p_value: float, theme: viz.Theme
) -> None:
    """Survival curves per archetype, direct-labelled at the right edge.

    Four series, so a legend is present; it is also direct-labelled, because two
    categorical slots sit below 3:1 contrast and identity must not rest on colour.
    """
    viz.apply_style(theme)
    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    x_max = X_LIMIT_STREAK_DAYS

    for name, kmf in fitters.items():
        color = viz.archetype_color(name, theme)
        curve = kmf.survival_function_.iloc[:, 0]
        ax.step(curve.index, curve.values, where="post", color=color, label=name, zorder=4)
        ci = kmf.confidence_interval_
        ax.fill_between(
            ci.index,
            ci.iloc[:, 0],
            ci.iloc[:, 1],
            step="post",
            color=color,
            alpha=0.10,
            linewidth=0,
            zorder=2,
        )

    # Direct labels ride each curve at a *distinct height* rather than at its right
    # end. All four curves decay toward zero, so end-labels would stack on top of
    # one another along the axis — the classic converging-series collision, where
    # nudging labels apart detaches them from their lines and reads as noise.
    # Every curve is monotone from 1 to 0, so it crosses any given survival level
    # exactly once; assigning each series its own level places the labels on their
    # own lines at four separated heights, and no collision is possible by
    # construction rather than by tuning offsets.
    ladder = [0.55, 0.40, 0.26, 0.12]
    ranked = sorted(fitters.items(), key=lambda kv: -float(kv[1].predict(x_max)))
    for (name, kmf), target in zip(ranked, ladder, strict=False):
        curve = kmf.survival_function_.iloc[:, 0]
        below = curve[curve <= target]
        if below.empty:
            continue
        x_at = float(below.index[0])
        if x_at > x_max:
            continue
        color = viz.archetype_color(name, theme)
        viz.marker(ax, x_at, target, color, theme)
        viz.label_line_end(ax, x_at, target, name, theme)

    ax.set_xlim(0, x_max)
    ax.set_ylim(0, 1)
    # Days are whole numbers; a tick reading "day 2.5 of streak" means nothing.
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("Day of streak")
    ax.set_ylabel("Probability streak still alive")
    ax.set_title("Streak survival by archetype")
    ax.legend(loc="upper right")

    # Underflows to exactly 0.0 in double precision when separation is extreme;
    # printing "p = 0.000e+00" would read as a bug rather than a result.
    p_label = "p < 1e-300" if p_value == 0 else f"p = {p_value:.2e}"
    ax.text(
        0.99,
        0.42,
        f"log-rank {p_label}",
        transform=ax.transAxes,
        ha="right",
        fontsize=8,
        color=theme.ink_muted,
    )
    viz.save(fig, "streak_survival_by_archetype", theme)


def plot_recovery_curve(kmf: KaplanMeierFitter, table: pd.DataFrame, theme: viz.Theme) -> None:
    """Cumulative probability of returning, as a function of days missed.

    One series, so no legend box — the title names it. The annotated milestones
    are the point of the chart: a reader should be able to take the intervention
    day off the figure without opening the table.
    """
    viz.apply_style(theme)
    fig, ax = plt.subplots(figsize=(8.4, 4.8))

    curve = 1 - kmf.survival_function_.iloc[:, 0]
    ax.step(curve.index, curve.values, where="post", color=theme.series[0], zorder=4)

    ax.set_xlim(0, 30)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Days since the streak broke")
    ax.set_ylabel("Cumulative probability of returning")
    ax.set_title("How likely is a lapsed user to come back?")

    for _, row in table.iterrows():
        k = int(row["days_missed"])
        if k > 14:
            continue
        y = float(1 - _survival_at(kmf, k))
        viz.marker(ax, k, y, theme.series[1], theme)
        # The curve climbs steeply then plateaus: labels sit above the mark while
        # there is headroom to the upper-left, and below it once it flattens.
        dx, dy = (10, 12) if k <= 3 else (10, -14)
        horizon = int(row["horizon_days"])
        viz.label_line_end(
            ax,
            k,
            y,
            f"day {k}: {row['p_no_return_within_horizon']:.0%} still away at {horizon}d",
            theme,
            dx=dx,
            dy=dy,
        )

    viz.save(fig, "recovery_after_break", theme)


def plot_hazard_ratios(ratios: pd.DataFrame, theme: viz.Theme) -> None:
    """Forest plot of hazard ratios with 95% confidence intervals.

    Status colours (not categorical slots) encode direction, because "raises
    risk" is a state rather than an identity. They are paired with the printed
    ratio and the covariate name, so meaning never rests on colour alone.
    Non-significant covariates are drawn in the de-emphasis token rather than
    hidden: an effect the data cannot resolve is a finding, and dropping it would
    silently turn a null result into a missing one.
    """
    viz.apply_style(theme)
    ordered = ratios.sort_values("hazard_ratio")
    fig, ax = plt.subplots(figsize=(8.4, 0.42 * len(ordered) + 1.8))

    for i, (_, row) in enumerate(ordered.iterrows()):
        if not row["significant"]:
            color = theme.deemphasis
        elif row["hazard_ratio"] > 1:
            color = theme.status_critical
        else:
            color = theme.status_good

        ax.plot([row["ci_lower"], row["ci_upper"]], [i, i], color=color, linewidth=2, alpha=0.55)
        viz.marker(ax, float(row["hazard_ratio"]), i, color, theme)
        label = f"{row['hazard_ratio']:.2f}" + ("" if row["significant"] else "  n.s.")
        viz.label_line_end(ax, float(row["ci_upper"]), i, label, theme)

    # Solid, because 1.0 genuinely is a threshold here — no effect.
    ax.axvline(1.0, color=theme.axis, linewidth=1, zorder=1)
    ax.set_yticks(np.arange(len(ordered)))
    ax.set_yticklabels(ordered.index, fontsize=9, color=theme.ink_secondary)
    ax.set_xlabel("Hazard ratio (>1 = streak breaks faster)")
    ax.set_title("What changes the risk of a streak breaking?")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_axisbelow(True)
    viz.save(fig, "hazard_ratios", theme)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run() -> SurvivalSummary:
    """Run the full survival analysis and write figures. Returns headline numbers."""
    streaks = load_streaks()
    if streaks.empty:
        raise RuntimeError("no streaks found — run the streak builder first")

    observation_end = pd.to_datetime(
        db.read_sql("SELECT MAX(txn_date) AS d FROM v_clean_transactions")["d"].iat[0]
    )

    kmf_streak = fit_streak_survival(streaks)
    median_length = float(kmf_streak.median_survival_time_)
    survival_at = {d: round(float(_survival_at(kmf_streak, d)), 4) for d in [3, 7, 14, 30, 60]}
    logger.info("median streak length: %.0f days", median_length)
    logger.info("streak survival: %s", survival_at)

    by_archetype, archetype_p = streak_survival_by_segment(streaks, "archetype")
    logger.info("archetype survival curves differ, log-rank p = %.3e", archetype_p)

    gaps = build_gap_frame(streaks, observation_end)
    kmf_recovery = fit_recovery(gaps)
    recovery_table = recovery_by_gap_length(kmf_recovery)
    logger.info("recovery by days missed:\n%s", recovery_table.to_string(index=False))

    cox_frame = build_cox_frame(streaks)
    cph = fit_cox(cox_frame)
    ratios = hazard_ratios(cph)
    logger.info("hazard ratios:\n%s", ratios.to_string())

    # Dark mode is a selected set of steps, not an inversion, so each theme is
    # rendered from the same data rather than post-processed from the other.
    for theme in viz.THEMES.values():
        plot_streak_survival_by_archetype(by_archetype, archetype_p, theme)
        plot_recovery_curve(kmf_recovery, recovery_table, theme)
        plot_hazard_ratios(ratios, theme)
    logger.info("wrote figures for themes: %s", ", ".join(viz.THEMES))

    overall_recovery = float(gaps["returned"].mean())
    return SurvivalSummary(
        median_streak_length=median_length,
        streak_survival_at=survival_at,
        recovery_by_gap=recovery_table,
        overall_recovery_rate=overall_recovery,
    )


def compare_arms(streaks: pd.DataFrame) -> float:
    """Log-rank test of streak survival between experiment arms.

    Reported alongside the nudge analysis: if the arms were exchangeable at
    assignment, any difference in streak survival is attributable to the nudge.
    """
    treatment = streaks[streaks["arm"] == "treatment"]
    control = streaks[streaks["arm"] == "control"]
    result = logrank_test(
        treatment["streak_length"],
        control["streak_length"],
        ~treatment["is_censored"].astype(bool),
        ~control["is_censored"].astype(bool),
    )
    return float(result.p_value)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    summary = run()
    logger.info(
        "overall observed recovery rate: %.1f%% (censoring-corrected estimates in the table above)",
        100 * summary.overall_recovery_rate,
    )


if __name__ == "__main__":
    main()
